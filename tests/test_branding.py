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
                assets = {
                    'copper-logo.webp': ('image/webp', '277380eeaffcfa4ffb5429b15281eff9caa2c799ebe2d8548437f57980d3bffc'),
                    'admiral-logo-ink.svg': ('image/svg+xml', 'f043907a475f00badae071ede1349e48ba08324aa383fbccf1ba646b9d6880f4'),
                    'Archivo.woff2': ('font/woff2', 'c44a3147770a4e67df0e68fc99249e503dab408165d6a48c4180d8e4c272642c'),
                    'IBMPlexMono-Regular.woff2': ('font/woff2', 'b6e314e84f62a17399dbc54fb50345292417dd4392f133687e3e9488d3eea9f2'),
                    'IBMPlexMono-Medium.woff2': ('font/woff2', '337a34abdf3dfa11bd0213f6345f0d3a6111fcbe251f6cc44627da40f63c68a4'),
                    'paper-tile.webp': ('image/webp', '13f05ad5b2aebd7adc34e5a177d88ceffc25a65f902e86ab5e75b6abac5ccf99'),
                }
                for name, (mime, digest) in assets.items():
                    with self.subTest(asset=name), urllib.request.urlopen(origin + '/assets/' + name) as response:
                        self.assertEqual(response.headers['Content-Type'], mime)
                        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
                        self.assertEqual(hashlib.sha256(response.read()).hexdigest(), digest)
                for path in ('/assets/../service.py', '/assets/branding.json', '/assets/unknown.webp',
                             '/assets/unknown.woff2', '/assets/unknown.svg', '/assets/Archivo.woff',
                             '/assets/%2e%2e/service.py', '/assets/%2e%2e%2fservice.py',
                             '/assets/paper-tile.webp/extra', '/assets/admiral-logo-light.svg',
                             '/assets/console-device-800.webp', '/assets/./Archivo.woff2',
                             '/assets//Archivo.woff2', '/service.py'):
                    with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(origin + path)
                    self.assertEqual(error.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
