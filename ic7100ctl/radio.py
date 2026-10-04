"""Icom IC-7100 opcode wrapper on top of CIVTransport.

All command/response details below were checked against the IC-7100 manual.
Bench-verified corrections kept inline as docstring notes — they're the
non-obvious parts that justify a dedicated library:

* 0x11 attenuator value byte is 0x12 (12 dB, the IC-7100's fixed pad), NOT
  0x20 like the IC-7300/7610. Earlier guesses NG'd the radio.
* 0x0A = Memory-to-VFO, 0x0B = Memory clear. Earlier code had these swapped.
* CALL channels are memory channels 106-109 (144-C1, 144-C2, 430-C1, 430-C2);
  the older 0x08 0xA0 opcode is not a CALL selector: it selects a memory bank
  (A-E, numbered from 1) and needs the bank number as data.
* 0x07 0xD0/0xD1 (Main/Sub band) is for dual-receiver radios (IC-9100). The
  IC-7100 is single-receiver and has no such concept — opcodes removed.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Optional

from .civ import (
    CIVTransport, OK,
    bcd3_to_value, bcd_to_freq, bcd_to_rit_offset, bcd_to_tone,
    freq_to_bcd, pct_to_bcd3, rit_offset_to_bcd, tone_to_bcd3,
)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

# Operating modes (cmd 0x04/0x06, data byte 0)
MODES = {
    0x00: 'LSB', 0x01: 'USB', 0x02: 'AM',  0x03: 'CW',
    0x04: 'RTTY', 0x05: 'FM', 0x06: 'WFM', 0x07: 'CW-R',
    0x08: 'RTTY-R', 0x17: 'DV',
}
MODE_BY_NAME = {v: k for k, v in MODES.items()}

# CTCSS tones supported by IC-7100 (Hz), indices 0-49.
CTCSS_TONES = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9,
    186.2, 192.8, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8,
    250.3, 254.1,
]

# DTCS — standard 104-code list, octal-style numbers stored as plain ints.
DTCS_CODES = [
    23, 25, 26, 31, 32, 36, 43, 47, 51, 53, 54, 65, 71, 72, 73, 74,
    114, 115, 116, 122, 125, 131, 132, 134, 143, 145, 152, 155, 156, 162,
    165, 172, 174, 205, 212, 223, 225, 226, 243, 244, 245, 246, 251, 252,
    255, 261, 263, 265, 266, 271, 274, 306, 311, 315, 325, 331, 332, 343,
    346, 351, 356, 364, 365, 371, 411, 412, 413, 423, 431, 432, 445, 446,
    452, 454, 455, 462, 464, 465, 466, 503, 506, 516, 523, 526, 532, 546,
    565, 606, 612, 624, 627, 631, 632, 654, 662, 664, 703, 712, 723, 731,
    732, 734, 743, 754,
]

# DTCS polarity byte for cmd 0x1B 0x07. Index 0..3:
#   0 = TX normal / RX normal
#   1 = TX normal / RX reverse
#   2 = TX reverse / RX normal
#   3 = TX reverse / RX reverse
_DTCS_POLARITY = [0x00, 0x01, 0x10, 0x11]

_AGC_MAP = {'fast': 0x01, 'mid': 0x02, 'slow': 0x03}
_AGC_RMAP = {v: k for k, v in _AGC_MAP.items()}

# IC-7100 has FOUR CALL channels, addressed as memory channels 106-109:
#   106 = 144-C1, 107 = 144-C2, 108 = 430-C1, 109 = 430-C2.
CALL_CHANNELS = {'144-C1': 106, '144-C2': 107, '430-C1': 108, '430-C2': 109}


def _dtcs_to_bytes(code: int, polarity: int) -> bytes:
    pol = _DTCS_POLARITY[max(0, min(3, int(polarity)))]
    code = max(0, min(999, int(code)))
    h = code // 100
    t = (code % 100) // 10
    u = code % 10
    return bytes([pol, h, (t << 4) | u])


def _bytes_to_dtcs(data: bytes):
    if len(data) < 3:
        return (23, 0)
    try:
        polarity = _DTCS_POLARITY.index(data[0])
    except ValueError:
        polarity = 0
    code = (data[1] & 0x0f) * 100 + ((data[2] >> 4) & 0x0f) * 10 + (data[2] & 0x0f)
    return (code, polarity)


def _ch_to_bcd(ch: int) -> bytes:
    """Encode memory channel 1..999 as 2-byte BCD.
    e.g. 23 -> 00 23 ; 99 -> 00 99 ; 100 -> 01 00."""
    ch = max(0, min(999, int(ch)))
    hi_b = (ch // 100) & 0xFF
    lo = ch % 100
    lo_b = ((lo // 10) << 4) | (lo % 10)
    return bytes([hi_b, lo_b])


# ---------------------------------------------------------------------------
# USB audio codec auto-detect (IC-7100 built-in PCM2901, VID:PID 08bb:2901)
# ---------------------------------------------------------------------------

# The chip is a generic TI PCM2901 — ALSA only sees "USB Audio CODEC", with
# no IC-7100/ICOM string — so keyword-matching `arecord -l` finds nothing.
# Match on USB VID:PID for a positive ID.
_IC7100_AUDIO_USBIDS = ('08bb:2901',)


def find_alsa_card(keywords=('IC-7100', 'ICOM', 'icom', 'IC7100')) -> Optional[str]:
    """Return `hw:N,0` for the IC-7100 USB audio device, or None.

    Tries USB VID:PID match first (specific to the IC-7100's PCM2901 codec).
    Falls back to keyword matching for radios whose codec actually does
    identify itself as IC-7100/ICOM."""
    try:
        for card_dir in sorted(os.listdir('/proc/asound')):
            if not card_dir.startswith('card'):
                continue
            try:
                with open(f'/proc/asound/{card_dir}/usbid') as f:
                    usbid = f.read().strip().lower()
            except (FileNotFoundError, OSError):
                continue
            if usbid in _IC7100_AUDIO_USBIDS:
                # Use plughw so the ALSA plugin layer permits simultaneous
                # capture + playback (the PCM2901 hardware is full-duplex
                # but the raw hw: device doesn't allow two opens).
                return f'plughw:{card_dir[4:]},0'
    except (FileNotFoundError, OSError):
        pass
    try:
        out = subprocess.check_output(
            ['arecord', '-l'], stderr=subprocess.DEVNULL, timeout=5).decode()
        for line in out.split('\n'):
            for kw in keywords:
                if kw.lower() in line.lower():
                    parts = line.split()
                    if parts and parts[0] == 'card':
                        return f'plughw:{parts[1].rstrip(":")},0'
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Settings file location (XDG)
# ---------------------------------------------------------------------------

def _default_settings_path() -> str:
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'ic7100ctl', 'settings.json')


# ---------------------------------------------------------------------------
# IC7100
# ---------------------------------------------------------------------------

class IC7100:
    """IC-7100-specific opcode wrapper sitting on top of a CIVTransport.

    State is cached on the instance — every `set_*` updates the cache on OK,
    every `get_*` refreshes it from the radio. The `poll()` / `poll_settings()`
    helpers walk the readable opcodes so a long-running owner can keep its
    cache (and any GUI) synced to front-panel changes on the radio itself.
    """

    # How often the periodic settings group (AGC/NB/NR/preamp/atten/IF-shift/
    # levels/CTCSS/DTCS) gets refreshed. The settings group is ~16 CI-V round-
    # trips, so the owner usually runs it every Nth cycle of the fast group
    # rather than every time.
    SETTINGS_POLL_OPCODES = (
        'split', 'rit_offset', 'rit_on', 'xit_on', 'agc', 'nb', 'nr',
        'preamp', 'atten', 'if_shift', 'squelch', 'rf_power', 'mic_gain',
        'af_level', 'ctcss', 'dtcs_on',
    )

    def __init__(self, transport: CIVTransport,
                 settings_file: Optional[str] = None):
        self.t = transport
        self.settings_file = settings_file or _default_settings_path()

        # RX state
        self.freq_hz: int = 0
        self.mode: str = 'FM'
        self.filter_idx: int = 1
        self.transmitting: bool = False
        self.smeter: int = 0
        self.squelch_open: bool = True

        # Tone-squelch state
        self.ctcss_tx_hz: float = 88.5
        self.ctcss_rx_hz: float = 88.5
        self.tone_on: bool = False      # CI-V 16 42: TONE (repeater tone encode)
        self.tsql_on: bool = False      # CI-V 16 43: TSQL (tone squelch)
        self.dtcs_on: bool = False
        self.dtcs_code: int = 23
        self.dtcs_polarity: int = 0      # 0..3 — see _DTCS_POLARITY

        # HF feature state
        self.split: bool = False
        self.rit_hz: int = 0
        self.rit_on: bool = False
        self.xit_on: bool = False
        self.agc: str = 'mid'
        self.nb_on: bool = False
        self.nb_level: int = 50          # 0..100 percent
        self.nr_on: bool = False
        self.nr_level: int = 50
        self.preamp: int = 0             # 0=off, 1=pre1, 2=pre2
        self.atten: bool = False         # 12 dB pad
        self.if_shift: int = 50          # 0..100, 50=center
        self.squelch: int = 0
        self.rf_power: int = 0
        self.mic_gain: int = 50
        self.af_level: int = 50

        # TX meters (raw 0..255, meaningful only while transmitting)
        self.po: int = 0
        self.swr: int = 0
        self.alc: int = 0

        # DATA mode (USB-D / FM-D / etc.) — controls which MOD INPUT the radio
        # uses on TX. With DATA OFF MOD=MIC and DATA MOD=USB configured on
        # the radio, toggling this swaps between the operator's hand mic and
        # the host's USB audio.
        self.data_mode: bool = False
        self.data_mode_filter: int = 1
        # Tracks whether WE engaged data mode for a programmatic TX cycle;
        # set_ptt() auto-engages on key-up and undoes on key-down, but only
        # if we set it — never disturbs a user-engaged data mode.
        self._we_set_data_mode: bool = False

        # VFO / memory state. Unverified read-back; tracked locally on set.
        self.active_vfo: str = 'A'       # 'A' or 'B'
        self.memory_mode: bool = False
        self.memory_channel: int = 1

    # -- Connection --

    def connect(self) -> bool:
        if not self.t.connect():
            return False
        # First read gates the rest — if the radio isn't actually responding
        # we bail rather than burn a 1 s timeout on each remaining command.
        if self.get_freq() is None:
            print("[IC7100] No response to connect snapshot — "
                  "radio off or wrong port?", flush=True)
            return True   # transport is open; caller may want to retry
        self.get_mode()
        self.get_ptt()
        self.get_smeter()
        self.get_squelch_status()
        self.poll_settings()
        # DTCS code/polarity excluded from periodic poll (rarely changed,
        # one extra round-trip per cycle); grab once on connect.
        self.get_dtcs_code()
        return True

    def disconnect(self) -> None:
        self.t.disconnect()

    @property
    def connected(self) -> bool:
        return self.t.connected

    # -- Helpers --

    def _txn(self, cmd: int, subcmd: Optional[int] = None,
             data: bytes = b'', timeout: Optional[float] = None):
        return self.t.transact(self.t.build_frame(cmd, subcmd, data), timeout)

    def _set_ok(self, cmd: int, subcmd: Optional[int] = None,
                data: bytes = b'') -> bool:
        resp = self._txn(cmd, subcmd, data)
        return resp is not None and resp[0:1] == OK

    # -- Frequency / mode --

    def get_freq(self) -> Optional[float]:
        """Read VFO frequency. Returns MHz, or None on failure."""
        resp = self._txn(0x03)
        if resp and len(resp) >= 6 and resp[0] == 0x03:
            self.freq_hz = bcd_to_freq(resp[1:6])
            return self.freq_hz / 1e6
        return None

    def set_frequency(self, mhz: float) -> bool:
        hz = round(mhz * 1e6)
        if self._set_ok(0x05, data=freq_to_bcd(hz)):
            self.freq_hz = hz
            return True
        return False

    def get_mode(self) -> Optional[str]:
        resp = self._txn(0x04)
        if resp and len(resp) >= 2 and resp[0] == 0x04:
            self.mode = MODES.get(resp[1], f'?{resp[1]:02x}')
            if len(resp) >= 3:
                self.filter_idx = resp[2]
            return self.mode
        return None

    def set_mode(self, mode: str, filter_idx: int = 1) -> bool:
        m = MODE_BY_NAME.get(mode.upper())
        if m is None:
            return False
        if self._set_ok(0x06, data=bytes([m, filter_idx])):
            self.mode = mode.upper()
            self.filter_idx = filter_idx
            return True
        return False

    def set_filter(self, idx: int) -> bool:
        """Filter selection uses 0x06 with mode + filter (1/2/3)."""
        idx = max(1, min(3, int(idx)))
        m = MODE_BY_NAME.get(self.mode.upper())
        if m is None:
            return False
        if self._set_ok(0x06, data=bytes([m, idx])):
            self.filter_idx = idx
            return True
        return False

    # -- PTT --

    def set_ptt(self, on: bool) -> bool:
        """Key/unkey the transmitter.

        Auto-engages DATA mode around the TX cycle so the USB codec is the
        modulation source (per the operator's MOD INPUT config: DATA OFF
        MOD=MIC, DATA MOD=USB), then restores on release so the manual hand
        mic works again. In SPLIT mode the radio's TX side is the inactive
        VFO — data mode is a per-VFO/mode attribute so we have to set it on
        BOTH sides before keying and undo on BOTH after, else the swap-on-PTT
        exposes the still-non-data VFO and we end up with carrier + MIC. Only
        undo if WE engaged it.
        """
        if on and not self.data_mode:
            self.set_data_mode(True)
            if self.data_mode:
                self._we_set_data_mode = True
                if self.split and self.swap_vfo():
                    self.set_data_mode(True)
                    self.swap_vfo()
        ok = self._set_ok(0x1c, subcmd=0x00,
                          data=bytes([0x01 if on else 0x00]))
        if ok:
            self._set_transmitting(on)
        if not on and self._we_set_data_mode:
            self.set_data_mode(False)
            if self.split and self.swap_vfo():
                self.set_data_mode(False)
                self.swap_vfo()
            self._we_set_data_mode = False
        return ok

    def _set_transmitting(self, on: bool) -> None:
        """Record the TX state. Po/SWR/ALC are only read while transmitting, so
        they keep their last value in between; wipe them whenever the state
        flips so a new key-up never starts by showing the previous transmit."""
        if bool(on) != self.transmitting:
            self.po = self.swr = self.alc = 0
        self.transmitting = bool(on)

    def get_ptt(self) -> Optional[bool]:
        resp = self._txn(0x1c, subcmd=0x00)
        if resp and len(resp) >= 3 and resp[0] == 0x1c and resp[1] == 0x00:
            self._set_transmitting(resp[2])
            return self.transmitting
        return None

    # -- Meters --

    def _read_meter(self, subcmd: int) -> Optional[int]:
        resp = self._txn(0x15, subcmd=subcmd)
        if resp and len(resp) >= 4 and resp[0] == 0x15 and resp[1] == subcmd:
            return bcd3_to_value(resp[2:4])
        return None

    def get_smeter(self) -> Optional[int]:
        v = self._read_meter(0x02)
        if v is not None:
            self.smeter = v
        return v

    def get_po(self) -> Optional[int]:
        v = self._read_meter(0x11)
        if v is not None:
            self.po = v
        return v

    def get_swr(self) -> Optional[int]:
        v = self._read_meter(0x12)
        if v is not None:
            self.swr = v
        return v

    def get_alc(self) -> Optional[int]:
        v = self._read_meter(0x13)
        if v is not None:
            self.alc = v
        return v

    # Squelch condition — 0x15 0x01 (0=closed/muted, 1=open/audio passing).
    def get_squelch_status(self) -> Optional[bool]:
        resp = self._txn(0x15, subcmd=0x01)
        if resp and len(resp) >= 3 and resp[0] == 0x15 and resp[1] == 0x01:
            self.squelch_open = bool(resp[2])
            return self.squelch_open
        return None

    # -- CTCSS --

    def get_ctcss(self):
        """Read full CTCSS state. Returns (tone_hz, tsql_hz, tone_on, tsql_on)."""
        # The radio answers with three BCD bytes (00 hh ll = tenths of Hz),
        # e.g. 127.3 Hz -> 1B 00 00 12 73.
        r = self._txn(0x1b, subcmd=0x00)            # TX tone freq
        if r and len(r) >= 5 and r[0] == 0x1b and r[1] == 0x00:
            self.ctcss_tx_hz = bcd_to_tone(r[3:5])
        r = self._txn(0x1b, subcmd=0x01)            # RX tone squelch freq
        if r and len(r) >= 5 and r[0] == 0x1b and r[1] == 0x01:
            self.ctcss_rx_hz = bcd_to_tone(r[3:5])
        r = self._txn(0x16, subcmd=0x42)            # TONE (repeater tone) on/off
        if r and len(r) >= 3 and r[0] == 0x16 and r[1] == 0x42:
            self.tone_on = bool(r[2])
        r = self._txn(0x16, subcmd=0x43)            # TSQL (tone squelch) on/off
        if r and len(r) >= 3 and r[0] == 0x16 and r[1] == 0x43:
            self.tsql_on = bool(r[2])
        return (self.ctcss_tx_hz, self.ctcss_rx_hz,
                self.tone_on, self.tsql_on)

    def set_ctcss(self, tx_hz: Optional[float] = None,
                  rx_hz: Optional[float] = None,
                  tone_on: Optional[bool] = None,
                  tsql_on: Optional[bool] = None,
                  tx_on: Optional[bool] = None,
                  rx_on: Optional[bool] = None) -> bool:
        """tx_hz = repeater tone (1B 00), rx_hz = tone-squelch tone (1B 01),
        tone_on = TONE (16 42), tsql_on = TSQL (16 43).

        tx_on / rx_on are the old names for tone_on / tsql_on, kept so
        existing callers keep working."""
        if tone_on is None:
            tone_on = tx_on
        if tsql_on is None:
            tsql_on = rx_on
        ok = True
        if tx_hz is not None:
            if self._set_ok(0x1b, subcmd=0x00, data=tone_to_bcd3(tx_hz)):
                self.ctcss_tx_hz = tx_hz
            else:
                ok = False
        if rx_hz is not None:
            if self._set_ok(0x1b, subcmd=0x01, data=tone_to_bcd3(rx_hz)):
                self.ctcss_rx_hz = rx_hz
            else:
                ok = False
        if tone_on is not None:
            if self._set_ok(0x16, subcmd=0x42,
                            data=bytes([0x01 if tone_on else 0x00])):
                self.tone_on = tone_on
            else:
                ok = False
        if tsql_on is not None:
            if self._set_ok(0x16, subcmd=0x43,
                            data=bytes([0x01 if tsql_on else 0x00])):
                self.tsql_on = tsql_on
            else:
                ok = False
        return ok

    # Old attribute names (the 16 42 / 16 43 meanings were swapped under them).
    @property
    def ctcss_tx_on(self) -> bool:
        return self.tone_on

    @property
    def ctcss_rx_on(self) -> bool:
        return self.tsql_on

    # -- DTCS --

    def set_dtcs_on(self, on: bool) -> bool:
        """DTCS enable — 0x16 0x4A. Gates RX (and encodes TX) with a digital
        code instead of a CTCSS tone — the FM "digital squelch"."""
        if self._set_ok(0x16, subcmd=0x4a,
                        data=bytes([0x01 if on else 0x00])):
            self.dtcs_on = on
            return True
        return False

    def get_dtcs_on(self) -> Optional[bool]:
        resp = self._txn(0x16, subcmd=0x4a)
        if resp and len(resp) >= 3 and resp[0] == 0x16 and resp[1] == 0x4a:
            self.dtcs_on = bool(resp[2])
            return self.dtcs_on
        return None

    def set_dtcs_code(self, code: int, polarity: int = 0) -> bool:
        """DTCS code + polarity — 0x1B 0x07."""
        if self._set_ok(0x1b, subcmd=0x07,
                        data=_dtcs_to_bytes(code, polarity)):
            self.dtcs_code = max(0, min(999, int(code)))
            self.dtcs_polarity = max(0, min(3, int(polarity)))
            return True
        return False

    def get_dtcs_code(self):
        resp = self._txn(0x1b, subcmd=0x07)
        if resp and len(resp) >= 5 and resp[0] == 0x1b and resp[1] == 0x07:
            self.dtcs_code, self.dtcs_polarity = _bytes_to_dtcs(resp[2:5])
            return (self.dtcs_code, self.dtcs_polarity)
        return None

    # -- Split / RIT / XIT --

    # Split VFO — 0x0F (0x00=simplex, 0x01=split).
    def get_split(self) -> Optional[bool]:
        resp = self._txn(0x0f)
        if resp and len(resp) >= 2 and resp[0] == 0x0f:
            self.split = bool(resp[1])
            return self.split
        return None

    def set_split(self, on: bool) -> bool:
        if self._set_ok(0x0f, data=bytes([0x01 if on else 0x00])):
            self.split = on
            return True
        return False

    # RIT offset — 0x21 0x00 (signed BCD, 3 bytes). Range +/- 9.999 kHz.
    def get_rit_offset(self) -> Optional[int]:
        resp = self._txn(0x21, subcmd=0x00)
        if resp and len(resp) >= 5 and resp[0] == 0x21 and resp[1] == 0x00:
            self.rit_hz = bcd_to_rit_offset(resp[2:5])
            return self.rit_hz
        return None

    def set_rit_offset(self, hz: int) -> bool:
        hz = max(-9999, min(9999, int(hz)))
        if self._set_ok(0x21, subcmd=0x00, data=rit_offset_to_bcd(hz)):
            self.rit_hz = hz
            return True
        return False

    def get_rit_on(self) -> Optional[bool]:
        resp = self._txn(0x21, subcmd=0x01)
        if resp and len(resp) >= 3 and resp[0] == 0x21 and resp[1] == 0x01:
            self.rit_on = bool(resp[2])
            return self.rit_on
        return None

    def set_rit_on(self, on: bool) -> bool:
        if self._set_ok(0x21, subcmd=0x01,
                        data=bytes([0x01 if on else 0x00])):
            self.rit_on = on
            return True
        return False

    def get_xit_on(self) -> Optional[bool]:
        resp = self._txn(0x21, subcmd=0x02)
        if resp and len(resp) >= 3 and resp[0] == 0x21 and resp[1] == 0x02:
            self.xit_on = bool(resp[2])
            return self.xit_on
        return None

    def set_xit_on(self, on: bool) -> bool:
        if self._set_ok(0x21, subcmd=0x02,
                        data=bytes([0x01 if on else 0x00])):
            self.xit_on = on
            return True
        return False

    # -- 0x16 flags + 0x14 levels --

    def _read_flag(self, subcmd: int) -> Optional[bool]:
        resp = self._txn(0x16, subcmd=subcmd)
        if resp and len(resp) >= 3 and resp[0] == 0x16 and resp[1] == subcmd:
            return bool(resp[2])
        return None

    def _read_level_pct(self, subcmd: int) -> Optional[int]:
        resp = self._txn(0x14, subcmd=subcmd)
        if resp and len(resp) >= 4 and resp[0] == 0x14 and resp[1] == subcmd:
            raw = bcd3_to_value(resp[2:4])
            return max(0, min(100, round(raw * 100 / 255)))
        return None

    def _set_level_pct(self, subcmd: int, pct: int) -> bool:
        pct = max(0, min(100, int(pct)))
        scaled = round(pct * 255 / 100)
        return self._set_ok(0x14, subcmd=subcmd, data=pct_to_bcd3(scaled))

    # AGC — 0x16 0x12 (0x01 fast, 0x02 mid, 0x03 slow).
    def get_agc(self) -> Optional[str]:
        resp = self._txn(0x16, subcmd=0x12)
        if resp and len(resp) >= 3 and resp[0] == 0x16 and resp[1] == 0x12:
            self.agc = _AGC_RMAP.get(resp[2], self.agc)
            return self.agc
        return None

    def set_agc(self, mode: str) -> bool:
        m = mode.lower()
        if m not in _AGC_MAP:
            return False
        if self._set_ok(0x16, subcmd=0x12, data=bytes([_AGC_MAP[m]])):
            self.agc = m
            return True
        return False

    # Noise Blanker — on/off: 0x16 0x22 ; level: 0x14 0x12.
    def get_nb(self):
        on = self._read_flag(0x22)
        if on is not None:
            self.nb_on = on
        lvl = self._read_level_pct(0x12)
        if lvl is not None:
            self.nb_level = lvl
        return (self.nb_on, self.nb_level)

    def set_nb_on(self, on: bool) -> bool:
        if self._set_ok(0x16, subcmd=0x22,
                        data=bytes([0x01 if on else 0x00])):
            self.nb_on = on
            return True
        return False

    def set_nb_level(self, pct: int) -> bool:
        if self._set_level_pct(0x12, pct):
            self.nb_level = max(0, min(100, int(pct)))
            return True
        return False

    # Noise Reduction — on/off: 0x16 0x40 ; level: 0x14 0x06.
    def get_nr(self):
        on = self._read_flag(0x40)
        if on is not None:
            self.nr_on = on
        lvl = self._read_level_pct(0x06)
        if lvl is not None:
            self.nr_level = lvl
        return (self.nr_on, self.nr_level)

    def set_nr_on(self, on: bool) -> bool:
        if self._set_ok(0x16, subcmd=0x40,
                        data=bytes([0x01 if on else 0x00])):
            self.nr_on = on
            return True
        return False

    def set_nr_level(self, pct: int) -> bool:
        if self._set_level_pct(0x06, pct):
            self.nr_level = max(0, min(100, int(pct)))
            return True
        return False

    # Preamp — 0x16 0x02 (0=off, 1=pre1, 2=pre2).
    def get_preamp(self) -> Optional[int]:
        resp = self._txn(0x16, subcmd=0x02)
        if resp and len(resp) >= 3 and resp[0] == 0x16 and resp[1] == 0x02:
            self.preamp = max(0, min(2, resp[2]))
            return self.preamp
        return None

    def set_preamp(self, stage: int) -> bool:
        stage = max(0, min(2, int(stage)))
        if self._set_ok(0x16, subcmd=0x02, data=bytes([stage])):
            self.preamp = stage
            return True
        return False

    # Attenuator — 0x11. The IC-7100 attenuator is a fixed 12 dB pad (NOT
    # 20 dB like the IC-7300/IC-7610; the value byte is the dB amount in
    # BCD). Earlier guesses of 0x01 and 0x20 both NG'd the radio.
    def get_atten(self) -> Optional[bool]:
        resp = self._txn(0x11)
        if resp and len(resp) >= 2 and resp[0] == 0x11:
            self.atten = resp[1] != 0x00
            return self.atten
        return None

    def set_atten(self, on: bool) -> bool:
        if self._set_ok(0x11, data=bytes([0x12 if on else 0x00])):
            self.atten = on
            return True
        return False

    # IF shift — 0x14 0x07 (0..255, 0x80 = center).
    def get_if_shift(self) -> Optional[int]:
        v = self._read_level_pct(0x07)
        if v is not None:
            self.if_shift = v
        return v

    def set_if_shift(self, pct: int) -> bool:
        if self._set_level_pct(0x07, pct):
            self.if_shift = max(0, min(100, int(pct)))
            return True
        return False

    # Squelch level — 0x14 0x03. FM-relevant; SSB/CW usually run at 0.
    def get_squelch(self) -> Optional[int]:
        v = self._read_level_pct(0x03)
        if v is not None:
            self.squelch = v
        return v

    def set_squelch(self, pct: int) -> bool:
        if self._set_level_pct(0x03, pct):
            self.squelch = max(0, min(100, int(pct)))
            return True
        return False

    # RF output power — 0x14 0x0A.
    def get_rf_power(self) -> Optional[int]:
        v = self._read_level_pct(0x0A)
        if v is not None:
            self.rf_power = v
        return v

    def set_rf_power(self, pct: int) -> bool:
        if self._set_level_pct(0x0A, pct):
            self.rf_power = max(0, min(100, int(pct)))
            return True
        return False

    # AF (volume / AF GAIN knob) — 0x14 0x01. Drives the physical speaker
    # volume; independent of any host-side gain.
    def get_af_level(self) -> Optional[int]:
        v = self._read_level_pct(0x01)
        if v is not None:
            self.af_level = v
        return v

    def set_af_level(self, pct: int) -> bool:
        if self._set_level_pct(0x01, pct):
            self.af_level = max(0, min(100, int(pct)))
            return True
        return False

    # Mic gain — 0x14 0x0B.
    def get_mic_gain(self) -> Optional[int]:
        v = self._read_level_pct(0x0B)
        if v is not None:
            self.mic_gain = v
        return v

    def set_mic_gain(self, pct: int) -> bool:
        if self._set_level_pct(0x0B, pct):
            self.mic_gain = max(0, min(100, int(pct)))
            return True
        return False

    # DATA mode — 0x1A 0x06 [on] [filter].
    # Per IC-7100 manual p20-14:
    #   byte 0: 00 = data mode OFF, 01 = data mode ON
    #   byte 1: 00 when off, else 01=FIL1 / 02=FIL2 / 03=FIL3
    # With the radio's MOD config DATA OFF MOD=MIC and DATA MOD=USB,
    # toggling this picks which audio source feeds the modulator.
    def set_data_mode(self, on: bool, filt: int = 1) -> bool:
        flag = 0x01 if on else 0x00
        filt_byte = max(1, min(3, int(filt))) if on else 0
        if self._set_ok(0x1a, subcmd=0x06, data=bytes([flag, filt_byte])):
            self.data_mode = bool(on)
            if on:
                self.data_mode_filter = filt_byte
            return True
        return False

    def get_data_mode(self) -> Optional[bool]:
        resp = self._txn(0x1a, subcmd=0x06)
        if resp and len(resp) >= 4 and resp[0] == 0x1a and resp[1] == 0x06:
            self.data_mode = bool(resp[2])
            if resp[2]:
                self.data_mode_filter = int(resp[3]) or self.data_mode_filter
            return self.data_mode
        return None

    # -- VFO / memory --
    # Opcodes per the published IC-7100 manual; bench-validation still TODO.

    def select_vfo(self, vfo: str) -> bool:
        """Select VFO A or B (CI-V 0x07 0x00 / 0x01)."""
        v = (vfo or '').strip().upper()
        if v not in ('A', 'B'):
            return False
        sub = 0x00 if v == 'A' else 0x01
        if self._set_ok(0x07, subcmd=sub):
            self.active_vfo = v
            self.memory_mode = False
            return True
        return False

    def swap_vfo(self) -> bool:
        """Exchange VFO A <-> B (CI-V 0x07 0xB0)."""
        if self._set_ok(0x07, subcmd=0xB0):
            self.active_vfo = 'B' if self.active_vfo == 'A' else 'A'
            return True
        return False

    def equalize_vfo(self) -> bool:
        """Copy active VFO to inactive (A=B). CI-V 0x07 0xA0."""
        return self._set_ok(0x07, subcmd=0xA0)

    # NOTE: select_band(Main/Sub) intentionally absent. 0x07 0xD0/0xD1 belong
    # to dual-receiver radios (IC-9100 etc.); the IC-7100 is single-receiver.

    def enter_vfo_mode(self) -> bool:
        """Leave memory mode, return to VFO A."""
        if self._set_ok(0x07, subcmd=0x00):
            self.memory_mode = False
            return True
        return False

    def enter_memory_mode(self) -> bool:
        """Switch to memory mode (CI-V 0x08, no data — selects last channel)."""
        if self._set_ok(0x08):
            self.memory_mode = True
            return True
        return False

    def memory_select(self, ch: int) -> bool:
        """Switch to memory mode and select channel (1..99 typical).
        CI-V 0x08 [hi_bcd] [lo_bcd]."""
        ch = int(ch)
        if ch < 1:
            return False
        if self._set_ok(0x08, data=_ch_to_bcd(ch)):
            self.memory_mode = True
            self.memory_channel = ch
            return True
        return False

    def select_call_channel(self, which: str = '') -> bool:
        """Select a CALL channel by name (144-C1/144-C2/430-C1/430-C2).

        With no argument, picks the right band-1 call channel based on the
        current frequency: <300 MHz -> 144-C1, else 430-C1.

        Earlier code used 0x08 0xA0, which selects a memory bank rather than a
        CALL channel — the IC-7100's CALL channels are memory channels 106-109.
        """
        if not which:
            which = '144-C1' if (self.freq_hz or 0) < 300_000_000 else '430-C1'
        ch = CALL_CHANNELS.get(which)
        if ch is None:
            return False
        if self._set_ok(0x08, data=_ch_to_bcd(ch)):
            self.memory_mode = True
            self.memory_channel = ch
            return True
        return False

    def memory_write(self) -> bool:
        """Write current VFO contents into the selected memory channel
        (CI-V 0x09). Destructive — overwrites the channel."""
        return self._set_ok(0x09)

    def memory_to_vfo(self) -> bool:
        """Copy current memory contents into the VFO (CI-V 0x0A).

        NOTE: earlier code had 0x0A and 0x0B swapped — the IC-7100 manual
        defines 0x0A = Memory copy to VFO, 0x0B = Memory clear.
        """
        return self._set_ok(0x0a)

    def memory_clear(self) -> bool:
        """Clear the currently-selected memory channel (CI-V 0x0B). See
        memory_to_vfo() note — opcodes were swapped in earlier versions."""
        return self._set_ok(0x0b)

    def memory_read(self, ch: int) -> Optional[bytes]:
        """Read raw memory channel contents (CI-V 0x1A 0x00 [hi] [lo]).
        Returns the raw payload for the caller to decode — channel format is
        radio-model-specific."""
        ch = int(ch)
        if ch < 1:
            return None
        resp = self._txn(0x1a, subcmd=0x00, data=_ch_to_bcd(ch))
        if not resp:
            return None
        if len(resp) >= 4 and resp[0] == 0x1a and resp[1] == 0x00:
            return resp[4:]
        return None

    # -- Polling --

    def poll_settings(self, abort: Optional[threading.Event] = None) -> None:
        """Read every front-panel-adjustable setting into the cache.

        Each read sleeps 2 ms after releasing the lock so a user command
        (e.g. PTT) waiting on transact() can preempt — otherwise this thread
        re-grabs the lock instantly and a multi-second settings poll blocks
        the user cmd for the full poll duration.

        If *abort* is set partway through, return immediately. The partial
        cache is fine — the next poll fills it in.
        """
        getters = (self.get_split, self.get_rit_offset, self.get_rit_on,
                   self.get_xit_on, self.get_agc, self.get_nb, self.get_nr,
                   self.get_preamp, self.get_atten, self.get_if_shift,
                   self.get_squelch, self.get_rf_power, self.get_mic_gain,
                   self.get_af_level, self.get_ctcss, self.get_dtcs_on)
        for fn in getters:
            if abort is not None and abort.is_set():
                return
            fn()
            time.sleep(0.002)

    def poll_fast(self, abort: Optional[threading.Event] = None) -> None:
        """The cheap cycle — freq/mode/PTT/S-meter/squelch_status. Suitable
        for a ~5 s loop. Skips remaining reads if *abort* fires."""
        for fn in (self.get_freq, self.get_mode, self.get_ptt,
                   self.get_smeter, self.get_squelch_status):
            if abort is not None and abort.is_set():
                return
            fn()
            time.sleep(0.002)

    def poll_meters_tx(self) -> None:
        """TX meters — call this in a tight loop (~10 Hz) while transmitting
        so a GUI sees SSB drive in near-real-time."""
        self.get_po()
        self.get_swr()
        self.get_alc()

    # -- Settings persistence (JSON, XDG path) --

    def load_settings(self) -> Optional[dict]:
        try:
            with open(self.settings_file) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return None

    def save_settings(self, data: dict) -> bool:
        try:
            d = os.path.dirname(self.settings_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.settings_file, 'w') as f:
                json.dump(data, f)
            return True
        except Exception as e:
            print(f"[IC7100] Failed to save settings: {e}", flush=True)
            return False
