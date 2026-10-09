"""AlsaPlayback must never block the caller (it runs on the WebRTC asyncio loop)."""
import threading
import time
import unittest
from unittest import mock

from ic7100ctl.audio import AlsaPlayback


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


if __name__ == '__main__':
    unittest.main()
