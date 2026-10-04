// ic7100ctl — IC-7100 control panel.
// Adapted from the Radio Gateway ic7100 panel. The original lived inside a
// multi-radio gateway shell; this version is standalone. Endpoint/link/
// interlock/voice-relay chrome was removed (see notes inline). The chase-
// target tuner, pot-knob chase, freq-spinner _userTuning lock, and RIT
// auto-engage logic are preserved verbatim.

// ── Static data ─────────────────────────────────────────────────────────
// USA national calling frequencies (MHz) + conventional mode for each.
// [name, freq_mhz, mode]
var IC_BANDS = [
  ['160m', 1.910,  'LSB'], ['80m', 3.985,  'LSB'], ['40m', 7.285,  'LSB'],
  ['30m', 10.116,  'CW'],  ['20m', 14.285, 'USB'], ['17m', 18.130, 'USB'],
  ['15m', 21.385,  'USB'], ['12m', 24.950, 'USB'], ['10m', 28.400, 'USB'],
  ['6m', 50.125,   'USB'], ['2m', 146.520, 'FM'],  ['70cm', 446.000, 'FM'],
];
var IC_MODES = ['LSB', 'USB', 'AM', 'CW', 'CW-R', 'RTTY', 'RTTY-R', 'FM', 'DV'];

// ── Helpers ─────────────────────────────────────────────────────────────
function icGet(path) {
  return fetch(path, {cache: 'no-store'}).then(function(r){ return r.json(); })
                                         .catch(function(){ return {}; });
}
function icPost(body) {
  return fetch('/ic7100cmd', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(function(r){ return r.json(); })
    .catch(function(e){ return {ok:false, error:String(e)}; });
}
function icFeedback(msg, isErr) {
  var el = document.getElementById('ic-feedback');
  if (!el) return;
  el.textContent = msg;
  el.className = 'ic-val ' + (isErr ? 'err' : 'ok');
  setTimeout(function(){ if (el.textContent === msg) el.textContent = ''; }, 3000);
}
function icCmd(cmd, extra) {
  var body = Object.assign({cmd: cmd}, extra || {});
  return icPost(body).then(function(d){
    if (!d.ok) icFeedback(d.error || 'cmd failed', true);
    pollStatus();
    return d;
  });
}

// ── Memory helpers ─────────────────────────────────────────────────────
var _memMode = false;
function toggleMemoryMode() {
  if (_memMode) {
    icCmd('vfo', {vfo: _activeVfo || 'A'});
  } else {
    icCmd('memory_mode', {on: true});
  }
}
function selectMemoryChannel() {
  var el = document.getElementById('ic-mem-ch');
  var n = parseInt(el.value, 10);
  if (isNaN(n) || n < 1) n = 1;
  if (n > 99) n = 99;
  el.value = n;
  icCmd('memory_select', {channel: n});
}
function writeMemory() {
  var ch = parseInt(document.getElementById('ic-mem-ch').value, 10) || 0;
  if (!window.confirm('Overwrite memory channel ' + ch + ' with current VFO?')) return;
  icCmd('memory_write');
}
function clearMemory() {
  var ch = parseInt(document.getElementById('ic-mem-ch').value, 10) || 0;
  if (!window.confirm('Clear memory channel ' + ch + '?')) return;
  icCmd('memory_clear');
}
var _activeVfo = null;

// ── Band + mode row population ─────────────────────────────────────────
function buildBandRow() {
  var row = document.getElementById('ic-band-row');
  IC_BANDS.forEach(function(b) {
    var btn = document.createElement('button');
    btn.className = 'rb rb-sm';
    btn.textContent = b[0];
    btn.dataset.band = b[0];
    btn.dataset.freq = b[1];
    btn.onclick = function() {
      _freqHz = _clampFreq(b[1] * 1e6);
      _userTuning = true;
      _renderFreq();
      icPost({cmd:'freq', args: String(b[1])}).then(function() {
        icPost({cmd:'mode', mode: b[2]}).then(function() {
          setTimeout(function() { _userTuning = false; }, 250);
        });
      });
    };
    row.appendChild(btn);
  });
}
function buildModeRow() {
  var row = document.getElementById('ic-mode-row');
  IC_MODES.forEach(function(m) {
    var btn = document.createElement('button');
    btn.className = 'rb rb-sm';
    btn.textContent = m;
    btn.dataset.mode = m;
    btn.onclick = function() { icCmd('mode', {mode: m}); };
    row.appendChild(btn);
  });
}

// ── Freq display: digit-row "spin the knob" controller ─────────────────
var IC_FREQ_DIGITS = 10;
var IC_FREQ_MIN_HZ = 30000;        // 0.030 MHz
var IC_FREQ_MAX_HZ = 470000000;    // 470 MHz
var _freqHz = 14200000;
var _activePos = 3;
var _userTuning = false;

function _clampFreq(hz) {
  return Math.max(IC_FREQ_MIN_HZ, Math.min(IC_FREQ_MAX_HZ, Math.round(hz)));
}

function _renderFreq() {
  var el = document.getElementById('ic-freq');
  if (!el) return;
  var hz = Math.max(0, Math.round(_freqHz));
  var s = String(hz).padStart(IC_FREQ_DIGITS, '0');
  var html = '';
  var seenNonZero = false;
  for (var i = 0; i < IC_FREQ_DIGITS; i++) {
    var pos = IC_FREQ_DIGITS - 1 - i;
    var digit = s.charAt(i);
    if (digit !== '0') seenNonZero = true;
    var leading = !seenNonZero && pos > 6;
    var active = (pos === _activePos);
    html += '<span class="ic-fd' + (active ? ' active' : '')
          + (leading ? ' leading' : '') + '" data-pos="' + pos + '">'
          + digit + '</span>';
    if (pos === 6 || pos === 3) html += '<span class="ic-freq-dot">.</span>';
  }
  html += '<span class="ic-freq-unit">MHz</span>';
  el.innerHTML = html;
  var kc = document.getElementById('ic-vfo-knob-cap');
  if (kc) kc.textContent = 'VFO · ' + _fmtStep(Math.pow(10, _activePos));
}

// Single-in-flight tuner: only one freq cmd at a time. When it completes,
// if the user's displayed target has moved, fire another for the new
// target. This self-throttles to whatever rate the radio + serial link
// actually sustain — measured ~1-3 cmd/s on real hardware.
var _freqSendInFlight = false;
var _freqLastSentHz = null;
var _freqSettleMs = 20;

function _kickFreqSend() {
  if (_freqSendInFlight) return;
  if (_freqHz === _freqLastSentHz) return;
  _freqSendInFlight = true;
  var target = _freqHz;
  icPost({cmd:'freq', args: String(target / 1e6)}).then(function(d) {
    if (!d.ok) icFeedback(d.error || 'tune failed', true);
    _freqLastSentHz = target;
  }).catch(function() {
    /* ignore — let poll resync */
  }).finally(function() {
    setTimeout(function() {
      _freqSendInFlight = false;
      if (_freqHz !== _freqLastSentHz) {
        _kickFreqSend();
      } else {
        setTimeout(function() { _userTuning = false; }, 250);
      }
    }, _freqSettleMs);
  });
}

function _stepFreq(delta) {
  var want = _freqHz + delta;
  _freqHz = _clampFreq(want);
  if (_freqHz !== Math.round(want)) {
    icFeedback(_freqHz === IC_FREQ_MIN_HZ ? 'min 0.030 MHz' : 'max 470 MHz', true);
  }
  _userTuning = true;
  _renderFreq();
  _kickFreqSend();
}

function _moveActivePos(delta) {
  _activePos = Math.max(0, Math.min(IC_FREQ_DIGITS - 1, _activePos + delta));
  _renderFreq();
}

function initFreqDisplay() {
  var el = document.getElementById('ic-freq');

  el.addEventListener('click', function(e) {
    var target = e.target.closest('.ic-fd');
    if (target) {
      _activePos = +target.dataset.pos;
      _renderFreq();
      el.focus();
    }
  });

  el.addEventListener('wheel', function(e) {
    e.preventDefault();
    var target = e.target.closest('.ic-fd');
    var pos = target ? +target.dataset.pos : _activePos;
    var dir = e.deltaY < 0 ? +1 : -1;
    _stepFreq(dir * Math.pow(10, pos));
  }, {passive: false});

  el.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowUp')   { e.preventDefault(); _stepFreq( Math.pow(10, _activePos)); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); _stepFreq(-Math.pow(10, _activePos)); }
    else if (e.key === 'ArrowLeft')  { e.preventDefault(); _moveActivePos(+1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); _moveActivePos(-1); }
    else if (e.key === 'PageUp')   { e.preventDefault(); _stepFreq( 10 * Math.pow(10, _activePos)); }
    else if (e.key === 'PageDown') { e.preventDefault(); _stepFreq(-10 * Math.pow(10, _activePos)); }
    else if (/^[0-9]$/.test(e.key)) {
      e.preventDefault();
      var place = Math.pow(10, _activePos);
      var curDigit = Math.floor(_freqHz / place) % 10;
      var delta = (parseInt(e.key, 10) - curDigit) * place;
      _stepFreq(delta);
      if (_activePos > 0) _moveActivePos(-1);
    }
  });
}

function buildStepRow() {
  var row = document.getElementById('ic-tune-steps');
  var steps = [
    [-1000000, '−1M'], [-100000, '−100k'], [-10000, '−10k'], [-1000, '−1k'],
    [-100, '−100'], [-10, '−10'], [-1, '−1'],
    [ 1, '+1'], [ 10, '+10'], [ 100, '+100'], [ 1000, '+1k'],
    [ 10000, '+10k'], [ 100000, '+100k'], [ 1000000, '+1M'],
  ];
  steps.forEach(function(s, i) {
    var b = document.createElement('button');
    b.className = 'rb rb-sm';
    b.textContent = s[1];
    b.title = (s[0] > 0 ? '+' : '') + s[0] + ' Hz';
    b.onclick = function() { _stepFreq(s[0]); document.getElementById('ic-freq').focus(); };
    if (i === 7) b.style.marginLeft = 'var(--s-2)';
    row.appendChild(b);
  });
}

function applyTypedFreq() {
  var inp = document.getElementById('ic-freq-typed');
  var v = inp.value.trim();
  if (!v) return;
  var n = parseFloat(v);
  if (isNaN(n)) { icFeedback('not a number', true); return; }
  var mhz = (n > 3000) ? (n / 1000) : n;
  var want = Math.round(mhz * 1e6);
  _freqHz = _clampFreq(want);
  if (_freqHz !== want) {
    icFeedback('clamped to ' + (_freqHz / 1e6).toFixed(3) + ' MHz (IC-7100 RX range)', true);
  }
  _userTuning = true;
  _renderFreq();
  icPost({cmd:'freq', args: String(_freqHz / 1e6)}).then(function(d) {
    if (!d.ok) icFeedback(d.error || 'tune failed', true);
    setTimeout(function() { _userTuning = false; }, 500);
  });
  inp.value = '';
  inp.blur();
}

function initTypedFreq() {
  var inp = document.getElementById('ic-freq-typed');
  inp.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); applyTypedFreq(); }
  });
}

function setFreqDisplay(mhz) {
  if (_userTuning) return;
  if (!mhz || +mhz <= 0) return;
  _freqHz = _clampFreq((+mhz) * 1e6);
  _renderFreq();
}

// ── RIT digit tuner — same spin pattern, signed ±9999 Hz ───────────────
var _ritHz = 0;
var _ritActivePos = 1;
var _ritDigits = 4;
var _ritDebounce = null;
var _ritUserTuning = false;

function _renderRit() {
  var el = document.getElementById('ic-rit-hz');
  if (!el) return;
  var hz = Math.max(-9999, Math.min(9999, _ritHz | 0));
  var mag = Math.abs(hz);
  var s = String(mag).padStart(_ritDigits, '0');
  var sign = (hz < 0) ? '−' : '+';
  var html = '';
  html += '<span class="ic-fd" data-rit-sign="1" title="Click to toggle sign">' + sign + '</span>';
  var seenNonZero = false;
  for (var i = 0; i < _ritDigits; i++) {
    var pos = _ritDigits - 1 - i;
    var digit = s.charAt(i);
    if (digit !== '0') seenNonZero = true;
    var leading = !seenNonZero && pos > 0;
    var active = (pos === _ritActivePos);
    html += '<span class="ic-fd' + (active ? ' active' : '')
          + (leading ? ' leading' : '') + '" data-rit-pos="' + pos + '">'
          + digit + '</span>';
  }
  el.innerHTML = html;
  var rkc = document.getElementById('ic-rit-knob-cap');
  if (rkc) rkc.textContent = 'RIT · ' + _fmtStep(Math.pow(10, _ritActivePos));
}

function _sendRit() {
  if (_ritDebounce) clearTimeout(_ritDebounce);
  _ritDebounce = setTimeout(function() {
    icPost({cmd:'rit', hz: _ritHz}).then(function(d) {
      if (!d.ok) icFeedback(d.error || 'RIT failed', true);
      setTimeout(function() { _ritUserTuning = false; }, 250);
    });
  }, 50);
}

function _stepRit(delta) {
  _ritHz = Math.max(-9999, Math.min(9999, _ritHz + delta));
  _ritUserTuning = true;
  _renderRit();
  _sendRit();
}

function _setRitHz(hz) {
  _ritHz = Math.max(-9999, Math.min(9999, hz | 0));
  _ritUserTuning = true;
  _renderRit();
  _sendRit();
}

function _toggleRitSign() {
  if (_ritHz === 0) return;
  _ritHz = -_ritHz;
  _ritUserTuning = true;
  _renderRit();
  _sendRit();
}

function _moveRitActivePos(delta) {
  _ritActivePos = Math.max(0, Math.min(_ritDigits - 1, _ritActivePos + delta));
  _renderRit();
}

function initRitDisplay() {
  var el = document.getElementById('ic-rit-hz');
  if (!el) return;

  el.addEventListener('click', function(e) {
    var sign = e.target.closest('[data-rit-sign]');
    if (sign) { _toggleRitSign(); el.focus(); return; }
    var d = e.target.closest('[data-rit-pos]');
    if (d) {
      _ritActivePos = +d.dataset.ritPos;
      _renderRit();
      el.focus();
    }
  });

  el.addEventListener('wheel', function(e) {
    e.preventDefault();
    var d = e.target.closest('[data-rit-pos]');
    var pos = d ? +d.dataset.ritPos : _ritActivePos;
    var dir = e.deltaY < 0 ? +1 : -1;
    _stepRit(dir * Math.pow(10, pos));
  }, {passive: false});

  el.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowUp')   { e.preventDefault(); _stepRit( Math.pow(10, _ritActivePos)); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); _stepRit(-Math.pow(10, _ritActivePos)); }
    else if (e.key === 'ArrowLeft')  { e.preventDefault(); _moveRitActivePos(+1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); _moveRitActivePos(-1); }
    else if (e.key === '+' || e.key === '-') {
      e.preventDefault();
      var want = (e.key === '-') ? -1 : +1;
      if (Math.sign(_ritHz) !== want && _ritHz !== 0) _toggleRitSign();
    }
    else if (e.key === '0' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      _setRitHz(0);
    }
  });
}

function setRitDisplay(hz) {
  if (_ritUserTuning) return;
  _ritHz = (typeof hz === 'number') ? hz : 0;
  _renderRit();
}

// ── Tuning knobs — drag-to-spin VFO / RIT controls ─────────────────────
var KNOB_VFO = {degPerDetent: 7,   accelKnee: 220, accelSpan: 1200, accelMax: 8, accelExp: 1.7};
var KNOB_RIT = {degPerDetent: 7.2, accelKnee: 280, accelSpan: 1700, accelMax: 3, accelExp: 1.6};

function _fmtStep(hz) {
  if (hz >= 1e6) return (hz / 1e6) + ' MHz';
  if (hz >= 1e3) return (hz / 1e3) + ' kHz';
  return hz + ' Hz';
}

function _knobAccelGain(speed, p) {
  if (speed <= p.accelKnee) return 1;
  var g = 1 + Math.pow((speed - p.accelKnee) / p.accelSpan,
                       p.accelExp) * (p.accelMax - 1);
  return Math.min(p.accelMax, g);
}

function makeTuningKnob(opts) {
  var el = opts.el;
  if (!el) return;
  var p = opts.profile;
  var rotation = 0;
  var dragging = false;
  var prevAngle = 0;
  var prevT = 0;
  var emaSpeed = 0;
  var accum = 0;
  var wheelT = 0;

  function centre() {
    var r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
  }
  function angleAt(e, c) {
    return Math.atan2(e.clientY - c.y, e.clientX - c.x) * 180 / Math.PI;
  }
  function spinGlow(gain) {
    el.style.setProperty('--spin',
      Math.min(1, (gain - 1) / (p.accelMax - 1)).toFixed(3));
  }

  el.addEventListener('pointerdown', function(e) {
    dragging = true;
    el.classList.add('spinning');
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    prevAngle = angleAt(e, centre());
    prevT = performance.now();
    emaSpeed = 0; accum = 0;
    e.preventDefault();
  });

  el.addEventListener('pointermove', function(e) {
    if (!dragging) return;
    var ang = angleAt(e, centre());
    var dAng = ang - prevAngle;
    if (dAng > 180) dAng -= 360;
    else if (dAng < -180) dAng += 360;
    prevAngle = ang;

    var now = performance.now();
    var dt = Math.max((now - prevT) / 1000, 0.001);
    prevT = now;

    rotation += dAng;
    el.style.transform = 'rotate(' + rotation.toFixed(2) + 'deg)';

    var speed = Math.abs(dAng) / dt;
    emaSpeed = emaSpeed * 0.6 + speed * 0.4;

    var gain = _knobAccelGain(emaSpeed, p);
    spinGlow(gain);
    accum += (dAng / p.degPerDetent) * gain;
    var whole = Math.trunc(accum);
    if (whole !== 0) {
      accum -= whole;
      opts.onStep(whole * opts.baseStep());
    }
  });

  function release(e) {
    if (!dragging) return;
    dragging = false;
    el.classList.remove('spinning');
    spinGlow(1);
    try { el.releasePointerCapture(e.pointerId); } catch (_) {}
  }
  el.addEventListener('pointerup', release);
  el.addEventListener('pointercancel', release);

  el.addEventListener('wheel', function(e) {
    e.preventDefault();
    var now = performance.now();
    var dt = Math.max((now - wheelT) / 1000, 0.001);
    wheelT = now;
    var dir = e.deltaY < 0 ? 1 : -1;
    emaSpeed = emaSpeed * 0.5 + (p.degPerDetent / dt) * 0.5;
    var gain = _knobAccelGain(emaSpeed, p);
    var mult = Math.max(1, Math.round(gain));
    rotation += dir * p.degPerDetent * mult;
    el.style.transform = 'rotate(' + rotation.toFixed(2) + 'deg)';
    spinGlow(gain);
    opts.onStep(dir * mult * opts.baseStep());
    clearTimeout(el._wheelGlow);
    el._wheelGlow = setTimeout(function() { spinGlow(1); }, 180);
  }, {passive: false});
}

function initTuningKnobs() {
  makeTuningKnob({
    el: document.getElementById('ic-vfo-knob'),
    profile: KNOB_VFO,
    baseStep: function() { return Math.pow(10, _activePos); },
    onStep: function(d) { _stepFreq(d); }});
  makeTuningKnob({
    el: document.getElementById('ic-rit-knob'),
    profile: KNOB_RIT,
    baseStep: function() { return Math.pow(10, _ritActivePos); },
    onStep: function(d) { _stepRit(d); }});
}

// ── Pot knobs — bounded, linear rotary controls ─────
function makePotKnob(opts) {
  var el = opts.el;
  if (!el) return null;
  var min = opts.min, max = opts.max, sweep = opts.sweep || 270;
  var dial = el.closest('.knob-dial');
  var value = Math.max(min, Math.min(max, opts.value != null ? opts.value : min));
  var dragging = false, prevAngle = 0, sendT = null, pending = null;
  var moved = 0, lastTapT = 0;
  var chaseInFlight = false, chaseLastSent = null;

  function centre() {
    var r = el.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
  }
  function angleAt(e, c) {
    return Math.atan2(e.clientY - c.y, e.clientX - c.x) * 180 / Math.PI;
  }
  function render() {
    var frac = (value - min) / (max - min);
    el.style.transform = 'rotate(' + (-sweep / 2 + frac * sweep).toFixed(2) + 'deg)';
    if (dial) dial.style.setProperty('--val', frac.toFixed(4));
    if (opts.onRender) opts.onRender(Math.round(value));
  }
  function _kickChase() {
    if (chaseInFlight) return;
    var t = pending;
    if (t === chaseLastSent) return;
    chaseInFlight = true;
    var ret = opts.onChange(t);
    Promise.resolve(ret).finally(function() {
      chaseLastSent = t;
      chaseInFlight = false;
      if (pending !== chaseLastSent) _kickChase();
    });
  }
  function commit() {
    pending = Math.round(value);
    if (opts.chase) {
      _kickChase();
      return;
    }
    if (sendT) clearTimeout(sendT);
    sendT = setTimeout(function() {
      pending = Math.round(value);
      opts.onChange(pending);
    }, 150);
  }
  function setValue(v) {
    value = Math.max(min, Math.min(max, v));
    render();
    commit();
  }

  el.addEventListener('pointerdown', function(e) {
    dragging = true;
    moved = 0;
    el.classList.add('spinning');
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    prevAngle = angleAt(e, centre());
    e.preventDefault();
  });
  el.addEventListener('pointermove', function(e) {
    if (!dragging) return;
    var ang = angleAt(e, centre());
    var dAng = ang - prevAngle;
    if (dAng > 180) dAng -= 360;
    else if (dAng < -180) dAng += 360;
    prevAngle = ang;
    moved += Math.abs(dAng);
    setValue(value + (dAng / sweep) * (max - min));
  });
  function release(e) {
    if (!dragging) return;
    dragging = false;
    el.classList.remove('spinning');
    try { el.releasePointerCapture(e.pointerId); } catch (_) {}
    if (moved < 3 && opts.resetValue != null) {
      var now = performance.now();
      if (now - lastTapT < 350) { setValue(opts.resetValue); lastTapT = 0; }
      else { lastTapT = now; }
    }
  }
  el.addEventListener('pointerup', release);
  el.addEventListener('pointercancel', release);

  el.addEventListener('wheel', function(e) {
    e.preventDefault();
    var step = opts.wheelStep || Math.max(1, Math.round((max - min) / 50));
    setValue(value + (e.deltaY < 0 ? step : -step));
  }, {passive: false});

  render();
  return {
    set: function(v) {
      if (typeof v !== 'number' || dragging) return;
      v = Math.max(min, Math.min(max, v));
      if (pending !== null) {
        if (Math.abs(v - pending) <= 1) pending = null;
        else return;
      }
      value = v;
      render();
    }
  };
}

var _volKnob = null, _sqlKnob = null, _pwrKnob = null, _micKnob = null, _afKnob = null;
function initPotKnobs() {
  _volKnob = makePotKnob({
    el: document.getElementById('ic-vol-knob'),
    min: 0, max: 100, value: 100, wheelStep: 5, resetValue: 0,
    chase: true,
    onRender: function(v) {
      var c = document.getElementById('ic-vol-knob-cap');
      if (c) c.textContent = 'Vol ' + v + '%';
      var led = document.getElementById('ic-vol-led');
      if (led) led.classList.toggle('led-red', v === 0);
    },
    onChange: function(v) { return icCmd('vol', {value: v}); }});
  _sqlKnob = makePotKnob({
    el: document.getElementById('ic-squelch-knob'),
    min: 0, max: 100, value: 0, wheelStep: 2, resetValue: 20,
    chase: true,
    onRender: function(v) {
      var c = document.getElementById('ic-squelch-knob-cap');
      if (c) c.textContent = 'Sql ' + v + '%';
    },
    onChange: function(v) { return icCmd('squelch', {pct: v}); }});
  _pwrKnob = makePotKnob({
    el: document.getElementById('ic-pwr-knob'),
    min: 0, max: 100, value: 0, wheelStep: 5, resetValue: 0,
    chase: true,
    onRender: function(v) {
      var c = document.getElementById('ic-pwr-knob-cap');
      if (c) c.textContent = 'PWR ' + v + '%';
    },
    onChange: function(v) { return icCmd('power', {pct: v}); }});
  _micKnob = makePotKnob({
    el: document.getElementById('ic-mic-knob'),
    min: 0, max: 100, value: 50, wheelStep: 5,
    chase: true,
    onRender: function(v) {
      var c = document.getElementById('ic-mic-knob-cap');
      if (c) c.textContent = 'MIC ' + v + '%';
    },
    onChange: function(v) { return icCmd('mic_gain', {pct: v}); }});
  _afKnob = makePotKnob({
    el: document.getElementById('ic-af-knob'),
    min: 0, max: 100, value: 50, wheelStep: 5,
    chase: true,
    onRender: function(v) {
      var c = document.getElementById('ic-af-knob-cap');
      if (c) c.textContent = 'AF ' + v + '%';
    },
    onChange: function(v) { return icCmd('af_level', {pct: v}); }});
}

// ── CTCSS ───────────────────────────────────────────────────────────────
function icCtcssApply(which) {
  if (which === 'tx') {
    var hz = parseFloat(document.getElementById('ic-ctcss-tx').value);
    var on = document.getElementById('ic-ctcss-tx-on').checked;
    icCmd('ctcss', {tx_hz: isNaN(hz) ? 0 : hz, tone_on: on});
  } else {
    var hzR = parseFloat(document.getElementById('ic-ctcss-rx').value);
    icCmd('ctcss', {rx_hz: isNaN(hzR) ? 0 : hzR});
  }
}

var IC_CTCSS_TONES = [
  67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
  94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
  131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9,
  186.2, 192.8, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8,
  250.3, 254.1
];
function buildCtcssTones() {
  ['ic-ctcss-tx', 'ic-ctcss-rx'].forEach(function(id) {
    var sel = document.getElementById(id);
    if (!sel) return;
    IC_CTCSS_TONES.forEach(function(hz) {
      var opt = document.createElement('option');
      opt.value = hz.toFixed(1);
      opt.textContent = hz.toFixed(1) + ' Hz';
      sel.appendChild(opt);
    });
  });
}

var IC_DTCS_CODES = [
  23,25,26,31,32,36,43,47,51,53,54,65,71,72,73,74,
  114,115,116,122,125,131,132,134,143,145,152,155,156,162,
  165,172,174,205,212,223,225,226,243,244,245,246,251,252,
  255,261,263,265,266,271,274,306,311,315,325,331,332,343,
  346,351,356,364,365,371,411,412,413,423,431,432,445,446,
  452,454,455,462,464,465,466,503,506,516,523,526,532,546,
  565,606,612,624,627,631,632,654,662,664,703,712,723,731,
  732,734,743,754
];
function buildDtcsCodes() {
  var sel = document.getElementById('ic-dtcs-code');
  if (!sel) return;
  IC_DTCS_CODES.forEach(function(code) {
    var opt = document.createElement('option');
    opt.value = code;
    opt.textContent = String(code).padStart(3, '0');
    sel.appendChild(opt);
  });
}

// ── Raw CI-V console ────────────────────────────────────────────────────
function icCatSend() {
  var inp = document.getElementById('ic-cat-in');
  var hex = inp.value.trim();
  if (!hex) return;
  var log = document.getElementById('ic-cat-log');
  log.textContent += '> ' + hex + '\n';
  icPost({cmd: 'cat', args: hex}).then(function(d){
    log.textContent += '< ' + (d.error || d.response || JSON.stringify(d)) + '\n';
    log.scrollTop = log.scrollHeight;
  });
  inp.value = '';
}

// ── Status polling ─────────────────────────────────────────────────────
function setOfflineFlags(connected, civ, audio) {
  var apply = function(id, ok, text) {
    var dot = document.getElementById('ic-chk-' + id);
    var tx  = document.getElementById('ic-chk-' + id + '-text');
    if (!dot || !tx) return;
    dot.className = 'dot ' + (ok === null ? 'pending' : (ok ? 'ok' : 'err'));
    tx.textContent = text;
    tx.className = 'ic-val ' + (ok ? 'ok' : 'dim');
  };
  apply('civ',   civ,   civ   ? 'connected' : 'waiting (check USB cable + power)');
  apply('audio', audio, audio ? 'streaming' : 'waiting (check USB audio device)');
}

// SWR meter raw (0–255) → VSWR ratio. IC-7100 meter breakpoints.
function swrRatio(raw) {
  var pts = [[0, 1.0], [48, 1.5], [80, 2.0], [120, 3.0], [255, 5.0]];
  for (var i = 1; i < pts.length; i++) {
    if (raw <= pts[i][0]) {
      var a = pts[i - 1], b = pts[i];
      return a[1] + (b[1] - a[1]) * (raw - a[0]) / (b[0] - a[0]);
    }
  }
  return 5.0;
}

// Adaptive status polling — fast while transmitting so the TX meters stay
// live, relaxed on receive.
var _txActive = false, _pollTimer = null;
function _schedulePoll() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(pollStatus, _txActive ? 200 : 300);
}

function pollStatus() {
  icGet('/ic7100status').then(function(s) {
    s = s || {};
    var offline = document.getElementById('ic-offline');
    var panel = document.getElementById('ic-panel');

    // "Connected" semantics: in the standalone server we treat the radio as
    // connected when CI-V is talking. (The gateway version had a `connected`
    // field reflecting whether the IC-7100 endpoint was registered with the
    // gateway link manager — that's gone here.) Fall back: if `connected`
    // is present in the status, honour it; otherwise infer from
    // `serial_connected`.
    var civ   = !!s.serial_connected;
    var audio = !!(s.audio_rx || s.input_active);
    var connected = (typeof s.connected === 'boolean') ? s.connected : civ;

    setOfflineFlags(connected, civ, audio);
    if (!connected) {
      offline.style.display = '';
      panel.style.display = 'none';
      return;
    }
    offline.style.display = 'none';
    panel.style.display = '';

    // Status bar — stripped to CI-V + audio + VFO/MEM. The gateway version
    // also showed endpoint name, mute state, and HF/VHF TX interlock dots;
    // those are gateway-side concepts and don't apply to a single-radio app.
    var civEl = document.getElementById('ic-civ-state');
    if (civEl) {
      civEl.textContent = civ ? 'connected' : 'no serial';
      civEl.className = 'ic-val ' + (civ ? 'ok' : 'err');
    }
    var audEl = document.getElementById('ic-audio-state');
    if (audEl) {
      audEl.textContent = audio ? 'streaming' : 'no audio';
      audEl.className = 'ic-val ' + (audio ? 'ok' : 'err');
    }

    // TX badge + PTT button
    var txOn = !!(s.transmitting || s.ptt_active);
    if (txOn !== _txActive) { _txActive = txOn; _schedulePoll(); }
    var badge = document.getElementById('ic-tx-badge');
    if (badge) badge.classList.toggle('on', txOn);
    var pttBtn = document.getElementById('ic-ptt-btn');
    if (pttBtn) pttBtn.classList.toggle('active', txOn);
    var pwrLed = document.getElementById('ic-pwr-led');
    if (pwrLed) {
      pwrLed.classList.toggle('led-red', txOn);
      pwrLed.classList.toggle('led-pulse', txOn);
    }

    // NOTE: gateway-side TX antenna-port safety interlock (tx_allow_hf /
    // tx_allow_vu / tx_port) is intentionally not surfaced here — a
    // standalone control panel should not pretend to enforce RF safety in
    // software; use a dummy load or a physical antenna switch instead.

    // Frequency + mode + filter
    if (typeof s.freq === 'number') setFreqDisplay(s.freq);
    var modeEl = document.getElementById('ic-mode-cur');
    if (modeEl) modeEl.textContent = s.mode || '—';
    var filtEl = document.getElementById('ic-filter-cur');
    if (filtEl) filtEl.textContent = (s.filter !== undefined) ? String(s.filter) : '—';

    // Highlight active mode button
    Array.prototype.forEach.call(document.querySelectorAll('#ic-mode-row button'), function(btn) {
      btn.classList.toggle('active', btn.dataset.mode === s.mode);
    });

    // S-meter (0–255 -> 0–100%)
    var smRaw = (typeof s.smeter === 'number') ? s.smeter : 0;
    var smPct = Math.max(0, Math.min(100, smRaw * 100 / 255));
    var smF = document.getElementById('ic-smeter-fill');
    if (smF) smF.style.width = smPct.toFixed(0) + '%';
    var smV = document.getElementById('ic-smeter-val');
    if (smV) smV.textContent = smRaw;

    // TX meters
    function _txMeter(fillId, valId, raw, text) {
      var pct = Math.max(0, Math.min(100, raw * 100 / 255));
      var f = document.getElementById(fillId);
      if (f) f.style.clipPath = 'inset(0 ' + (100 - pct).toFixed(1) + '% 0 0)';
      var v = document.getElementById(valId);
      if (v) v.textContent = text;
    }
    var poRaw  = (typeof s.po  === 'number') ? s.po  : 0;
    var alcRaw = (typeof s.alc === 'number') ? s.alc : 0;
    var swrRaw = (typeof s.swr === 'number') ? s.swr : 0;
    _txMeter('ic-po-fill',  'ic-po-val',  poRaw,  Math.round(poRaw * 100 / 255) + '%');
    _txMeter('ic-alc-fill', 'ic-alc-val', alcRaw, Math.round(alcRaw * 100 / 255) + '%');
    _txMeter('ic-swr-fill', 'ic-swr-val', swrRaw,
             txOn ? swrRatio(swrRaw).toFixed(1) + ':1' : '—');

    // CTCSS — only update if user isn't editing the dropdown
    var ctxEls = {
      'ic-ctcss-tx':    s.ctcss_tx_hz,
      'ic-ctcss-rx':    s.ctcss_rx_hz,
    };
    Object.keys(ctxEls).forEach(function(id) {
      var el = document.getElementById(id);
      var v = ctxEls[id];
      if (el && document.activeElement !== el && typeof v === 'number') {
        el.value = v.toFixed(1);
      }
    });
    var txOnBox = document.getElementById('ic-ctcss-tx-on');
    if (txOnBox && document.activeElement !== txOnBox) {
      txOnBox.checked = !!s.tone_on;
    }

    // HF panel sync
    function setCheck(id, val) {
      var el = document.getElementById(id);
      if (el && document.activeElement !== el) el.checked = !!val;
    }
    function setRange(id, valId, v, suffix) {
      var el = document.getElementById(id);
      if (el && document.activeElement !== el && typeof v === 'number') {
        el.value = v;
        var lbl = document.getElementById(valId);
        if (lbl) lbl.textContent = v + (suffix || '');
      }
    }
    function setActiveBtn(rowId, attr, want) {
      Array.prototype.forEach.call(document.querySelectorAll('#' + rowId + ' button'), function(btn) {
        btn.classList.toggle('active', String(btn.dataset[attr]) === String(want));
      });
    }
    setCheck('ic-split-on', s.split);
    setCheck('ic-rit-on',   s.rit_on);
    setCheck('ic-xit-on',   s.xit_on);
    setCheck('ic-nb-on',    s.nb_on);
    setCheck('ic-nr-on',    s.nr_on);
    setCheck('ic-atten-on', s.atten);
    if (typeof s.rit_hz === 'number') setRitDisplay(s.rit_hz);
    setRange('ic-nb-level',  'ic-nb-level-val',  s.nb_level,  '%');
    setRange('ic-nr-level',  'ic-nr-level-val',  s.nr_level,  '%');
    setRange('ic-ifshift',   'ic-ifshift-val',   s.if_shift,  '%');
    // rx_boost_pct was a gateway-side concept (RX audio gain applied
    // before streaming over the gateway link). On a standalone panel the
    // Vol knob mirrors a server-side gain; we fall back to either
    // `rx_boost_pct` (legacy) or `vol` if the server exposes that name.
    if (_volKnob) _volKnob.set(typeof s.vol === 'number' ? s.vol : s.rx_boost_pct);
    if (_sqlKnob) _sqlKnob.set(s.squelch);
    if (_pwrKnob) _pwrKnob.set(s.rf_power);
    if (_micKnob) _micKnob.set(s.mic_gain);
    if (_afKnob && typeof s.af_level === 'number') _afKnob.set(s.af_level);
    var sqlLed = document.getElementById('ic-squelch-led');
    if (sqlLed) sqlLed.classList.toggle('led-green', s.squelch_open !== false);
    setActiveBtn('ic-agc-row',    'agc',    s.agc);
    setActiveBtn('ic-preamp-row', 'preamp', s.preamp);
    setActiveBtn('ic-filter-row', 'filter', s.filter);

    // VFO / Memory state
    if (s.active_vfo === 'A' || s.active_vfo === 'B') _activeVfo = s.active_vfo;
    setActiveBtn('ic-vfo-row', 'vfo', s.active_vfo);
    _memMode = !!s.memory_mode;
    var memBtn = document.getElementById('ic-mem-toggle');
    if (memBtn) memBtn.classList.toggle('active', _memMode);
    var memCh = document.getElementById('ic-mem-ch');
    if (memCh && document.activeElement !== memCh && typeof s.memory_channel === 'number') {
      memCh.value = s.memory_channel;
    }
    var vfoPill = document.getElementById('ic-vfo-pill');
    if (vfoPill) {
      if (_memMode) {
        var n = (typeof s.memory_channel === 'number') ? String(s.memory_channel).padStart(2, '0') : '--';
        vfoPill.textContent = 'MEM ' + n;
      } else {
        vfoPill.textContent = s.active_vfo || '—';
      }
    }

    // FM squelch type + DTCS
    setActiveBtn('ic-sqltype-row', 'sqltype', s.squelch_type || 'noise');
    var dcEl = document.getElementById('ic-dtcs-code');
    if (dcEl && document.activeElement !== dcEl && typeof s.dtcs_code === 'number') {
      dcEl.value = s.dtcs_code;
    }
    var dpEl = document.getElementById('ic-dtcs-pol');
    if (dpEl && document.activeElement !== dpEl && typeof s.dtcs_polarity === 'number') {
      dpEl.value = s.dtcs_polarity;
    }
  });
}

// ── Init ────────────────────────────────────────────────────────────────
function icInit() {
  buildBandRow();
  buildModeRow();
  buildStepRow();
  buildDtcsCodes();
  buildCtcssTones();
  initFreqDisplay();
  initTypedFreq();
  initRitDisplay();
  initTuningKnobs();
  initPotKnobs();
  _renderFreq();
  _renderRit();
  pollStatus();
  _schedulePoll();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', icInit);
} else {
  icInit();
}

// ── WebRTC audio (Phase 2) ──────────────────────────────────────────────
// One peer connection: server sends RX audio (radio's USB codec → Opus),
// browser sends mic (Opus → server → aplay → radio's USB codec).
// PTT remains the existing CI-V button — audio just keeps flowing both
// ways; the radio modulates whatever's in its USB-codec input when
// DATA mode is on (set automatically by the gateway PTT path).
(function () {
  var pc = null;
  var micStream = null;
  var btn = document.getElementById('ic-audio-toggle');
  var rxEl = document.getElementById('ic-audio-rx');
  if (!btn || !rxEl) return;

  function setStatus(text) {
    var el = document.getElementById('ic-audio-state');
    if (el) el.textContent = text;
  }

  async function startAudio() {
    btn.disabled = true;
    btn.textContent = 'Connecting…';
    // Try the mic, but don't abort if denied or unavailable — RX-only is
    // a valid mode (plain-HTTP origins block getUserMedia entirely).
    micStream = null;
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      try {
        micStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: false, noiseSuppression: false,
                   autoGainControl: false, channelCount: 1 }
        });
      } catch (e) {
        // RX-only fallback — keep going.
      }
    }
    pc = new RTCPeerConnection({ iceServers: [] });
    if (micStream) {
      pc.addTransceiver('audio', { direction: 'sendrecv' });
      micStream.getAudioTracks().forEach(function (t) {
        pc.addTrack(t, micStream);
      });
    } else {
      pc.addTransceiver('audio', { direction: 'recvonly' });
    }
    pc.ontrack = function (ev) {
      // First inbound track is the radio's RX audio.
      if (ev.streams && ev.streams[0]) {
        rxEl.srcObject = ev.streams[0];
      } else {
        var ms = new MediaStream();
        ms.addTrack(ev.track);
        rxEl.srcObject = ms;
      }
      rxEl.play().catch(function(){});
    };
    pc.onconnectionstatechange = function () {
      setStatus('audio: ' + pc.connectionState);
      if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
        teardown();
      }
    };

    var offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // Wait briefly for ICE candidates to be gathered (non-trickle).
    await new Promise(function (resolve) {
      if (pc.iceGatheringState === 'complete') return resolve();
      var t = setTimeout(resolve, 800);
      pc.addEventListener('icegatheringstatechange', function () {
        if (pc.iceGatheringState === 'complete') {
          clearTimeout(t);
          resolve();
        }
      });
    });

    var resp;
    try {
      resp = await fetch('/webrtc/offer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sdp: pc.localDescription.sdp,
          type: pc.localDescription.type
        })
      }).then(function (r) { return r.json(); });
    } catch (e) {
      setStatus('audio: offer failed');
      teardown();
      return;
    }
    if (!resp || resp.ok === false || !resp.sdp) {
      setStatus('audio: ' + (resp && resp.error || 'no answer'));
      teardown();
      return;
    }
    await pc.setRemoteDescription({ type: resp.type, sdp: resp.sdp });

    btn.textContent = 'Stop audio';
    btn.disabled = false;
    setStatus('audio: connecting');
  }

  function teardown() {
    if (pc) { try { pc.close(); } catch (e) {} pc = null; }
    if (micStream) {
      micStream.getTracks().forEach(function (t) { t.stop(); });
      micStream = null;
    }
    rxEl.srcObject = null;
    btn.textContent = 'Start audio';
    btn.disabled = false;
    setStatus('audio: off');
  }

  btn.addEventListener('click', function () {
    if (pc) teardown();
    else startAudio();
  });
})();
