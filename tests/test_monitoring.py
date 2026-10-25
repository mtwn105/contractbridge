import json
import zipfile
from pathlib import Path

import httpx
import pytest

from contractbridge.analysis import structural_changes
from contractbridge.api import create_app
from contractbridge.config import Settings
from contractbridge.models import Plan
from contractbridge.monitoring import RuntimeMonitor
from contractbridge.storage import Conflict, Store
from contractbridge.util import canonical, sha256_file
from contractbridge.workflow import Workflow
from examples.demo_services import Orders

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'runtime-tests-operator-token-with-32-characters'


class FixtureDiff:
    async def analyze(self, old, new):
        return {'engine': 'test-double-not-oasdiff', 'changes': structural_changes(old, new)}


class FixturePlanner:
    def __init__(self, plan):
        self.plan, self.calls = plan, 0

    async def propose(self, *_):
        self.calls += 1
        return Plan.parse(self.plan), {'provider': 'fixture-not-ai', 'liveInference': False}


class FixtureRunner:
    def __init__(self, root):
        self.root, self.calls = root, 0

    async def build(self, identifier, source_hash):
        self.calls += 1
        source = self.root / 'builds' / identifier / 'source'
        output = source.parent / 'output'
        output.mkdir()
        with zipfile.ZipFile(output / 'adapter.zip', 'w') as archive:
            for path in source.rglob('*'):
                if path.is_file():
                    archive.write(path, path.relative_to(source))
        return {'passed': True, 'artifactHash': sha256_file(output / 'adapter.zip'), 'sourceHash': source_hash,
                'tests': 8, 'verificationMode': 'test-double-not-a-container'}


@pytest.fixture
def monitored(tmp_path, bundle):
    settings = Settings(ROOT, tmp_path / 'artifacts', 'sqlite:///' + str(tmp_path / 'db.sqlite'), TOKEN, upstream_allowlist=('http://provider-api:8080',))
    planner, runner = FixturePlanner(bundle['plan']), FixtureRunner(settings.artifacts)
    workflow = Workflow(settings, Store(settings.database_url), FixtureDiff(), planner, runner)
    state = {'provider': Orders('v1'), 'calls': []}

    def handle(request):
        assert 'authorization' not in request.headers
        state['calls'].append((request.method, request.url.path))
        if request.url.path == '/openapi.json':
            document = bundle['oldSpec'] if state['provider'].version == 'v1' else bundle['newSpec']
            return httpx.Response(200, json=document)
        status, result = state['provider'].dispatch(request.method, request.url.path, json.loads(request.content))
        return httpx.Response(status, json=result)
    monitor = RuntimeMonitor(workflow, settings, transport=httpx.MockTransport(handle))
    connection = monitor.create({'name': 'Orders', 'upstreamBaseUrl': 'http://provider-api:8080',
        'sourceSpec': canonical(bundle['oldSpec']), 'fixtureSource': 'synthetic', 'fixtures': bundle['fixtures']})
    return monitor, connection, state, planner, runner


async def test_working_traffic_breaks_and_creates_one_automatic_repair(monitored, bundle):
    monitor, c, state, planner, runner = monitored
    payload = bundle['fixtures'][0]['clientRequest']
    response = await monitor.gateway(c['id'], 'POST', '/v1/orders', payload)
    assert response.status_code == 201 and not monitor.resources.list('incidents')
    state['provider'] = Orders('v2')
    response = await monitor.gateway(c['id'], 'POST', '/v1/orders', payload)
    assert response.status_code == 404 and response.background
    assert state['calls'].count(('POST', '/v1/orders')) == 2  # once per caller, no retry
    incident_id = json.loads(response.body)['incidentId']
    again = await monitor.gateway(c['id'], 'POST', '/v1/orders', payload)
    assert json.loads(again.body)['incidentId'] == incident_id and again.background is None
    await response.background()
    incident = monitor.resources.get('incidents', incident_id)
    assert incident['state'] == 'READY_FOR_REVIEW' and planner.calls == runner.calls == 1
    m = monitor.workflow.store.get(incident['migrationId'])
    assert m['state'] == 'NEEDS_REVIEW' and not m['approvalHash'] and not m['builds']
    assert incident['candidate']['activationApproved'] is False
    stored = str(monitor.resources.list('incidents'))
    assert 'cust-123' not in stored and 'sku-blue' not in stored
    assert 'diff --git' not in await monitor.patch(incident_id)  # ordinary unified patch
    assert 'adapter.py' in await monitor.patch(incident_id)
    with pytest.raises(Conflict):
        await monitor.route(c['id'], m['id'], monitor.resources.get('connections', c['id'])['revision'])


async def test_invalid_client_input_never_reaches_provider(monitored):
    monitor, c, state, planner, runner = monitored
    response = await monitor.gateway(c['id'], 'POST', '/v1/orders', {})
    assert response.status_code == 422 and not state['calls']
    assert not monitor.resources.list('incidents') and planner.calls == runner.calls == 0


async def test_unchanged_spec_does_not_invent_a_mapping(monitored, bundle):
    monitor, c, state, planner, runner = monitored
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    assert monitor.resources.get('incidents', incident['id'])['state'] == 'NO_CONTRACT_CHANGE'
    assert planner.calls == runner.calls == 0


async def test_missing_fixtures_preserves_proposal_without_claiming_verification(monitored, bundle):
    monitor, c, state, planner, runner = monitored
    monitor.resources.update('connections', c['id'], 'TEST_FIXTURES_REMOVED', lambda v: v.update(fixtures=[]))
    state['provider'] = Orders('v2')
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    assert monitor.resources.get('incidents', incident['id'])['state'] == 'NEEDS_FIXTURES'
    assert planner.calls == 1 and runner.calls == 0


async def test_provider_change_and_artifact_tampering_block_publication(monitored, bundle):
    monitor, c, state, _, _ = monitored
    state['provider'] = Orders('v2')
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    i = monitor.resources.get('incidents', incident['id'])
    state['provider'] = Orders('v1')
    with pytest.raises(Conflict):
        await monitor.package(i['id'])
    state['provider'] = Orders('v2')
    monitor.artifact(i['candidate']['id']).write_bytes(b'changed')
    with pytest.raises(Conflict):
        await monitor.package(i['id'])


async def test_access_errors_do_not_trigger_mapping_or_retries(monitored):
    monitor, c, _, planner, runner = monitored
    incident, _ = monitor.record_failure(c['id'], 'ACCESS_OR_RATE_LIMIT', 401)
    await monitor.repair(incident['id'])
    assert monitor.resources.get('incidents', incident['id'])['state'] == 'ACTION_REQUIRED'
    assert planner.calls == runner.calls == 0


def test_origin_allowlist_and_restart_recovery(monitored, bundle):
    monitor, c, _, _, _ = monitored
    with pytest.raises(ValueError):
        monitor.create({'name': 'Unsafe', 'upstreamBaseUrl': 'http://169.254.169.254', 'sourceSpec': canonical(bundle['oldSpec']), 'fixtureSource': 'synthetic'})
    incident, _ = monitor.record_failure(c['id'], 'CONTRACT_MISMATCH', 404)
    monitor.stage(incident['id'], 'PROPOSING')
    monitor.recover()
    assert monitor.resources.get('incidents', incident['id'])['state'] == 'FAILED'


async def test_runtime_endpoints_keep_operator_authorization_and_revisions(monitored, bundle):
    monitor, c, _, _, _ = monitored
    app = create_app(monitor.settings, store=monitor.workflow.store, diff=monitor.workflow.diff, planner=monitor.workflow.planner, runner=monitor.workflow.runner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://control') as client:
        assert (await client.get('/api/v1/connections')).status_code == 401
        headers = {'Authorization': 'Bearer ' + TOKEN}
        assert (await client.get('/api/v1/connections', headers=headers)).status_code == 200
        r = await client.post('/api/v1/connections/' + c['id'] + '/route', headers=headers, json={'confirmed': True, 'migrationId': 'bad'})
        assert r.status_code == 409


async def test_successful_http_with_changed_response_shape_creates_incident_without_retry(monitored, bundle):
    monitor, c, state, planner, _ = monitored
    seen = []
    def changed(request):
        seen.append(request.method)
        return httpx.Response(201, json={'purchase': {'id': 'new-id', 'state': 'PLACED'}, 'pricing': {'grandTotal': 1998}})
    monitor.transport = httpx.MockTransport(changed)
    response = await monitor.gateway(c['id'], 'POST', '/v1/orders', bundle['fixtures'][0]['clientRequest'])
    assert response.status_code == 502 and seen == ['POST']
    incident = monitor.resources.list('incidents')[0]
    assert incident['kind'] == 'RESPONSE_SCHEMA_MISMATCH' and planner.calls == 0


async def test_unsupported_mapping_blocks_candidate_code_generation(monitored, bundle):
    monitor, c, state, planner, runner = monitored
    state['provider'] = Orders('v2')
    planner.plan = {**bundle['plan'], 'requestMappings': []}
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    assert monitor.resources.get('incidents', incident['id'])['state'] == 'BLOCKED'
    assert runner.calls == 0


async def test_target_operation_uses_stable_operation_id_when_spec_has_multiple_paths(monitored, bundle):
    import copy
    monitor, c, state, planner, runner = monitored
    bundle['newSpec'] = copy.deepcopy(bundle['newSpec'])
    bundle['newSpec']['paths']['/v2/purchases']['post']['operationId'] = bundle['oldSpec']['paths']['/v1/orders']['post']['operationId']
    bundle['newSpec']['paths']['/other-operation'] = copy.deepcopy(bundle['newSpec']['paths']['/v2/purchases'])
    bundle['newSpec']['paths']['/other-operation']['post']['operationId'] = 'unrelated'
    state['provider'] = Orders('v2')
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    i = monitor.resources.get('incidents', incident['id'])
    assert i['state'] == 'READY_FOR_REVIEW'
    assert monitor.workflow.store.get(i['migrationId'])['targetPath'] == '/v2/purchases'


async def test_publication_requires_confirmation_exact_checksum_and_current_revision(monitored, bundle):
    monitor, c, state, _, _ = monitored
    state['provider'] = Orders('v2')
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    i = monitor.resources.get('incidents', incident['id'])
    app = create_app(monitor.settings, store=monitor.workflow.store, diff=monitor.workflow.diff, planner=monitor.workflow.planner, runner=monitor.workflow.runner)
    app.state.monitor.transport = monitor.transport
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://control', headers={'Authorization': 'Bearer ' + TOKEN}) as client:
        path = '/api/v1/incidents/' + i['id'] + '/publish'
        response = await client.post(path, json={'confirmed': False, 'artifactHash': i['candidate']['artifactHash']}, headers={'If-Match': str(i['revision'])})
        assert response.status_code == 422
        response = await client.post(path, json={'confirmed': True, 'artifactHash': '0' * 64}, headers={'If-Match': str(i['revision'])})
        assert response.status_code == 409
        response = await client.post(path, json={'confirmed': True, 'artifactHash': i['candidate']['artifactHash']}, headers={'If-Match': str(i['revision'] + 1)})
        assert response.status_code == 409
    assert monitor.resources.get('incidents', incident['id'])['publication'] is None


async def test_actual_provider_recovery_closes_incident_without_activating_and_new_break_starts_fresh(monitored, bundle):
    monitor, c, state, _, _ = monitored
    state['provider'] = Orders('v2')
    first = await monitor.gateway(c['id'], 'POST', '/v1/orders', bundle['fixtures'][0]['clientRequest'])
    identifier = json.loads(first.body)['incidentId']
    await first.background()
    assert monitor.resources.get('incidents', identifier)['state'] == 'READY_FOR_REVIEW'
    state['provider'] = Orders('v1')
    recovered = await monitor.gateway(c['id'], 'POST', '/v1/orders', bundle['fixtures'][0]['clientRequest'])
    assert recovered.status_code == 201
    assert monitor.resources.get('incidents', identifier)['state'] == 'RESOLVED'
    c = monitor.resources.get('connections', c['id'])
    assert c['route']['mode'] == 'DIRECT' and c['openIncidentId'] is None
    state['provider'] = Orders('v2')
    new = await monitor.gateway(c['id'], 'POST', '/v1/orders', bundle['fixtures'][0]['clientRequest'])
    assert json.loads(new.body)['incidentId'] != identifier and new.background


class RecordingPublisher:
    configured = True
    repository = 'test-owner/test-repo'
    base = 'main'

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def publish(self, incident, files, body):
        self.calls.append((incident['candidate']['artifactHash'], sorted(files), body))
        if self.fail:
            raise RuntimeError('Test transport intentionally unavailable')
        return {'url': 'https://github.com/test-owner/test-repo/pull/1', 'number': 1, 'draft': True, 'verificationMode': 'test-double-not-a-real-pr'}


async def test_opt_in_automatic_draft_publication_does_not_approve_or_route(monitored):
    monitor, c, state, _, _ = monitored
    publisher = RecordingPublisher()
    monitor.publisher = publisher
    monitor.configure_publication(c['id'], True, c['revision'])
    state['provider'] = Orders('v2')
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    i = monitor.resources.get('incidents', incident['id'])
    assert i['state'] == 'READY_FOR_REVIEW' and i['publication']['status'] == 'CREATED'
    assert len(publisher.calls) == 1 and publisher.calls[0][0] == i['candidate']['artifactHash']
    assert '## Request mappings' in publisher.calls[0][2] and '/items[]/sku' in publisher.calls[0][2]
    assert '## Response mappings' in publisher.calls[0][2] and 'PLACED' in publisher.calls[0][2]
    assert '## Business assumptions to review' in publisher.calls[0][2]
    m = monitor.workflow.store.get(i['migrationId'])
    assert m['approvalHash'] is None and m['state'] == 'NEEDS_REVIEW' and m['activeBuildId'] is None
    assert monitor.resources.get('connections', c['id'])['route']['mode'] == 'DIRECT'
    authorizations = [e for e in monitor.resources.audit(c['id']) if e['action'] == 'AUTOMATIC_DRAFT_PUBLICATION_AUTHORIZED']
    assert authorizations[-1]['metadata']['publicationAuthorization']['repository'] == publisher.repository


async def test_failed_automatic_publication_preserves_passing_candidate(monitored):
    monitor, c, state, _, _ = monitored
    monitor.publisher = RecordingPublisher(fail=True)
    monitor.configure_publication(c['id'], True, c['revision'])
    state['provider'] = Orders('v2')
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    i = monitor.resources.get('incidents', incident['id'])
    assert i['state'] == 'READY_FOR_REVIEW' and i['candidate']['passed']
    assert i['publication']['status'] == 'RETRY_REQUIRED' and i['publicationError']


async def test_auto_publication_cannot_follow_a_changed_repository_without_new_authorization(monitored):
    monitor, c, state, _, _ = monitored
    publisher = RecordingPublisher()
    monitor.publisher = publisher
    monitor.configure_publication(c['id'], True, c['revision'])
    publisher.repository = 'different-owner/different-repo'
    state['provider'] = Orders('v2')
    incident, _ = monitor.record_failure(c['id'], 'HTTP_CONTRACT_MISMATCH', 404)
    await monitor.repair(incident['id'])
    i = monitor.resources.get('incidents', incident['id'])
    assert i['candidate']['passed'] and not publisher.calls
    assert i['publication']['status'] == 'RETRY_REQUIRED'
