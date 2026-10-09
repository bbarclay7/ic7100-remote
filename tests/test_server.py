"""Smoke test the HTTP server with a mock radio.

Starts a RadioServer on an ephemeral port backed by MockCIVTransport
and exercises each route via urllib. No serial hardware required.
"""
import json
import threading
import time
import unittest
import urllib.request
from urllib.error import HTTPError

from ic7100ctl.radio import IC7100
from ic7100ctl.server import RadioServer
from tests.mock_transport import MockCIVTransport


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.transport = MockCIVTransport(civ_addr=0x88)
        cls.radio = IC7100(cls.transport)
        cls.port = _free_port()
        cls.server = RadioServer(cls.radio, host='127.0.0.1', port=cls.port)
        cls.server.start()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # Give the server a tick to bind.
        time.sleep(0.1)
        cls.base = f'http://127.0.0.1:{cls.port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _get_json(self, path):
        with urllib.request.urlopen(f'{self.base}{path}', timeout=2) as r:
            self.assertEqual(r.status, 200)
            return json.loads(r.read().decode())

    def _post_json(self, path, payload):
        req = urllib.request.Request(
            f'{self.base}{path}',
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=2) as r:
            self.assertEqual(r.status, 200)
            return json.loads(r.read().decode())

    def test_status_endpoint_returns_json(self):
        s = self._get_json('/ic7100/status')
        self.assertIsInstance(s, dict)
        # Expected core fields:
        for k in ('mode', 'freq_hz'):
            self.assertIn(k, s, f"/status missing key {k!r}; got keys={list(s.keys())[:30]}")

    def test_root_serves_html_or_404_gracefully(self):
        try:
            with urllib.request.urlopen(f'{self.base}/', timeout=2) as r:
                self.assertIn(r.status, (200, 404))
                body = r.read()
                self.assertTrue(len(body) > 0)
        except HTTPError as e:
            # Inline fallback page or 404 — either is acceptable.
            self.assertIn(e.code, (404,))

    def test_post_freq(self):
        r = self._post_json('/ic7100cmd', {'cmd': 'freq', 'hz': 145_500_000})
        self.assertIn('ok', r)
        self.assertTrue(r['ok'])
        # Mock should have received an 0x05 frame.
        cmds = [f[4] for f in self.transport.sent]
        self.assertIn(0x05, cmds)

    def test_post_atten(self):
        before = self.transport.cmd_count()
        r = self._post_json('/ic7100cmd', {'cmd': 'atten', 'on': True})
        self.assertTrue(r['ok'])
        # Find an 0x11 frame after `before`.
        atten_frames = [f for f in self.transport.sent[before:] if f[4] == 0x11]
        self.assertGreaterEqual(len(atten_frames), 1)
        # Critical: byte 5 must be 0x12 (the IC-7100 correction).
        self.assertEqual(atten_frames[0][5], 0x12,
                         "Atten on must send 0x11 0x12, not 0x11 0x20")

    def test_squelch_type_tsql_switches_tsql_not_tone(self):
        before = self.transport.cmd_count()
        r = self._post_json('/ic7100cmd', {'cmd': 'squelch_type', 'type': 'tsql'})
        self.assertTrue(r['ok'])
        sent = [bytes(f[4:7]) for f in self.transport.sent[before:] if f[4] == 0x16]
        self.assertIn(b'\x16\x43\x01', sent)          # TSQL on
        self.assertNotIn(b'\x16\x42\x01', sent)       # not TONE
        st = self._get_json('/ic7100/status')
        self.assertTrue(st['tsql_on'])
        self.assertEqual(st['squelch_type'], 'tsql')
        self._post_json('/ic7100cmd', {'cmd': 'squelch_type', 'type': 'noise'})
        self.assertFalse(self._get_json('/ic7100/status')['tsql_on'])

    def test_ctcss_command_accepts_new_and_old_flag_names(self):
        before = self.transport.cmd_count()
        self._post_json('/ic7100cmd', {'cmd': 'ctcss', 'tone_on': True})
        self._post_json('/ic7100cmd', {'cmd': 'ctcss', 'rx_on': True})      # old name for TSQL
        sent = [bytes(f[4:7]) for f in self.transport.sent[before:] if f[4] == 0x16]
        self.assertEqual(sent, [b'\x16\x42\x01', b'\x16\x43\x01'])
        st = self._get_json('/ic7100/status')
        self.assertEqual((st['tone_on'], st['tsql_on']), (True, True))
        self.assertEqual((st['ctcss_tx_on'], st['ctcss_rx_on']), (True, True))  # old keys, same flags

    def test_tx_meters_read_zero_when_not_transmitting(self):
        self.radio.po, self.radio.swr, self.radio.alc = 227, 40, 30
        self.assertFalse(self.radio.transmitting)
        self.server._refresh_state()                # don't depend on the poll timer
        st = self._get_json('/ic7100/status')
        self.assertEqual((st['po'], st['swr'], st['alc']), (0, 0, 0))

    def test_post_mode(self):
        r = self._post_json('/ic7100cmd', {'cmd': 'mode', 'mode': 'FM'})
        self.assertTrue(r['ok'])

    def test_post_af_level(self):
        r = self._post_json('/ic7100cmd', {'cmd': 'af_level', 'level': 50})
        self.assertTrue(r['ok'])
        # Find a 0x14 0x01 frame.
        af_frames = [f for f in self.transport.sent
                     if f[4] == 0x14 and len(f) > 5 and f[5] == 0x01]
        self.assertGreaterEqual(len(af_frames), 1)

    def test_post_unknown_command_does_not_500(self):
        try:
            r = self._post_json('/ic7100cmd', {'cmd': 'nonexistent_xyz'})
            # Either ok:false or some error structure, but no 500.
            self.assertIn('ok', r)
        except HTTPError as e:
            self.assertLess(e.code, 500, "unknown cmd should not 500")


class InputActiveTests(unittest.TestCase):
    """input_active used to be hard-coded False; it must follow the browser's mic stream."""
    def test_status_reports_whether_mic_audio_is_arriving(self):
        class FakeBridge:
            active = False
            def mic_active(self): return self.active

        bridge = FakeBridge()
        radio = IC7100(MockCIVTransport(civ_addr=0x88))
        port = _free_port()
        server = RadioServer(radio, host='127.0.0.1', port=port, webrtc_bridge=bridge)
        server.start()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(0.1)
        try:
            def status():
                server._refresh_state()
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/ic7100/status', timeout=2) as r:
                    return json.loads(r.read().decode())
            self.assertFalse(status()['input_active'])
            bridge.active = True
            self.assertTrue(status()['input_active'])
        finally:
            server.stop()

    def test_no_bridge_means_no_input(self):
        radio = IC7100(MockCIVTransport(civ_addr=0x88))
        server = RadioServer(radio, host='127.0.0.1', port=_free_port())
        server._refresh_state()
        self.assertFalse(server.state['input_active'])


if __name__ == '__main__':
    unittest.main()
