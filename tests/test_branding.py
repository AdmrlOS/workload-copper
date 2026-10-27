import hashlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from service import StateStore, RecordingStore, make_server


class BrandingTests(unittest.TestCase):
    def test_supplied_copper_logo_is_served_offline_without_widening_asset_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = StateStore(steps=800, revision='branding-test', copper_version='test')
            recordings = RecordingStore(Path(temporary) / 'runs')
            server = make_server(state, recordings, ROOT / 'www', '127.0.0.1', 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            origin = f'http://127.0.0.1:{server.server_port}'
            try:
                with urllib.request.urlopen(origin + '/assets/copper-logo.webp') as response:
                    self.assertEqual(response.headers.get_content_type(), 'image/webp')
                    self.assertNotIn('charset=', response.headers['Content-Type'])
                    self.assertEqual(hashlib.sha256(response.read()).hexdigest(),
                                     '277380eeaffcfa4ffb5429b15281eff9caa2c799ebe2d8548437f57980d3bffc')
                for path in ('/assets/../service.py', '/assets/branding.json', '/assets/unknown.webp'):
                    with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(origin + path)
                    self.assertEqual(error.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
