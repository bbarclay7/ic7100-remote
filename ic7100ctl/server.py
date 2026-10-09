"""Standalone HTTP server for the IC-7100 remote panel.

Wraps an `IC7100` instance with:

* a background poll thread that walks `radio.poll_fast()` and
  `radio.poll_settings()` on a configurable cadence, mirroring the
  radio's state into an in-memory `state` dict;
* a stdlib `http.server`-based JSON API (no Flask) that the bundled
  static web panel calls into.

The HTTP surface preserves the original radio-gateway URLs and JSON
field names exactly — the same `ic7100.html` page works against this
server unmodified. Gateway-specific fields the standalone server has
no concept of (link endpoint name, TX interlock, audio source flags)
are emitted with safe defaults so the UI degrades gracefully.

Auth: NONE. v0.1 assumes the server is bound to localhost or behind a
trusted reverse proxy / tunnel. Do not expose to the open internet.

Threading model:

* one HTTP worker thread per request (`ThreadingHTTPServer`);
* one background poll thread, tagged via
  `radio.transport.mark_poll_thread()` so its CI-V reads use the
  short poll-timeout;
* user commands from HTTP set `radio.transport.user_cmd_pending`
  before issuing their write to abort any in-progress poll cycle and
  let the user cmd grab the lock immediately.

The IC7100 instance itself does NOT need a real serial port at server
construction time — `connect()` is deferred to `start()`. Tests can
inject a mock transport that quacks like `CIVTransport`.
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Web assets directory (shipped inside the package)
# ---------------------------------------------------------------------------

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_WEB_DIR = os.path.join(_PKG_DIR, 'web')


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class RadioServer:
    """HTTP server + background poll thread for an IC-7100.

    Usage:
        radio = IC7100(CIVTransport('/dev/ttyUSB0'))
        srv   = RadioServer(radio, host='0.0.0.0', port=8080)
        srv.start()         # connects radio + starts threads
        srv.serve_forever() # blocks
    """

    # How often the cheap (freq/mode/PTT/smeter/squelch) cycle runs.
    DEFAULT_FAST_INTERVAL = 0.5

    # The expensive settings group (AGC/NB/NR/preamp/atten/levels/CTCSS/DTCS)
    # is ~16 round-trips so we don't run it every fast tick.
    DEFAULT_SETTINGS_EVERY = 20

    # While transmitting, switch to a tight meter loop so the GUI's TX bar
    # graphs update at ~10 Hz.
    DEFAULT_TX_METER_INTERVAL = 0.1

    def __init__(self, radio, host: str = '127.0.0.1', port: int = 8080,
                 fast_interval: float = DEFAULT_FAST_INTERVAL,
                 settings_every: int = DEFAULT_SETTINGS_EVERY,
                 tx_meter_interval: float = DEFAULT_TX_METER_INTERVAL,
                 web_dir: Optional[str] = None,
                 webrtc_bridge=None):
        self.radio = radio
        self.host = host
        self.port = port
        self.fast_interval = float(fast_interval)
        self.settings_every = int(settings_every)
        self.tx_meter_interval = float(tx_meter_interval)
        self.web_dir = web_dir or _WEB_DIR
        # Optional WebRTC bridge — provides /webrtc/offer endpoint when set.
        self.webrtc_bridge = webrtc_bridge

        # In-memory state dict — what /ic7100/status returns. Refreshed by
        # the poll loop from `radio.<attr>` after each cycle.
        self.state: dict = {}
        self._state_lock = threading.Lock()

        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._connected = False

    # ----- lifecycle -----

    def start(self) -> None:
        """Connect the radio (if not already) and start the poll thread +
        bind the HTTP socket. Does NOT block — call `serve_forever()`."""
        if not getattr(self.radio, 'connected', False):
            try:
                ok = self.radio.connect()
                self._connected = bool(ok)
            except Exception as e:
                print(f"[RadioServer] radio.connect() raised: {e}", flush=True)
                self._connected = False
        else:
            self._connected = True

        self._refresh_state()  # seed initial snapshot

        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name='ic7100-poll', daemon=True)
        self._poll_thread.start()

        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        print(f"[RadioServer] Listening on http://{self.host}:{self.port}",
              flush=True)

    def serve_forever(self) -> None:
        if self._httpd is None:
            raise RuntimeError("start() must be called before serve_forever()")
        try:
            self._httpd.serve_forever()
        finally:
            self.stop()

    def stop(self) -> None:
        self._poll_stop.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None

    # ----- factory for tests/smoke -----

    @classmethod
    def create_with_mock_radio(cls, radio, **kwargs) -> 'RadioServer':
        """Construct a server bound to an already-built mock radio. The
        mock just needs to quack like `IC7100` (same attribute names and
        method signatures used here). No serial port required."""
        return cls(radio, **kwargs)

    # ----- polling -----

    def _poll_loop(self) -> None:
        t = getattr(self.radio, 't', None) or getattr(self.radio, 'transport', None)
        if t is not None and hasattr(t, 'mark_poll_thread'):
            try:
                t.mark_poll_thread()
            except Exception:
                pass

        tick = 0
        while not self._poll_stop.is_set():
            if not getattr(self.radio, 'connected', False):
                # Radio not connected — sleep and try to reconnect every few
                # seconds. Keep emitting the state dict so the UI shows
                # "offline" cleanly.
                self._refresh_state()
                for _ in range(20):
                    if self._poll_stop.is_set():
                        return
                    time.sleep(0.1)
                try:
                    self.radio.connect()
                except Exception:
                    pass
                continue

            try:
                abort = t.user_cmd_pending if t is not None else None
                # Transmitting? Tight meter loop for TX bar graphs.
                if getattr(self.radio, 'transmitting', False):
                    try:
                        self.radio.poll_meters_tx()
                    except Exception as e:
                        print(f"[RadioServer] poll_meters_tx error: {e}",
                              flush=True)
                    # Also keep PTT + squelch + smeter fresh.
                    try:
                        self.radio.get_ptt()
                        self.radio.get_smeter()
                    except Exception:
                        pass
                    self._refresh_state()
                    self._sleep(self.tx_meter_interval)
                    continue

                # Fast cycle.
                try:
                    self.radio.poll_fast(abort=abort)
                except TypeError:
                    # Older signature without abort kwarg
                    self.radio.poll_fast()
                except Exception as e:
                    print(f"[RadioServer] poll_fast error: {e}", flush=True)

                # Settings cycle every Nth tick.
                if self.settings_every > 0 and (tick % self.settings_every) == 0:
                    try:
                        self.radio.poll_settings(abort=abort)
                    except TypeError:
                        self.radio.poll_settings()
                    except Exception as e:
                        print(f"[RadioServer] poll_settings error: {e}",
                              flush=True)

                self._refresh_state()
            except Exception as e:
                print(f"[RadioServer] poll loop error: {e}", flush=True)
                traceback.print_exc()

            tick += 1
            self._sleep(self.fast_interval)

    def _sleep(self, secs: float) -> None:
        # Wake up promptly on stop.
        self._poll_stop.wait(timeout=max(0.0, secs))

    def _refresh_state(self) -> None:
        """Copy radio attributes into the public state dict.

        Field names match what `ic7100.html` reads — see pollStatus() in
        that file. Gateway-specific fields (endpoint_name, tx_allow_*,
        rx_boost_pct, audio_rx, input_active) are emitted with safe
        defaults so the UI degrades cleanly."""
        r = self.radio
        connected = bool(getattr(r, 'connected', False))
        freq_hz = int(getattr(r, 'freq_hz', 0) or 0)

        snap = {
            # Connection / transport state. We have no separate "audio" link
            # here so audio_rx is always None (the panel treats that as
            # "no audio" — fine for standalone CI-V-only use).
            'connected':         connected,
            'serial_connected':  connected,
            'endpoint_name':     'ic7100',
            'audio_rx':          bool(self.webrtc_bridge is not None),
            'audio_enabled':     bool(self.webrtc_bridge is not None),
            'input_active':      bool(self.webrtc_bridge is not None
                                      and self.webrtc_bridge.mic_active()),
            'rx_muted':          False,
            # Frequency / mode
            'freq':              (freq_hz / 1e6) if freq_hz else 0.0,
            'freq_hz':           freq_hz,
            'mode':              getattr(r, 'mode', None),
            'filter':            getattr(r, 'filter_idx', None),
            # PTT + meters
            'transmitting':      bool(getattr(r, 'transmitting', False)),
            'ptt_active':        bool(getattr(r, 'transmitting', False)),
            'smeter':            int(getattr(r, 'smeter', 0) or 0),
            'squelch_open':      bool(getattr(r, 'squelch_open', True)),
            # TX-only meters: they aren't read while receiving, so without this
            # the last transmit's value would sit on screen indefinitely.
            'po':                _tx_only(r, 'po'),
            'swr':               _tx_only(r, 'swr'),
            'alc':               _tx_only(r, 'alc'),
            # CTCSS / DTCS
            'ctcss_tx_hz':       float(getattr(r, 'ctcss_tx_hz', 0.0) or 0.0),
            'ctcss_rx_hz':       float(getattr(r, 'ctcss_rx_hz', 0.0) or 0.0),
            'tone_on':           bool(getattr(r, 'tone_on', False)),   # CI-V 16 42
            'tsql_on':           bool(getattr(r, 'tsql_on', False)),   # CI-V 16 43
            # Old names for the same two flags, kept for existing clients.
            'ctcss_tx_on':       bool(getattr(r, 'tone_on', False)),
            'ctcss_rx_on':       bool(getattr(r, 'tsql_on', False)),
            'dtcs_on':           bool(getattr(r, 'dtcs_on', False)),
            'dtcs_code':         int(getattr(r, 'dtcs_code', 23) or 23),
            'dtcs_polarity':     int(getattr(r, 'dtcs_polarity', 0) or 0),
            # HF / DSP
            'split':             bool(getattr(r, 'split', False)),
            'rit_hz':            int(getattr(r, 'rit_hz', 0) or 0),
            'rit_on':            bool(getattr(r, 'rit_on', False)),
            'xit_on':            bool(getattr(r, 'xit_on', False)),
            'agc':               getattr(r, 'agc', 'mid'),
            'nb_on':             bool(getattr(r, 'nb_on', False)),
            'nb_level':          int(getattr(r, 'nb_level', 0) or 0),
            'nr_on':             bool(getattr(r, 'nr_on', False)),
            'nr_level':          int(getattr(r, 'nr_level', 0) or 0),
            'preamp':            int(getattr(r, 'preamp', 0) or 0),
            'atten':             bool(getattr(r, 'atten', False)),
            'if_shift':          int(getattr(r, 'if_shift', 50) or 50),
            'squelch':           int(getattr(r, 'squelch', 0) or 0),
            'rf_power':          int(getattr(r, 'rf_power', 0) or 0),
            'mic_gain':          int(getattr(r, 'mic_gain', 50) or 50),
            'af_level':          int(getattr(r, 'af_level', 50) or 50),
            'data_mode':         bool(getattr(r, 'data_mode', False)),
            # VFO / memory
            'active_vfo':        getattr(r, 'active_vfo', 'A'),
            'memory_mode':       bool(getattr(r, 'memory_mode', False)),
            'memory_channel':    int(getattr(r, 'memory_channel', 1) or 1),
            # Gateway-only fields the standalone server doesn't track —
            # safe defaults so the UI's interlock dots/sliders render.
            'tx_allow_hf':       True,
            'tx_allow_vu':       True,
            'tx_port':           None,
            'rx_boost_pct':      100,
            'squelch_type':      ('dtcs' if getattr(r, 'dtcs_on', False)
                                  else ('tsql' if getattr(r, 'tsql_on', False)
                                        else 'noise')),
            # Pack a derived settings sub-dict too — handy for clients that
            # want one blob to persist to disk.
            'settings': {
                'rf_power':   int(getattr(r, 'rf_power', 0) or 0),
                'mic_gain':   int(getattr(r, 'mic_gain', 50) or 50),
                'af_level':   int(getattr(r, 'af_level', 50) or 50),
                'squelch':    int(getattr(r, 'squelch', 0) or 0),
                'if_shift':   int(getattr(r, 'if_shift', 50) or 50),
                'agc':        getattr(r, 'agc', 'mid'),
                'preamp':     int(getattr(r, 'preamp', 0) or 0),
                'atten':      bool(getattr(r, 'atten', False)),
                'nb_on':      bool(getattr(r, 'nb_on', False)),
                'nb_level':   int(getattr(r, 'nb_level', 0) or 0),
                'nr_on':      bool(getattr(r, 'nr_on', False)),
                'nr_level':   int(getattr(r, 'nr_level', 0) or 0),
            },
        }
        with self._state_lock:
            self.state = snap

    def snapshot(self) -> dict:
        with self._state_lock:
            # Shallow copy is fine — nested dicts are read-only from caller.
            return dict(self.state)

    # ----- command dispatch -----

    def dispatch(self, payload: dict) -> dict:
        """Execute one panel command. Returns a JSON-safe dict — always
        includes `ok`, plus an `error` string on failure."""
        cmd = (payload.get('cmd') or '').strip()
        r = self.radio
        t = getattr(r, 't', None) or getattr(r, 'transport', None)
        # Wake any in-progress poll cycle so this user cmd takes the lock
        # promptly. The transport itself checks user_cmd_pending between
        # reads inside CIVTransport.
        if t is not None and hasattr(t, 'user_cmd_pending'):
            try:
                t.user_cmd_pending.set()
            except Exception:
                pass

        try:
            try:
                handler = _DISPATCH.get(cmd)
                if handler is None:
                    return {'ok': False, 'error': f'unknown cmd: {cmd}'}
                result = handler(r, payload)
            finally:
                # Refresh state immediately so the UI's next poll reflects
                # the change without waiting a full fast-tick.
                self._refresh_state()
                if t is not None and hasattr(t, 'user_cmd_pending'):
                    try:
                        t.user_cmd_pending.clear()
                    except Exception:
                        pass
            return result
        except Exception as e:
            traceback.print_exc()
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


# ---------------------------------------------------------------------------
# Command dispatch table
#
# Each entry is `cmd -> (radio, payload) -> result_dict`. Keep these tiny —
# they mostly just unpack JSON keys and call the right IC7100 method.
# ---------------------------------------------------------------------------

def _ok(extra: Optional[dict] = None) -> dict:
    out = {'ok': True}
    if extra:
        out.update(extra)
    return out


def _fail(msg: str) -> dict:
    return {'ok': False, 'error': msg}


def _cmd_freq(r, p):
    # UI sends either {hz: 146520000} or, from the band buttons,
    # {args: "146.52"} (MHz as a string). Accept both.
    hz = p.get('hz')
    if hz is None and 'mhz' in p:
        hz = float(p['mhz']) * 1e6
    if hz is None and 'args' in p:
        try:
            hz = float(p['args']) * 1e6
        except (TypeError, ValueError):
            return _fail('freq: hz or args required')
    if hz is None:
        return _fail('freq: hz required')
    ok = r.set_frequency(float(hz) / 1e6)
    return _ok({'freq': float(hz) / 1e6}) if ok else _fail('set_frequency NG')


def _cmd_mode(r, p):
    mode = p.get('mode') or p.get('args')
    if not mode:
        return _fail('mode: mode required')
    filt = p.get('filter')
    if filt is None:
        filt = getattr(r, 'filter_idx', 1) or 1
    ok = r.set_mode(str(mode), int(filt))
    return _ok({'mode': str(mode).upper()}) if ok else _fail('set_mode NG')


def _cmd_filter(r, p):
    idx = p.get('idx', p.get('filter'))
    if idx is None:
        return _fail('filter: idx required')
    ok = r.set_filter(int(idx))
    return _ok({'filter': int(idx)}) if ok else _fail('set_filter NG')


def _tx_only(r, attr: str) -> int:
    return int(getattr(r, attr, 0) or 0) if getattr(r, 'transmitting', False) else 0


def _cmd_ptt(r, p):
    if 'state' in p:
        on = bool(p['state'])
    else:
        on = not bool(getattr(r, 'transmitting', False))
    ok = r.set_ptt(on)
    return _ok({'ptt': on}) if ok else _fail('set_ptt NG')


def _cmd_vfo(r, p):
    v = p.get('vfo')
    if not v:
        return _fail('vfo: vfo required')
    return _ok({'vfo': v.upper()}) if r.select_vfo(v) else _fail('select_vfo NG')


def _cmd_vfo_swap(r, p):
    return _ok() if r.swap_vfo() else _fail('swap_vfo NG')


def _cmd_vfo_equalize(r, p):
    return _ok() if r.equalize_vfo() else _fail('equalize_vfo NG')


def _cmd_memory_mode(r, p):
    on = bool(p.get('on', True))
    if on:
        ok = r.enter_memory_mode()
    else:
        # No standalone "leave memory" — re-select active VFO.
        ok = r.select_vfo(getattr(r, 'active_vfo', 'A'))
    return _ok({'memory_mode': on}) if ok else _fail('memory_mode NG')


def _cmd_memory_select(r, p):
    ch = p.get('channel', p.get('ch'))
    if ch is None:
        return _fail('memory_select: channel required')
    ok = r.memory_select(int(ch))
    return _ok({'memory_channel': int(ch)}) if ok else _fail('memory_select NG')


def _cmd_call_channel(r, p):
    which = p.get('which', '') or ''
    return _ok() if r.select_call_channel(which) else _fail('call_channel NG')


def _cmd_memory_to_vfo(r, p):
    return _ok() if r.memory_to_vfo() else _fail('memory_to_vfo NG')


def _cmd_memory_write(r, p):
    return _ok() if r.memory_write() else _fail('memory_write NG')


def _cmd_memory_clear(r, p):
    return _ok() if r.memory_clear() else _fail('memory_clear NG')


def _do_level(r, p, setter_name: str,
              key_aliases=('level', 'pct', 'value')) -> dict:
    """Generic 0-100 level setter — extracts the numeric value from one of
    the alias keys (`level`, `pct`, `value`, fallback `args`) and calls the
    named radio method."""
    v = None
    for k in key_aliases:
        if k in p:
            v = p[k]
            break
    if v is None and 'args' in p:
        v = p['args']
    if v is None:
        return _fail(f'{setter_name}: level required')
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return _fail(f'{setter_name}: level must be a number')
    ok = getattr(r, setter_name)(v)
    return _ok({setter_name: v}) if ok else _fail(f'{setter_name} NG')


def _cmd_af_level(r, p): return _do_level(r, p, 'set_af_level')
def _cmd_mic_gain(r, p): return _do_level(r, p, 'set_mic_gain')
def _cmd_rf_power(r, p): return _do_level(r, p, 'set_rf_power')
def _cmd_squelch(r, p):  return _do_level(r, p, 'set_squelch')
def _cmd_nb_level(r, p): return _do_level(r, p, 'set_nb_level')
def _cmd_nr_level(r, p): return _do_level(r, p, 'set_nr_level')
def _cmd_if_shift(r, p): return _do_level(r, p, 'set_if_shift',
                                          key_aliases=('value', 'pct', 'level'))


def _cmd_data_mode(r, p):
    on = bool(p.get('on'))
    filt = int(p.get('filter', 1) or 1)
    ok = r.set_data_mode(on, filt)
    return _ok({'data_mode': on}) if ok else _fail('set_data_mode NG')


def _cmd_rit_on(r, p):
    on = bool(p.get('on'))
    return _ok({'rit_on': on}) if r.set_rit_on(on) else _fail('set_rit_on NG')


def _cmd_rit_offset(r, p):
    hz = p.get('hz')
    if hz is None:
        return _fail('rit_offset: hz required')
    return _ok({'rit_hz': int(hz)}) if r.set_rit_offset(int(hz)) else _fail('set_rit_offset NG')


def _cmd_xit_on(r, p):
    on = bool(p.get('on'))
    return _ok({'xit_on': on}) if r.set_xit_on(on) else _fail('set_xit_on NG')


def _cmd_agc(r, p):
    mode = p.get('mode') or p.get('args')
    if not mode:
        return _fail('agc: mode required')
    return _ok({'agc': str(mode).lower()}) if r.set_agc(str(mode)) else _fail('set_agc NG')


def _cmd_nb_on(r, p):
    on = bool(p.get('on'))
    return _ok({'nb_on': on}) if r.set_nb_on(on) else _fail('set_nb_on NG')


def _cmd_nr_on(r, p):
    on = bool(p.get('on'))
    return _ok({'nr_on': on}) if r.set_nr_on(on) else _fail('set_nr_on NG')


def _cmd_preamp(r, p):
    stage = p.get('level', p.get('stage'))
    if stage is None:
        return _fail('preamp: level/stage required')
    return _ok({'preamp': int(stage)}) if r.set_preamp(int(stage)) else _fail('set_preamp NG')


def _cmd_atten(r, p):
    on = bool(p.get('on'))
    return _ok({'atten': on}) if r.set_atten(on) else _fail('set_atten NG')


def _cmd_ctcss(r, p):
    # Combined-fields panel command — set any/all of tx_hz (repeater tone),
    # rx_hz (tone-squelch tone), tone_on (TONE), tsql_on (TSQL). tx_on and
    # rx_on are accepted as the old names for tone_on / tsql_on.
    if 'hz' in p and 'tx_hz' not in p:
        p['tx_hz'] = p['hz']  # tolerate spec wording
    ok = r.set_ctcss(tx_hz=p.get('tx_hz'), rx_hz=p.get('rx_hz'),
                     tone_on=p.get('tone_on', p.get('tx_on')),
                     tsql_on=p.get('tsql_on', p.get('rx_on')))
    return _ok() if ok else _fail('set_ctcss NG')


def _cmd_dtcs_on(r, p):
    on = bool(p.get('on'))
    return _ok({'dtcs_on': on}) if r.set_dtcs_on(on) else _fail('set_dtcs_on NG')


def _cmd_dtcs_code(r, p):
    code = p.get('code')
    if code is None:
        return _fail('dtcs_code: code required')
    pol = int(p.get('polarity', getattr(r, 'dtcs_polarity', 0)) or 0)
    ok = r.set_dtcs_code(int(code), pol)
    return _ok({'dtcs_code': int(code), 'dtcs_polarity': pol}) if ok else _fail('set_dtcs_code NG')


def _cmd_split(r, p):
    on = bool(p.get('on'))
    return _ok({'split': on}) if r.set_split(on) else _fail('set_split NG')


def _cmd_raw(r, p):
    """Developer CI-V console — send a raw frame, return hex of the response.

    Body: {hex: "FE FE 88 E0 03 FD"} or {hex: "FEFE8800E003FD"}.
    Spaces, colons, dashes tolerated. Returns {ok, hex} or {ok:false, error}.
    """
    hexstr = (p.get('hex') or '').replace(' ', '').replace(':', '').replace('-', '')
    if not hexstr:
        return _fail('raw: hex required')
    try:
        frame = bytes.fromhex(hexstr)
    except ValueError as e:
        return _fail(f'raw: bad hex ({e})')
    t = getattr(r, 't', None) or getattr(r, 'transport', None)
    if t is None:
        return _fail('raw: no transport')
    resp = t.send_raw(frame) if hasattr(t, 'send_raw') else t.transact(frame)
    return _ok({'hex': resp.hex() if resp else '',
                'len': len(resp) if resp else 0})


# UI also sends panel-style aliases (squelch_type, dtcs, rit, xit, nb, nr,
# power) — map them to the canonical commands above.
def _cmd_squelch_type(r, p):
    t = (p.get('type') or '').lower()
    if t == 'noise':
        r.set_ctcss(tsql_on=False)
        r.set_dtcs_on(False)
        return _ok({'squelch_type': 'noise'})
    if t == 'tsql':
        r.set_dtcs_on(False)
        r.set_ctcss(tsql_on=True)
        return _ok({'squelch_type': 'tsql'})
    if t == 'dtcs':
        r.set_ctcss(tsql_on=False)
        r.set_dtcs_on(True)
        return _ok({'squelch_type': 'dtcs'})
    return _fail('squelch_type: type must be noise|tsql|dtcs')


def _cmd_dtcs(r, p):
    # UI: {code: 23} or {polarity: 0} or {on: true}
    if 'on' in p:
        return _cmd_dtcs_on(r, p)
    if 'code' in p or 'polarity' in p:
        code = p.get('code', getattr(r, 'dtcs_code', 23))
        pol = p.get('polarity', getattr(r, 'dtcs_polarity', 0))
        ok = r.set_dtcs_code(int(code), int(pol))
        return _ok({'dtcs_code': int(code), 'dtcs_polarity': int(pol)}) if ok \
            else _fail('set_dtcs_code NG')
    return _fail('dtcs: on|code|polarity required')


def _cmd_rit(r, p):
    if 'on' in p:
        return _cmd_rit_on(r, p)
    if 'hz' in p:
        return _cmd_rit_offset(r, p)
    return _fail('rit: on|hz required')


def _cmd_xit(r, p):
    if 'on' in p:
        return _cmd_xit_on(r, p)
    return _fail('xit: on required')


def _cmd_nb(r, p):
    if 'on' in p:
        return _cmd_nb_on(r, p)
    if 'level' in p:
        return _cmd_nb_level(r, p)
    return _fail('nb: on|level required')


def _cmd_nr(r, p):
    if 'on' in p:
        return _cmd_nr_on(r, p)
    if 'level' in p:
        return _cmd_nr_level(r, p)
    return _fail('nr: on|level required')


def _cmd_power(r, p):
    # alias for rf_power
    return _cmd_rf_power(r, p)


def _cmd_vol(r, p):
    # UI volume slider — gateway-side audio boost. We have no audio source in
    # the standalone server, so accept the value and noop. Returning ok keeps
    # the slider UX responsive even though we don't actually boost.
    try:
        pct = max(0, min(500, int(p.get('value', 100))))
    except (ValueError, TypeError):
        return _fail('vol must be 0-500')
    return _ok({'audio_boost': pct})


def _cmd_mute(r, p):
    # No audio pipeline here — return ok with mute=false so UI is consistent.
    return _ok({'muted': False})


def _cmd_tx_interlock(r, p):
    # Standalone server has no interlock concept — accept + echo.
    out = {}
    for k in ('hf', 'vu'):
        if k in p:
            out[f'tx_allow_{k}'] = bool(p[k])
    return _ok(out)


def _cmd_status(r, p):
    # UI sometimes pings cmd=status; nothing to do (poll loop refreshes).
    return _ok()


# Master dispatch table. Order = readability; lookup is dict so it's O(1).
_DISPATCH: dict = {
    'freq':           _cmd_freq,
    'mode':           _cmd_mode,
    'filter':         _cmd_filter,
    'ptt':            _cmd_ptt,
    'vfo':            _cmd_vfo,
    'vfo_swap':       _cmd_vfo_swap,
    'vfo_equalize':   _cmd_vfo_equalize,
    'memory_mode':    _cmd_memory_mode,
    'memory_select':  _cmd_memory_select,
    'call_channel':   _cmd_call_channel,
    'memory_to_vfo':  _cmd_memory_to_vfo,
    'memory_write':   _cmd_memory_write,
    'memory_clear':   _cmd_memory_clear,
    'af_level':       _cmd_af_level,
    'mic_gain':       _cmd_mic_gain,
    'rf_power':       _cmd_rf_power,
    'power':          _cmd_power,
    'squelch':        _cmd_squelch,
    'data_mode':      _cmd_data_mode,
    'rit_on':         _cmd_rit_on,
    'rit_offset':     _cmd_rit_offset,
    'rit':            _cmd_rit,
    'xit_on':         _cmd_xit_on,
    'xit':            _cmd_xit,
    'agc':            _cmd_agc,
    'nb_on':          _cmd_nb_on,
    'nb_level':       _cmd_nb_level,
    'nb':             _cmd_nb,
    'nr_on':          _cmd_nr_on,
    'nr_level':       _cmd_nr_level,
    'nr':             _cmd_nr,
    'preamp':         _cmd_preamp,
    'atten':          _cmd_atten,
    'if_shift':       _cmd_if_shift,
    'ctcss':          _cmd_ctcss,
    'dtcs_on':        _cmd_dtcs_on,
    'dtcs_code':      _cmd_dtcs_code,
    'dtcs':           _cmd_dtcs,
    'squelch_type':   _cmd_squelch_type,
    'split':          _cmd_split,
    'raw':            _cmd_raw,
    'vol':            _cmd_vol,
    'mute':           _cmd_mute,
    'tx_interlock':   _cmd_tx_interlock,
    'status':         _cmd_status,
}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _make_handler(server: RadioServer):
    """Build a BaseHTTPRequestHandler subclass bound to *server*. The
    handler class needs a reference to the RadioServer instance — easiest
    is to close over it in a factory."""

    class Handler(BaseHTTPRequestHandler):
        server_version = 'IC7100ctl/0.1'

        # Quieter access log — default impl spams stderr.
        def log_message(self, fmt, *args):
            return

        # ----- helpers -----

        def _send_json(self, obj, status: int = 200) -> None:
            try:
                body = json.dumps(obj).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_file(self, path: str) -> None:
            try:
                with open(path, 'rb') as f:
                    body = f.read()
            except (FileNotFoundError, IsADirectoryError):
                return self._send_json({'ok': False, 'error': 'not found'}, 404)
            ctype, _ = mimetypes.guess_type(path)
            ctype = ctype or 'application/octet-stream'
            try:
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ----- routes -----

        def do_GET(self):
            path = urlparse(self.path).path

            if path in ('/', '/index.html', '/ic7100', '/ic7100/'):
                idx = os.path.join(server.web_dir, 'index.html')
                if not os.path.isfile(idx):
                    idx = os.path.join(server.web_dir, 'ic7100.html')
                if os.path.exists(idx):
                    return self._send_file(idx)
                # Fallback inline page so the server is usable before any
                # static asset is shipped.
                fallback = (b'<!doctype html><meta charset=utf-8>'
                            b'<title>IC-7100 (no panel)</title>'
                            b'<h1>ic7100ctl running</h1>'
                            b'<p>No <code>ic7100.html</code> in '
                            + server.web_dir.encode() + b'</p>'
                            b'<p>Status JSON: '
                            b'<a href="/ic7100/status">/ic7100/status</a></p>')
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(fallback)))
                    self.end_headers()
                    self.wfile.write(fallback)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            # Static assets under /web/<filename>. Refuse path traversal.
            if path.startswith('/web/'):
                rel = path[len('/web/'):]
                if '..' in rel.split('/'):
                    return self._send_json({'ok': False, 'error': 'forbidden'}, 403)
                return self._send_file(os.path.join(server.web_dir, rel))

            # Status — UI uses /ic7100status (no slash); we also serve the
            # spec'd /ic7100/status for API consumers.
            if path in ('/ic7100/status', '/ic7100status'):
                return self._send_json(server.snapshot())

            if path.startswith('/ic7100/'):
                return self._send_json(
                    {'ok': False, 'error': f'unknown route: {path}'}, 404)

            return self._send_json(
                {'ok': False, 'error': f'unknown route: {path}'}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path in ('/ic7100cmd', '/ic7100/cmd'):
                try:
                    length = int(self.headers.get('Content-Length', '0') or '0')
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b''
                try:
                    payload = json.loads(raw.decode('utf-8')) if raw else {}
                    if not isinstance(payload, dict):
                        return self._send_json(
                            {'ok': False, 'error': 'body must be a JSON object'}, 400)
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    return self._send_json(
                        {'ok': False, 'error': f'bad json: {e}'}, 400)
                return self._send_json(server.dispatch(payload))

            if path == '/webrtc/offer':
                if server.webrtc_bridge is None:
                    return self._send_json(
                        {'ok': False, 'error': 'audio bridge not enabled '
                         '(install with `pip install ic7100ctl[audio]` and '
                         'run with --audio)'}, 503)
                try:
                    length = int(self.headers.get('Content-Length', '0') or '0')
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b''
                try:
                    payload = json.loads(raw.decode('utf-8')) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    return self._send_json(
                        {'ok': False, 'error': f'bad json: {e}'}, 400)
                sdp = payload.get('sdp')
                sdp_type = payload.get('type', 'offer')
                if not sdp:
                    return self._send_json(
                        {'ok': False, 'error': 'sdp required'}, 400)
                try:
                    answer = server.webrtc_bridge.handle_offer(sdp, sdp_type)
                    return self._send_json(answer)
                except Exception as e:
                    return self._send_json(
                        {'ok': False, 'error': f'webrtc: {e}'}, 500)

            return self._send_json(
                {'ok': False, 'error': f'unknown route: {path}'}, 404)

    return Handler


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='IC-7100 remote panel HTTP server.')
    ap.add_argument('--port-serial', default=os.environ.get('IC7100_PORT', '/dev/ttyUSB0'),
                    help='Serial port for CI-V (default: /dev/ttyUSB0)')
    ap.add_argument('--baud', type=int, default=19200)
    ap.add_argument('--civ-addr', type=lambda x: int(x, 0), default=0x88,
                    help='CI-V radio address (default 0x88 for IC-7100)')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--http-port', type=int, default=8080)
    args = ap.parse_args(argv)

    from .civ import CIVTransport
    from .radio import IC7100

    transport = CIVTransport(args.port_serial, baud=args.baud,
                             civ_addr=args.civ_addr)
    radio = IC7100(transport)
    srv = RadioServer(radio, host=args.host, port=args.http_port)
    srv.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n[RadioServer] shutting down', flush=True)
    finally:
        srv.stop()
        try:
            radio.disconnect()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
