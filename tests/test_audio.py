"""AlsaPlayback must never block the caller (it runs on the WebRTC asyncio loop), and AlsaCapture /
AlsaPlayback must recover by themselves when the codec disappears (USB unplug / re-plug)."""
import threading
import time
import unittest
from unittest import mock

from ic7100ctl.audio import AlsaCapture, AlsaPlayback, BYTES_PER_FRAME


class PlaybackNeverBlocksTests(unittest.TestCase):
    def test_write_frame_returns_immediately_even_if_aplay_is_stuck(self):
        class StuckStdin:
            def write(self, b): time.sleep(5)          # aplay not draining
            def close(self): pass

        class FakeProc:
            stdin = StuckStdin()
            def terminate(self): pass
            def wait(self, timeout=None): pass
            def kill(self): pass

        with mock.patch('ic7100ctl.audio.subprocess.Popen', return_value=FakeProc()):
            pb = AlsaPlayback('null'); pb.start()

            def burst():
                for _ in range(500):                    # far more than the queue holds
                    pb.write_frame(b'\x00' * 1920)

            t = threading.Thread(target=burst, daemon=True)
            t0 = time.monotonic(); t.start(); t.join(2.0)
            self.assertFalse(t.is_alive(), 'write_frame blocked on a stuck aplay')
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertLessEqual(pb._q.qsize(), pb.QUEUE_FRAMES)


def _wait(cond, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class PlaybackRecoveryTests(unittest.TestCase):
    def test_aplay_is_respawned_after_the_pipe_breaks(self):
        class Dead:
            class stdin:
                @staticmethod
                def write(b): raise BrokenPipeError
                @staticmethod
                def close(): pass
            def poll(self): return 1
            def terminate(self): pass
            def wait(self, timeout=None): pass
            def kill(self): pass

        class Good:
            def __init__(self):
                self.got = []
                outer = self
                class S:
                    def write(self, b): outer.got.append(b)
                    def close(self): pass
                self.stdin = S()
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): pass
            def kill(self): pass

        good = Good()
        with mock.patch('ic7100ctl.audio.subprocess.Popen', side_effect=[Dead(), good]) as popen:
            pb = AlsaPlayback('null'); pb.RETRY_S = 0.05; pb.start()
            ok = _wait(lambda: (pb.write_frame(b'\x01' * BYTES_PER_FRAME), bool(good.got))[1])
            pb.stop()
        self.assertTrue(ok, 'no audio reached the respawned aplay')
        self.assertEqual(popen.call_count, 2)


class CaptureRecoveryTests(unittest.TestCase):
    def test_arecord_is_respawned_after_eof(self):
        class Eof:
            class stdout:
                @staticmethod
                def read(n): return b''
            def poll(self): return 1
            def terminate(self): pass
            def wait(self, timeout=None): pass
            def kill(self): pass

        class Live:
            class stdout:
                @staticmethod
                def read(n):
                    time.sleep(0.01)
                    return b'\x02' * n
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): pass
            def kill(self): pass

        with mock.patch('ic7100ctl.audio.subprocess.Popen', side_effect=[Eof(), Live()]) as popen:
            cap = AlsaCapture('null'); cap.RETRY_S = 0.05; cap.start()
            got = []
            ok = _wait(lambda: (got.append(cap.read_frame(0.05)), bool(got[-1]))[1])
            frame = got[-1]
            cap.stop()
        self.assertTrue(ok, 'no frame after the codec came back')
        self.assertEqual(len(frame), BYTES_PER_FRAME)
        self.assertEqual(popen.call_count, 2)


if __name__ == '__main__':
    unittest.main()
