"""Synthetic APIs. dispatch() is shared by the HTTP server and protocol tests."""
import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Orders:
    def __init__(self, version, version_file=None):
        self.version, self.orders, self.lock = version, [], threading.Lock()
        self.version_file = Path(version_file) if version_file else None

    def current_version(self):
        configured = self.version_file and self.version_file.exists()
        version = json.loads(self.version_file.read_text())['version'] if configured else self.version
        if version not in ('v1', 'v2'):
            raise ValueError('Unsupported provider version')
        return version

    def dispatch(self, method, path, body=None):
        try:
            version = self.current_version()
        except (OSError, ValueError, KeyError):
            return 503, {'title': 'Invalid provider configuration', 'status': 503}
        if method == 'GET' and path == '/health':
            return 200, {'status': 'UP', 'version': version}
        if method == 'GET' and path == '/openapi.json':
            root = Path(os.getenv('CONTRACT_DIRECTORY', Path(__file__).resolve().parent / 'orders-v1-to-v2'))
            return 200, json.loads((root / ('openapi-' + version + '.yaml')).read_text())
        route = '/v1/orders' if version == 'v1' else '/v2/purchases'
        if method != 'POST' or path != route:
            return 404, {'title': 'Route not found', 'status': 404}
        try:
            if not isinstance(body, dict):
                raise ValueError('Body must be an object')
            if version == 'v1':
                customer, lines = body['customerId'], body['items']
                allowed, sku, count = {'customerId', 'items'}, 'sku', 'quantity'
            else:
                if set(body['buyer']) != {'id'}:
                    raise ValueError('buyer must contain only id')
                customer, lines = body['buyer']['id'], body['lines']
                allowed, sku, count = {'buyer', 'lines'}, 'productCode', 'count'
            if set(body) != allowed or not isinstance(customer, str) or not customer:
                raise ValueError('Unexpected fields or invalid customer')
            if not isinstance(lines, list) or not lines:
                raise ValueError('At least one item is required')
            for line in lines:
                if set(line) != {sku, count} or not isinstance(line[sku], str) or not line[sku]:
                    raise ValueError('Invalid item')
                if type(line[count]) is not int or not 1 <= line[count] <= 100:
                    raise ValueError('Quantity must be an integer from 1 to 100')
        except (KeyError, TypeError, ValueError) as exc:
            return 400, {'title': 'Invalid request', 'status': 400, 'detail': str(exc)}
        with self.lock:
            identifier = f'ord-{7001 + len(self.orders)}'
            total = sum(line[count] * 999 for line in lines)
            self.orders.append({'id': identifier, 'customer': customer, 'lines': lines})
        if version == 'v1':
            return 201, {'orderId': identifier, 'status': 'CONFIRMED', 'totalAmount': total}
        return 201, {'purchase': {'id': identifier, 'state': 'PLACED'}, 'pricing': {'grandTotal': total}}


def serve(version, port, version_file=None):
    orders = Orders(version, version_file)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond(*orders.dispatch('GET', self.path))

        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    return self.respond(413, {'title': 'Invalid body size', 'status': 413})
                body = json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                return self.respond(400, {'title': 'Invalid JSON', 'status': 400})
            self.respond(*orders.dispatch('POST', self.path, body))

        def respond(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass  # Never log customer payloads.

    ThreadingHTTPServer(('0.0.0.0', port), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', choices=['v1', 'v2'], required=True)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--version-file', help='Optional operator-edited JSON configuration read on every request')
    args = parser.parse_args()
    serve(args.version, args.port, args.version_file)
