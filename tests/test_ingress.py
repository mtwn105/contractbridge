import httpx
import pytest

from contractbridge.ingress import create_ingress


@pytest.mark.parametrize('prefix,host', [('eeed8e0f-f2be-4700-9f13-b3b0b16d4555', 'cb-proxy-eeed8e0f-f2be-4700-9f13-b3b0b16d4555')])
async def test_ingress_preserves_contract_and_drops_private_headers(prefix, host):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == host
        assert request.url.path == '/v1/orders'
        assert 'authorization' not in request.headers
        assert 'x-bridge-inspect' not in request.headers
        return httpx.Response(201, json={'orderId': 'synthetic'})
    app = create_ingress(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://ingress') as client:
        result = await client.post('/' + prefix + '/v1/orders', json={'unchanged': True},
                                   headers={'Authorization': 'private', 'X-Bridge-Inspect': 'private'})
    assert result.status_code == 201
    assert result.json() == {'orderId': 'synthetic'}
    assert len(calls) == 1


async def test_ingress_cannot_select_arbitrary_host():
    def handler(_request):
        pytest.fail('No upstream should be contacted')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_ingress(httpx.MockTransport(handler))), base_url='http://ingress') as client:
        assert (await client.post('/metadata.internal/v1/orders', json={})).status_code == 404
