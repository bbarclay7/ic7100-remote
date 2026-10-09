"""Smoke test the WebRTC offer/answer path.

This validates the aiortc bridge integration end-to-end without actually
playing audio anywhere — we create a minimal browser-side peer
connection in-process and confirm the server accepts the offer and
returns a valid answer SDP.

ALSA capture/playback are pointed at /dev/null devices: arecord/aplay
will fail silently, but the WebRTC machinery (SDP, ICE, peer
connection state) is fully exercised.
"""
import asyncio
import json
import threading
import time
import unittest
import urllib.request

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription
    AIORTC_AVAILABLE = True
except ImportError:
    AIORTC_AVAILABLE = False

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


@unittest.skipUnless(AIORTC_AVAILABLE, "aiortc not installed")
class WebRTCBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ic7100ctl.webrtc import WebRTCBridge
        cls.transport = MockCIVTransport()
        cls.radio = IC7100(cls.transport)
        # Use a known-bad device — arecord/aplay will fail to start
        # capturing/playing real audio, but the SDP exchange should
        # still complete.
        cls.bridge = WebRTCBridge(
            capture_device='null', playback_device='null')
        cls.bridge.start()
        cls.port = _free_port()
        cls.server = RadioServer(
            cls.radio, host='127.0.0.1', port=cls.port,
            webrtc_bridge=cls.bridge)
        cls.server.start()
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)
        cls.base = f'http://127.0.0.1:{cls.port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.bridge.stop()

    def test_offer_returns_valid_answer(self):
        async def run():
            pc = RTCPeerConnection()
            pc.addTransceiver('audio', direction='sendrecv')
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            # Wait for ICE gathering
            for _ in range(20):
                if pc.iceGatheringState == 'complete':
                    break
                await asyncio.sleep(0.1)
            return pc.localDescription

        loop = asyncio.new_event_loop()
        try:
            offer = loop.run_until_complete(run())
        finally:
            loop.close()

        req = urllib.request.Request(
            f'{self.base}/webrtc/offer',
            data=json.dumps({'sdp': offer.sdp, 'type': offer.type}).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(req, timeout=10) as r:
            answer = json.loads(r.read().decode())

        self.assertIn('sdp', answer, f"answer missing sdp: {answer}")
        self.assertIn('type', answer)
        self.assertEqual(answer['type'], 'answer')
        # SDP must contain an audio m-line and Opus payload-type mapping.
        self.assertIn('m=audio', answer['sdp'])
        self.assertIn('opus', answer['sdp'].lower())

    def test_offer_without_bridge_returns_503(self):
        # Spin a fresh server WITHOUT a bridge to verify graceful failure.
        port = _free_port()
        srv = RadioServer(self.radio, host='127.0.0.1', port=port,
                          webrtc_bridge=None)
        srv.start()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        time.sleep(0.1)
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/webrtc/offer',
                data=json.dumps({'sdp': 'v=0', 'type': 'offer'}).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST')
            try:
                urllib.request.urlopen(req, timeout=2)
                self.fail("Expected HTTPError")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 503)
        finally:
            srv.stop()


class TxGateTests(unittest.TestCase):
    def test_gate_follows_the_radios_transmitting_flag(self):
        import types
        from ic7100ctl import webrtc
        bridge = types.SimpleNamespace(radio=types.SimpleNamespace(transmitting=False))
        self.assertFalse(webrtc.WebRTCBridge._tx_gate(bridge))
        bridge.radio.transmitting = True
        self.assertTrue(webrtc.WebRTCBridge._tx_gate(bridge))

    def test_gate_is_open_while_a_hold_to_talk_key_is_held(self):
        import types
        from ic7100ctl import webrtc
        radio = types.SimpleNamespace(transmitting=False, _ptt_deadline=None)
        bridge = types.SimpleNamespace(radio=radio)
        self.assertFalse(webrtc.WebRTCBridge._tx_gate(bridge))
        radio._ptt_deadline = time.monotonic() + 1.5        # key-down in flight
        self.assertTrue(webrtc.WebRTCBridge._tx_gate(bridge))
        radio._ptt_deadline = None                          # released
        self.assertFalse(webrtc.WebRTCBridge._tx_gate(bridge))

    def test_mic_audio_is_played_only_while_the_gate_is_open(self):
        try:
            import av
        except ImportError:
            self.skipTest('av not installed')
        import struct
        from ic7100ctl import webrtc

        frame = av.AudioFrame(format='s16', layout='mono', samples=960)
        frame.sample_rate = 48000
        frame.planes[0].update(struct.pack('<960h', *([1000] * 960)))

        class Track:
            def __init__(self): self.n = 0
            async def recv(self):
                self.n += 1
                if self.n > 3:
                    raise RuntimeError('end')
                return frame

        class Sink:
            def __init__(self): self.frames = 0
            def write_frame(self, b): self.frames += 1

        opens = iter([False, True, False])
        sink = Sink()
        asyncio.run(webrtc._consume_tx_track(Track(), sink, asyncio.Event(),
                                             gate=lambda: next(opens)))
        self.assertEqual(sink.frames, 1)       # only the frame that arrived while keyed


class MicActiveTests(unittest.TestCase):
    def test_mic_active_only_while_frames_are_recent(self):
        from ic7100ctl import webrtc
        webrtc._TX_STATE['t'] = time.monotonic()
        self.assertTrue(webrtc.WebRTCBridge.mic_active(None))
        webrtc._TX_STATE['t'] = time.monotonic() - 5
        self.assertFalse(webrtc.WebRTCBridge.mic_active(None))


class TxPeakTests(unittest.TestCase):
    def test_peak_of_audio_played_to_the_radio_is_tracked(self):
        try:
            import av
        except ImportError:
            self.skipTest('av not installed')
        import struct
        from ic7100ctl import webrtc

        pcm = [0] * 960; pcm[100] = -12000                               # one loud sample
        frame = av.AudioFrame(format='s16', layout='mono', samples=960); frame.sample_rate = 48000
        frame.planes[0].update(struct.pack('<960h', *pcm))

        class Track:
            def __init__(self): self.n = 0
            async def recv(self):
                self.n += 1
                if self.n > 1:
                    raise RuntimeError('end')
                return frame

        class Sink:
            def write_frame(self, b): pass

        webrtc._TX_STATE.update(peak=0, n=0, fwd=0)
        asyncio.run(webrtc._consume_tx_track(Track(), Sink(), asyncio.Event(), gate=lambda: True))
        self.assertEqual(webrtc._TX_STATE['peak'], 12000)
        self.assertEqual((webrtc._TX_STATE['n'], webrtc._TX_STATE['fwd']), (1, 1))


if __name__ == '__main__':
    unittest.main()
