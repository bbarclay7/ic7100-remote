"""WebRTC audio bridge between the IC-7100's USB codec and the browser.

We use aiortc, which handles Opus encoding/decoding, SRTP, ICE, and the
RTCPeerConnection state machine. The complications:

1. aiortc is asyncio-native; our HTTP server is sync stdlib http.server.
   We run the aiortc event loop in its own thread and post coroutines
   from the HTTP handler via `run_coroutine_threadsafe`.

2. aiortc's MediaStreamTrack subclasses produce/consume `av.AudioFrame`
   objects (PyAV's audio frame). We pump 48 kHz mono s16le PCM in and
   out, letting aiortc handle Opus framing.

3. Browser TX track: the peer connection receives the browser's mic
   track. We install an `on_track` handler that pulls frames as fast
   as they arrive and writes them to ALSA playback.

PTT is NOT controlled here — the panel keeps using the existing CI-V
command path. The audio bridge is independent: when the user presses
PTT, `IC7100.set_ptt(True)` toggles DATA mode on the radio so the USB
codec is routed to TX, and the audio that's already flowing into the
codec from the browser's mic is what the radio modulates.
"""
from __future__ import annotations

import asyncio
import fractions
import threading
import time
from typing import Optional

try:
    from aiortc import (
        RTCPeerConnection, RTCSessionDescription, MediaStreamTrack,
    )
    import av
    AIORTC_AVAILABLE = True
except ImportError:
    AIORTC_AVAILABLE = False

from .audio import (
    AlsaCapture, AlsaPlayback,
    SAMPLE_RATE, CHANNELS, BYTES_PER_FRAME, FRAME_MS,
)


class RxAudioTrack(MediaStreamTrack if AIORTC_AVAILABLE else object):
    """aiortc track that pulls PCM from an AlsaCapture and feeds it to the
    Opus encoder. One per peer connection.

    The IC-7100's USB codec streams unconditionally — there is no hardware
    squelch gate on the audio side. We gate on `radio.squelch_open` (kept
    fresh by the poll loop) so closed-squelch noise floor isn't pumped
    over the air to the operator.
    """

    kind = "audio"

    def __init__(self, capture: AlsaCapture, radio=None):
        super().__init__()
        self.capture = capture
        self.radio = radio
        self._timestamp = 0
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)

    async def recv(self):
        # aiortc calls recv() in a loop. Block until a 20ms frame is
        # available, then wrap it in an av.AudioFrame.
        loop = asyncio.get_event_loop()
        pcm = await loop.run_in_executor(
            None, self.capture.read_frame, 0.1)
        if pcm is None:
            pcm = b'\x00' * BYTES_PER_FRAME
        elif self.radio is not None and getattr(self.radio, 'squelch_open', True) is False:
            # Squelch closed — drop the noise floor by replacing with
            # silence. The peer connection stays alive (constant frame
            # cadence) so audio resumes immediately when the squelch
            # opens again.
            pcm = b'\x00' * BYTES_PER_FRAME

        # AudioFrame from raw s16 mono. PyAV expects "mono" layout.
        frame = av.AudioFrame(format='s16', layout='mono',
                              samples=BYTES_PER_FRAME // 2)
        frame.planes[0].update(pcm)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = self._time_base
        self._timestamp += BYTES_PER_FRAME // 2
        return frame


# Monotonic time of the last mic frame from any browser; read by the server to
# report whether TX audio is really arriving (see WebRTCBridge.mic_active).
_TX_STATE = {'t': 0.0}


async def _consume_tx_track(track, playback: AlsaPlayback,
                            stop: asyncio.Event, gate=None):
    """Pull s16 audio frames from the browser's mic and play them.

    aiortc decodes incoming Opus → AudioFrame. The frame may not be at
    our target sample rate or layout, so we resample to 48 kHz mono s16
    using PyAV's AudioResampler.

    If *gate* is given, frames are only played while it returns True (the
    radio is keyed). Otherwise the mic audio would just pile up in the
    codec's playback buffer, and the first words of a transmission would
    be old audio.
    """
    resampler = av.AudioResampler(format='s16', layout='mono',
                                  rate=SAMPLE_RATE)
    while not stop.is_set():
        try:
            frame = await track.recv()
        except Exception:
            break
        _TX_STATE['t'] = time.monotonic()
        resampled_frames = resampler.resample(frame)
        # PyAV ≥ 9 returns a list; older returns a single frame. Normalise.
        if not isinstance(resampled_frames, list):
            resampled_frames = [resampled_frames]
        for rf in resampled_frames:
            if rf is None:
                continue
            if gate is not None and not gate():
                continue
            buf = bytes(rf.planes[0])
            # aplay accepts arbitrary chunk sizes; no need to align to
            # our 20 ms frame boundary.
            playback.write_frame(buf)


class WebRTCBridge:
    """Owns the asyncio event loop + a pool of active peer connections.

    Public methods are thread-safe: `handle_offer()` may be called from
    the HTTP server thread. Internally it dispatches to the bridge's
    event loop via run_coroutine_threadsafe.
    """

    def __init__(self, capture_device: str, playback_device: str, radio=None):
        if not AIORTC_AVAILABLE:
            raise RuntimeError(
                "aiortc is not installed. Install with: pip install ic7100ctl[audio]"
            )
        self.radio = radio
        self.capture = AlsaCapture(capture_device)
        self.playback = AlsaPlayback(playback_device)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pcs = set()
        self._consumer_tasks = []
        self._stop = asyncio.Event()
        self._started = threading.Event()

    def mic_active(self) -> bool:
        """True while browser mic audio has arrived in the last second."""
        return (time.monotonic() - _TX_STATE['t']) < 1.0

    def _tx_gate(self) -> bool:
        """True while the radio is keyed: only then is browser mic audio played."""
        return bool(getattr(self.radio, 'transmitting', False))

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(
            target=self._run_loop, name='webrtc-loop', daemon=True)
        self._thread.start()
        self._started.wait()
        self.capture.start()
        self.playback.start()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop = asyncio.Event()
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def stop(self) -> None:
        if not self._loop:
            return
        # Close all peer connections and stop the loop.
        async def _shutdown():
            for pc in list(self._pcs):
                try:
                    await pc.close()
                except Exception:
                    pass
            self._pcs.clear()
        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=2)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)
        self.capture.stop()
        self.playback.stop()

    def handle_offer(self, sdp: str, sdp_type: str) -> dict:
        """Process a browser offer and return our answer.

        Called from the HTTP handler thread. Blocks until the answer is
        ready (typically <1 s).
        """
        if not self._loop:
            raise RuntimeError("WebRTCBridge not started")
        fut = asyncio.run_coroutine_threadsafe(
            self._handle_offer_async(sdp, sdp_type), self._loop)
        return fut.result(timeout=10)

    async def _handle_offer_async(self, sdp: str, sdp_type: str) -> dict:
        pc = RTCPeerConnection()
        self._pcs.add(pc)

        # Server-side RX track (radio → browser). Gated on radio squelch.
        rx_track = RxAudioTrack(self.capture, radio=self.radio)
        pc.addTrack(rx_track)

        # Browser-side TX: when we get a track, consume it and play
        # it on ALSA. The DATA-mode toggle + actual keying are handled
        # by the existing CI-V PTT path — audio just keeps flowing
        # into the codec; the radio picks it up when DATA mode says so.
        consumer_stop = asyncio.Event()

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                task = asyncio.ensure_future(
                    _consume_tx_track(track, self.playback, consumer_stop,
                                      gate=self._tx_gate))
                self._consumer_tasks.append(task)

        @pc.on("connectionstatechange")
        async def on_state_change():
            if pc.connectionState in ("failed", "closed", "disconnected"):
                consumer_stop.set()
                self._pcs.discard(pc)

        offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        }
