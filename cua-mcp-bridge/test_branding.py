import base64
from pathlib import Path
import unittest
import server


class BrandingTests(unittest.TestCase):
    def test_server_and_every_tool_publish_the_same_embedded_logo(self):
        info = server.handle({'id': 1, 'method': 'initialize'})['result']['serverInfo']
        tools = server.handle({'id': 2, 'method': 'tools/list'})['result']['tools']
        self.assertEqual(info['title'], 'Workbuddy CUA')
        self.assertTrue(info['icons'])
        for tool in tools:
            self.assertEqual(tool['icons'], info['icons'])
        icon = info['icons'][0]
        prefix, encoded = icon['src'].split(',', 1)
        self.assertEqual(prefix, 'data:image/png;base64')
        png = base64.b64decode(encoded, validate=True)
        self.assertEqual(png, (Path(__file__).parent / 'assets/logo-64.png').read_bytes())
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')), (64, 64))
        self.assertEqual(icon['sizes'], ['64x64'])
