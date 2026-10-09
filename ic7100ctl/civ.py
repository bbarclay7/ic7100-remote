"""CI-V transport layer for Icom radios.

Owns the serial port, frame encoding/decoding, and lock fairness between a
background poll thread and foreground user commands.

The transport itself is radio-agnostic — it speaks the Icom CI-V framing
(`FE FE <radio> <ctrl> <cmd> [subcmd] [data...] FD`) but knows nothing
about specific opcodes. See `radio.py` for the IC-7100 wrapper.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


# CI-V framing constants
PREAMBLE  = b'\xfe\xfe'
END       = b'\xfd'
OK        = b'\xfb'
NG        = b'\xfa'
CTRL_ADDR = 0xe0      # default controller address (us)


# ---------------------------------------------------------------------------
# BCD helpers — common to most CI-V radios
# ---------------------------------------------------------------------------

def freq_to_bcd(hz: int) -> bytes:
    """Encode frequency in Hz as 5-byte BCD (LSB first, 2 digits/byte)."""
    digits = []
    for _ in range(10):
        digits.append(hz % 10)
        hz //= 10
    return bytes([digits[i] | (digits[i + 1] << 4) for i in range(0, 10, 2)])


def bcd_to_freq(data: bytes) -> int:
    """Decode 5-byte BCD frequency to Hz."""
    hz = 0
    mult = 1
    for byte in data:
        hz += (byte & 0x0f) * mult
        mult *= 10
        hz += ((byte >> 4) & 0x0f) * mult
        mult *= 10
    return hz


def tone_to_bcd(hz: float) -> bytes:
    """Encode CTCSS tone (Hz) as 2-byte BCD (tenths of Hz, e.g. 88.5 -> 08 85)."""
    tenths = round(hz * 10)
    d3 = tenths // 1000
    d2 = (tenths % 1000) // 100
    d1 = (tenths % 100) // 10
    d0 = tenths % 10
    return bytes([(d3 << 4) | d2, (d1 << 4) | d0])


def tone_to_bcd3(hz: float) -> bytes:
    """CTCSS tone as the radio's 3-byte BCD (tenths of Hz): 127.3 -> 00 12 73.
    This is what 1B 00 / 1B 01 take and return."""
    return b'\x00' + tone_to_bcd(hz)


def bcd_to_tone(data: bytes) -> float:
    """Decode 2-byte BCD tone to Hz."""
    hi = (data[0] >> 4) * 1000 + (data[0] & 0x0f) * 100
    lo = (data[1] >> 4) * 10 + (data[1] & 0x0f)
    return (hi + lo) / 10.0


def rit_offset_to_bcd(hz: int) -> bytes:
    """Encode RIT/XIT offset as 3-byte signed BCD (2 bytes magnitude LSB-first + 1 byte sign)."""
    sign = 0x00 if hz >= 0 else 0x01
    mag = abs(int(hz))
    digits = []
    for _ in range(4):
        digits.append(mag % 10)
        mag //= 10
    return bytes([digits[0] | (digits[1] << 4),
                  digits[2] | (digits[3] << 4),
                  sign])


def bcd_to_rit_offset(data: bytes) -> int:
    """Decode 3-byte signed BCD RIT/XIT offset to signed Hz."""
    if len(data) < 3:
        return 0
    mag = ((data[0] & 0x0f)
           + (data[0] >> 4) * 10
           + (data[1] & 0x0f) * 100
           + (data[1] >> 4) * 1000)
    return -mag if data[2] == 0x01 else mag


def pct_to_bcd3(value: int) -> bytes:
    """Encode 0..255 as 2-byte BCD (000-255). Used for level controls."""
    value = max(0, min(255, int(value)))
    h = value // 100
    t = (value % 100) // 10
    u = value % 10
    return bytes([h, (t << 4) | u])


def bcd3_to_value(data: bytes) -> int:
    """Decode 2-byte BCD level (0..255)."""
    if len(data) < 2:
        return 0
    return (data[0] & 0x0f) * 100 + ((data[1] >> 4) & 0x0f) * 10 + (data[1] & 0x0f)


# ---------------------------------------------------------------------------
# CIVTransport
# ---------------------------------------------------------------------------

class CIVTransport:
    """Manages a CI-V serial connection.

    Public methods are safe to call concurrently — a single mutex serialises
    access to the serial port. The transport supports two fairness aids:

    * `mark_poll_thread()`: tag the current thread as the background poller;
      its `transact()` calls use a shorter timeout so a NG opcode can't pin
      the lock for a full second while a user command is waiting.

    * `user_cmd_pending`: a `threading.Event` the caller can check inside its
      polling loop to abort an in-progress poll cycle and yield the lock.
    """

    def __init__(self, port: str, baud: int = 19200, civ_addr: int = 0x88,
                 timeout: float = 1.0, poll_timeout: float = 0.25,
                 ctrl_addr: int = CTRL_ADDR):
        self.port = port
        self.baud = baud
        self.addr = civ_addr
        self.ctrl_addr = ctrl_addr
        self.timeout = timeout
        # Shorter timeout for the poll thread. 0.25 s comfortably covers a
        # real IC-7100 CI-V response (typical 50-150 ms) but is short enough
        # that a NG opcode mid-poll doesn't pin the GUI.
        self.poll_timeout = poll_timeout
        self._tls = threading.local()
        self._serial = None
        self._lock = threading.Lock()
        self.connected = False
        self.consecutive_timeouts = 0   # CI-V commands in a row with no answer
        # Caller-owned abort signal — set by user-command path, checked by
        # the poll loop between reads. Exposed so callers can wait/clear it.
        self.user_cmd_pending = threading.Event()

    # -- Connection management --

    def connect(self) -> bool:
        try:
            import serial  # pyserial — imported lazily so import-time is cheap
            self._serial = serial.Serial(self.port, self.baud, timeout=self.timeout)
            self._serial.reset_input_buffer()
            self.connected = True
            print(f"[CIV] Connected to {self.port} @ {self.baud}", flush=True)
            return True
        except Exception as e:
            print(f"[CIV] Connect failed: {e}", flush=True)
            self.connected = False
            return False

    def disconnect(self) -> None:
        self.connected = False
        try:
            if self._serial:
                self._serial.close()
        except Exception:
            pass
        self._serial = None

    def mark_poll_thread(self) -> None:
        """Mark the current thread as the background poller. transact() will
        use the short poll_timeout for calls from this thread."""
        self._tls.in_poll = True

    # -- Framing --

    def build_frame(self, cmd: int, subcmd: Optional[int] = None,
                    data: bytes = b'') -> bytes:
        body = bytes([self.addr, self.ctrl_addr, cmd])
        if subcmd is not None:
            body += bytes([subcmd])
        body += data
        return PREAMBLE + body + END

    def transact(self, frame: bytes,
                 timeout: Optional[float] = None) -> Optional[bytes]:
        """Send *frame*, read until FD, return response body or None.

        The body is the bytes between the controller address and the FD
        terminator — i.e. starting at the echoed cmd byte.

        Uses in_waiting polling rather than pyserial's blocking read(N).
        pyserial's read(64) would wait for the FULL timeout even after the
        real ~13-byte response arrived (it kept waiting for the remaining
        51 bytes), flooring every CI-V cmd at the configured timeout.
        This loop returns the moment a valid response frame is parsed —
        typically ~23 ms end-to-end on an IC-7100.
        """
        if not self._serial or not self.connected:
            return None
        if timeout is not None:
            _to = float(timeout)
        elif getattr(self._tls, 'in_poll', False):
            _to = self.poll_timeout
        else:
            _to = self.timeout
        with self._lock:
            try:
                self._serial.reset_input_buffer()
                self._serial.write(frame)
                buf = b''
                deadline = time.monotonic() + _to
                while time.monotonic() < deadline:
                    avail = self._serial.in_waiting
                    if avail:
                        buf += self._serial.read(avail)
                        # Response pattern: FE FE E0 <addr> <data...> FD
                        idx = 0
                        while idx < len(buf) - 5:
                            if (buf[idx:idx + 2] == PREAMBLE
                                    and buf[idx + 2] == self.ctrl_addr):
                                end = buf.find(END, idx + 4)
                                if end != -1:
                                    self._answered()
                                    return buf[idx + 4:end]
                            idx += 1
                    else:
                        time.sleep(0.002)
                self._timed_out(frame[4])
                return None
            except Exception as e:
                print(f"[CIV] Transact error: {e}", flush=True)
                self.connected = False
                return None

    # A radio that is switched off times out on every command, several times a
    # second. Log the first timeout, one line when it is clearly not answering,
    # and one when it answers again, instead of thousands of lines an hour.
    QUIET_AFTER_TIMEOUTS = 5

    def _timed_out(self, cmd: int) -> None:
        self.consecutive_timeouts += 1
        n = self.consecutive_timeouts
        if n == 1:
            print(f"[CIV] Timeout on cmd 0x{cmd:02x}", flush=True)
        elif n == self.QUIET_AFTER_TIMEOUTS:
            print("[CIV] radio is not answering (switched off?); polling slows "
                  "down and further timeouts are not logged until it answers",
                  flush=True)

    def _answered(self) -> None:
        n, self.consecutive_timeouts = self.consecutive_timeouts, 0
        if n >= self.QUIET_AFTER_TIMEOUTS:
            print(f"[CIV] radio is answering again (after {n} timeouts)", flush=True)

    def send_raw(self, frame: bytes) -> Optional[bytes]:
        """Send a pre-built CI-V frame (for CAT passthrough). Returns body."""
        return self.transact(frame)
