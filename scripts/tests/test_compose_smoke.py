"""The smoke client must retain the guest identity between HTTP requests."""
import importlib.util
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading
import unittest


class SmokeClientTests(unittest.TestCase):
    def test_guest_cookie_survives_followup_request(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get('Cookie'))
                self.send_response(200)
                if self.path == '/create':
                    self.send_header('Set-Cookie', 'guest=fixture; Path=/; HttpOnly')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        path = Path(__file__).resolve().parents[2] / 'backend/scripts/compose_smoke.py'
        spec = importlib.util.spec_from_file_location('compose_smoke', path)
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            self.assertEqual(smoke.request('GET', base + '/create')[0], 200)
            self.assertEqual(smoke.request('GET', base + '/owned-resource')[0], 200)
            self.assertEqual(seen, [None, 'guest=fixture'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
