import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo_services import Orders

ROOT = Path(__file__).resolve().parents[1] / 'orders-v1-to-v2'


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / 'fixtures/golden.json').read_text())[0]

    def test_legacy_contract_and_expected_direct_failure(self):
        status, body = Orders('v1').dispatch('POST', '/v1/orders', self.fixture['clientRequest'])
        self.assertEqual((status, body), (201, self.fixture['clientResponse']))
        self.assertEqual(Orders('v2').dispatch('POST', '/v1/orders', self.fixture['clientRequest'])[0], 404)

    def test_v2_contract_and_storage(self):
        server = Orders('v2')
        self.assertEqual(server.dispatch('POST', '/v2/purchases', self.fixture['upstreamRequest']),
                         (201, self.fixture['upstreamResponse']))
        self.assertEqual(len(server.orders), 1)

    def test_invalid_request_does_not_create_order(self):
        for version in ['v1', 'v2']:
            server = Orders(version)
            route = '/v1/orders' if version == 'v1' else '/v2/purchases'
            for payload in [None, {}, {'customerId': 'x', 'items': []}]:
                self.assertEqual(server.dispatch('POST', route, payload)[0], 400)
            self.assertEqual(server.orders, [])

    def test_boolean_quantity_rejected(self):
        request = self.fixture['clientRequest']
        request['items'][0]['quantity'] = True
        self.assertEqual(Orders('v1').dispatch('POST', '/v1/orders', request)[0], 400)

    def test_health(self):
        self.assertEqual(Orders('v2').dispatch('GET', '/health')[0], 200)


if __name__ == '__main__':
    unittest.main()


def test_provider_serves_actual_current_specification_and_reads_operator_changes(tmp_path):
    config = tmp_path / 'provider.json'
    config.write_text('{"version":"v1"}')
    provider = Orders('v1', config)
    payload = {'customerId': 'synthetic', 'items': [{'sku': 'blue', 'quantity': 1}]}
    assert provider.dispatch('POST', '/v1/orders', payload)[0] == 201
    assert '/v1/orders' in provider.dispatch('GET', '/openapi.json')[1]['paths']
    config.write_text('{"version":"v2"}')
    assert provider.dispatch('POST', '/v1/orders', payload)[0] == 404
    assert '/v2/purchases' in provider.dispatch('GET', '/openapi.json')[1]['paths']


def test_invalid_provider_configuration_fails_closed(tmp_path):
    config = tmp_path / 'provider.json'
    config.write_text('{"version":"invented"}')
    assert Orders('v1', config).dispatch('GET', '/openapi.json')[0] == 503


def test_missing_version_file_uses_the_startup_version(tmp_path):
    provider = Orders('v1', tmp_path / 'provider.json')
    assert provider.dispatch('GET', '/openapi.json')[0] == 200
    assert '/v1/orders' in provider.dispatch('GET', '/openapi.json')[1]['paths']
