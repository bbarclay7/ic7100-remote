"""ALSA capture and playback via arecord/aplay subprocess pipes.

WebRTC standardises on 48 kHz, so we capture/play at 48 kHz mono s16le and
chunk into 20 ms frames (the Opus encoder's native frame size). Anything
that doesn't fit that envelope (resampling, channel mixing, anti-pop) is
deliberately delegated to arecord/aplay/PCM2901 so we don't reimplement
ALSA.

Why subprocess pipes and not the pyalsa bindings? pyalsa is finicky on
Arch and not always present on Debian; arecord/aplay are everywhere there
is ALSA. The subprocess model also gives us a clean kill-and-restart
story: if the codec disappears (USB unplug), the pipe closes and we just
respawn on next start().
"""
from __future__ import annotations

import queue
import subprocess
import threading
from typing import Optional


# 20 ms @ 48 kHz mono s16le = 1920 bytes
FRAME_MS = 20
SAMPLE_RATE = 48000
CHANNELS = 1
BYTES_PER_FRAME = SAMPLE_RATE * 2 * CHANNELS * FRAME_MS // 1000  # = 1920


class AlsaCapture:
    """Captures s16le 48 kHz mono PCM from an ALSA device.

    `read_frame()` returns one 20 ms PCM frame (1920 bytes) or None if
    the buffer underran. A short bounded queue (~80 ms) lets the
    WebRTC track absorb minor jitter without growing without bound.
    """

    def __init__(self, device: str, rate: int = SAMPLE_RATE,
                 channels: int = CHANNELS, buffer_frames: int = 4):
        self.device = device
        self.rate = rate
        self.channels = channels
        self._proc: Optional[subprocess.Popen] = None
        self._q: queue.Queue = queue.Queue(maxsize=buffer_frames)
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()

    RETRY_S = 2.0                # how often to retry after the codec disappears

    def _spawn(self) -> subprocess.Popen:
        cmd = [
            'arecord',
            '-D', self.device,
            '-f', 'S16_LE',
            '-r', str(self.rate),
            '-c', str(self.channels),
            '-t', 'raw',
            '--buffer-size=4800',   # 100 ms total ALSA buffer
            '-q',
        ]
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0)

    def start(self) -> None:
        if self._reader is not None and self._reader.is_alive():
            return
        self._stop.clear()
        self._proc = self._spawn()
        self._reader = threading.Thread(
            target=self._read_loop, name='alsa-capture', daemon=True)
        self._reader.start()
        print(f"[audio] capture started on {self.device}", flush=True)

    def _read_loop(self) -> None:
        accum = b''
        reported = False
        while not self._stop.is_set():
            proc = self._proc
            chunk = b''
            if proc is not None:
                need = BYTES_PER_FRAME - len(accum)
                try:
                    chunk = proc.stdout.read(need)
                except (ValueError, OSError):
                    chunk = b''
            if not chunk:
                # EOF: arecord died (codec unplugged, card renumbered...).
                # Respawn it ourselves; nothing else will.
                if self._stop.is_set():
                    break
                if not reported:
                    print(f"[audio] capture lost on {self.device}: retrying every "
                          f"{self.RETRY_S:g} s", flush=True)
                    reported = True
                accum = b''
                if self._stop.wait(self.RETRY_S):
                    break
                try:
                    self._proc = self._spawn()
                except OSError:
                    self._proc = None
                continue
            if reported and self._proc is not None and self._proc.poll() is None:
                print(f"[audio] capture restored on {self.device}", flush=True)
                reported = False
            accum += chunk
            if len(accum) < BYTES_PER_FRAME:
                continue
            frame = accum[:BYTES_PER_FRAME]
            accum = accum[BYTES_PER_FRAME:]
            try:
                # Drop oldest if the consumer fell behind — better to skip
                # than to grow the queue without bound.
                if self._q.full():
                    self._q.get_nowait()
                self._q.put_nowait(frame)
            except queue.Full:
                pass
        print("[audio] capture reader exiting", flush=True)

    def read_frame(self, timeout: float = 0.1) -> Optional[bytes]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None
        if self._reader:
            self._reader.join(timeout=1)
        self._reader = None
        # Drain queue
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break


class AlsaPlayback:
    """Writes s16le 48 kHz mono PCM frames to an ALSA device via aplay.

    `write_frame()` is called from the WebRTC asyncio loop, so it must never
    block: it only queues the frame. A writer thread does the (blocking) pipe
    write. If aplay can't keep up the oldest queued audio is dropped, so a
    stalled codec can no longer freeze the RX audio or the peer connection.
    """

    QUEUE_FRAMES = 25            # ~0.5 s of 20 ms chunks

    def __init__(self, device: str, rate: int = SAMPLE_RATE,
                 channels: int = CHANNELS):
        self.device = device
        self.rate = rate
        self.channels = channels
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=self.QUEUE_FRAMES)
        self._writer: Optional[threading.Thread] = None
        self._stop = threading.Event()

    RETRY_S = 2.0                # how often to retry after the codec disappears

    def _spawn(self) -> subprocess.Popen:
        cmd = [
            'aplay',
            '-D', self.device,
            '-f', 'S16_LE',
            '-r', str(self.rate),
            '-c', str(self.channels),
            '-t', 'raw',
            '--buffer-size=4800',
            '-q',
        ]
        return subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0)

    def start(self) -> None:
        with self._lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._stop.clear()
            self._proc = self._spawn()
            self._writer = threading.Thread(
                target=self._write_loop, name='alsa-playback', daemon=True)
            self._writer.start()
            print(f"[audio] playback started on {self.device}", flush=True)

    def _drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def _write_loop(self) -> None:
        reported = False
        while not self._stop.is_set():
            proc = self._proc
            if proc is None:
                # aplay died (codec unplugged, card renumbered...): respawn it
                # ourselves, dropping the stale audio that piled up meanwhile.
                if self._stop.wait(self.RETRY_S):
                    break
                self._drain()
                try:
                    with self._lock:
                        if self._stop.is_set():
                            break
                        self._proc = self._spawn()
                except OSError:
                    self._proc = None
                continue
            try:
                frame = self._q.get(timeout=0.2)
            except queue.Empty:
                if getattr(proc, 'poll', lambda: None)() is not None:      # died while idle
                    frame = None
                else:
                    continue
            try:
                if frame is None:
                    raise BrokenPipeError
                proc.stdin.write(frame)
                if reported and proc.poll() is None:
                    print(f"[audio] playback restored on {self.device}", flush=True)
                    reported = False
            except (BrokenPipeError, OSError, ValueError):
                with self._lock:
                    if self._proc is proc:
                        self._proc = None
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if not reported:
                    print(f"[audio] playback lost on {self.device}: retrying every "
                          f"{self.RETRY_S:g} s", flush=True)
                    reported = True

    def write_frame(self, frame: bytes) -> None:
        """Queue a frame without ever blocking the caller."""
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            try:
                self._q.get_nowait()          # drop the oldest
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                pass

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc, self._proc = self._proc, None
        if proc:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
