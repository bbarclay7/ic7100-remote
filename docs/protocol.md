# Protocol

This document covers the CI-V protocol as `ic7100ctl` uses it against the
Icom IC-7100, including the opcode table and the corrections we've made
versus other published Python implementations.

## CI-V framing primer

Every CI-V message — request or response — has the same shape:

```
FE FE  <radio_addr>  <ctrl_addr>  <cmd>  [subcmd]  [data...]  FD
\_____/ \__________/ \__________/ \____/ \________________/  \__/
preamble  to/from     to/from      cmd     payload          terminator
```

- **Preamble**: two `0xFE` bytes. Synchronization.
- **Radio address**: one byte. IC-7100 factory default is `0x88`. In a
  request this is the destination; in a response it's the source.
- **Controller address**: one byte. Conventionally `0xE0`. In a request
  this is the source ("us"); in a response it's the destination.
- **Command**: one byte. The CI-V opcode.
- **Subcommand**: optional byte. Used by most commands in the 0x14 / 0x16
  / 0x1A / 0x1B / 0x21 groups to namespace.
- **Data**: zero or more bytes. Frequencies and levels are usually BCD
  (binary-coded decimal); flags are plain `0x00`/`0x01` bytes.
- **Terminator**: one `0xFD` byte.

Two distinguished response bodies act as ACK/NAK:

- `FB` (`0xFB`) — OK. The command was accepted.
- `FA` (`0xFA`) — NG. The radio rejected the command (out of range, wrong
  mode, TX inhibited, etc.).

For read commands the response body echoes the request's `cmd`/`subcmd`
followed by the requested data — there is no separate OK byte.

Reference: `CIVTransport.build_frame()` and `transact()` in
[`../ic7100ctl/civ.py`](../ic7100ctl/civ.py).

## Implemented commands

All entries below are bench-verified against an IC-7100 unless flagged.
Manual page references (where given) are to the IC-7100 Full Manual.

### Frequency and mode

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `get_freq` | `0x03` | — | Response is 5-byte BCD frequency, LSB first, 2 digits/byte. |
| `set_frequency` | `0x05` | — | 5-byte BCD, same encoding as `0x03` reply. |
| `get_mode` | `0x04` | — | Response is `[mode_byte, filter_byte]`. |
| `set_mode` | `0x06` | — | Data `[mode_byte, filter_byte]`. Mode bytes: 0x00 LSB, 0x01 USB, 0x02 AM, 0x03 CW, 0x04 RTTY, 0x05 FM, 0x06 WFM, 0x07 CW-R, 0x08 RTTY-R, 0x17 DV. |
| `set_filter` | `0x06` | — | Same opcode as `set_mode`; preserves cached mode, varies filter byte (1/2/3). |

### PTT

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `set_ptt` | `0x1C 0x00` | — | Data `[0x01]` to key, `[0x00]` to unkey. Wrapped with split-mode-aware DATA-mode dance — see [DATA mode](#data-mode). |
| `get_ptt` | `0x1C 0x00` | — | Response body `[0x1C, 0x00, flag]`. |

### Meters

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `get_smeter` | `0x15 0x02` | — | 2-byte BCD, 0..255. |
| `get_squelch_status` | `0x15 0x01` | — | 1 byte: 0 closed, 1 open. |
| `get_po` | `0x15 0x11` | — | TX power output meter, 2-byte BCD. |
| `get_swr` | `0x15 0x12` | — | SWR meter, 2-byte BCD. |
| `get_alc` | `0x15 0x13` | — | ALC meter, 2-byte BCD. |

### CTCSS

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `set/get repeater (TX) tone freq` | `0x1B 0x00` | — | **3-byte** BCD tenths of Hz, `00 hh ll`: 127.3 Hz is `00 12 73`. The reply has the same layout. |
| `set/get tone-squelch freq` | `0x1B 0x01` | — | Same encoding. |
| `set/get TONE on` | `0x16 0x42` | — | 1 byte flag. Repeater tone encode ("TONE" on the display). |
| `set/get TSQL on` | `0x16 0x43` | — | 1 byte flag. Tone squelch ("TSQL" on the display). On the IC-7100 TONE and TSQL are exclusive: switching one on switches the other off. |

### DTCS

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `set/get_dtcs_on` | `0x16 0x4A` | — | 1 byte flag. |
| `set/get_dtcs_code` | `0x1B 0x07` | — | 3 bytes: `[polarity, hi_BCD, lo_BCD]`. Polarity byte: 0x00 N/N, 0x01 N/R, 0x10 R/N, 0x11 R/R. |

### Split, RIT, XIT

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `set/get_split` | `0x0F` | — | 1 byte: 0 simplex, 1 split. |
| `set/get_rit_offset` | `0x21 0x00` | — | 3-byte signed BCD: 2 bytes magnitude LSB-first + 1 sign byte (0 +, 1 -). Range +/- 9.999 kHz. |
| `set/get_rit_on` | `0x21 0x01` | — | 1 byte flag. |
| `set/get_xit_on` | `0x21 0x02` | — | 1 byte flag. |

### AGC and noise

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `set/get_agc` | `0x16 0x12` | — | 1 byte: 0x01 fast, 0x02 mid, 0x03 slow. |
| `set/get_nb_on` | `0x16 0x22` | — | 1 byte flag. |
| `set/get_nb_level` | `0x14 0x12` | — | 2-byte BCD level (0..255, mapped to 0..100% in the API). |
| `set/get_nr_on` | `0x16 0x40` | — | 1 byte flag. |
| `set/get_nr_level` | `0x14 0x06` | — | 2-byte BCD level. |

### Preamp / attenuator / IF shift

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `set/get_preamp` | `0x16 0x02` | — | 1 byte: 0 off, 1 PRE1, 2 PRE2. |
| `set/get_atten` | `0x11` | — | 1 byte: `0x12` ON (12 dB pad in BCD), `0x00` OFF. **See [corrections](#corrections-vs-other-implementations).** |
| `set/get_if_shift` | `0x14 0x07` | — | 2-byte BCD, 0..255, 0x80 center. |

### Levels (the live-tuner knobs)

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `set/get_squelch` | `0x14 0x03` | — | 2-byte BCD level. |
| `set/get_rf_power` | `0x14 0x0A` | — | 2-byte BCD level. |
| `set/get_mic_gain` | `0x14 0x0B` | — | 2-byte BCD level. |
| `set/get_af_level` | `0x14 0x01` | — | 2-byte BCD level. AF GAIN knob; physical speaker volume. |

### VFO / memory

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `select_vfo` | `0x07 0x00` / `0x07 0x01` | — | A / B. |
| `swap_vfo` | `0x07 0xB0` | — | A <-> B exchange. |
| `equalize_vfo` | `0x07 0xA0` | — | Copy active VFO to inactive. |
| `enter_memory_mode` | `0x08` | — | No data; selects last memory channel. |
| `memory_select` | `0x08 [hi] [lo]` | — | 2-byte BCD channel number, 1..999. |
| `memory_write` | `0x09` | — | Write current VFO contents into selected memory channel. |
| `memory_to_vfo` | `0x0A` | — | Copy current memory contents into VFO. **See [corrections](#corrections-vs-other-implementations).** |
| `memory_clear` | `0x0B` | — | Clear currently-selected memory channel. **See corrections.** |
| `memory_read` | `0x1A 0x00 [hi] [lo]` | — | Read raw memory channel contents. Channel format is radio-model-specific; the wrapper returns the raw payload. |

### DATA mode

| Symbolic name | Opcode | Manual | Notes |
|---|---|---|---|
| `set/get_data_mode` | `0x1A 0x06` | p20-14 | Data `[flag, filter]`. flag: 0 OFF, 1 ON. filter: 0 when off, else 1/2/3 = FIL1/FIL2/FIL3. |

DATA mode is **per-VFO and per-mode**. In split, the radio's TX side is
the *inactive* VFO, so a programmatic key-down has to engage DATA on both
VFOs before keying and undo it on both after. `IC7100.set_ptt()` performs
this dance automatically and only undoes the toggle if it set it (never
disturbs an operator-engaged DATA state).

Note on the `MOD INPUT` configuration: the wrapper assumes the radio is
configured `DATA OFF MOD = MIC, DATA MOD = USB`, which is the standard
setup for "USB audio when the host is keying, hand mic otherwise." With
that config in place, the DATA-mode toggle is what switches the
modulation source on TX.

> **Page references.** Only the DATA-mode opcode has a manual page citation
> in the current source (p20-14 in `radio.py`). The other opcode pages
> have not yet been backfilled — TODO: walk the IC-7100 Full Manual and
> fill in the `Manual` column.

## Corrections vs other implementations

Four IC-7100 CI-V details are wrong in widely-copied example code (the
IC-7300/IC-9100 implementations in Python ham libraries are usually the
source of the bad code). The corrections below are the bench-verified
truth for the IC-7100.

### 1. Attenuator value byte

```
set_atten(True)  ->  FE FE 88 E0 11 12 FD
set_atten(False) ->  FE FE 88 E0 11 00 FD
```

The IC-7100 attenuator is a fixed **12 dB** pad. The data byte after `0x11`
is the dB amount in BCD, i.e. `0x12`. Some Python libs send `0x20` (the
IC-7300/7610's 20 dB pad value), some send `0x01` (a misread of "enable"
as a boolean). Both NG the IC-7100. The value `0x12` is the only one the
radio accepts as ON.

See `IC7100.set_atten` in [`../ic7100ctl/radio.py`](../ic7100ctl/radio.py).

### 2. Memory-to-VFO vs memory-clear ordering

```
memory_to_vfo()  ->  FE FE 88 E0 0A FD    # copy memory contents into VFO
memory_clear()   ->  FE FE 88 E0 0B FD    # clear selected memory channel
```

The IC-7100 manual is unambiguous: `0x0A` is Memory-to-VFO, `0x0B` is
Memory-clear. Some sources online (and some earlier revisions of this
code) had these swapped. The destructive-direction failure mode is
exactly the worst case: a "copy from memory to working VFO" call ended
up clearing the channel instead.

### 3. CALL channels are memory channels 106-109

The IC-7100 has four CALL channels:

| Name   | Memory channel |
|--------|----------------|
| 144-C1 | 106 |
| 144-C2 | 107 |
| 430-C1 | 108 |
| 430-C2 | 109 |

To select a CALL channel, issue `0x08 [hi_bcd] [lo_bcd]` with the channel
number — exactly like any other memory channel. There is no separate CALL
opcode group.

The IC-7100 does have memory banks (A-E). `0x08 0xA0 [bank]` selects one,
where `bank` is a 2-byte BCD number **counted from 1**: A is `00 01`, E is
`00 05`, and `00 04` selects D. A bare `0x08 0xA0` with no bank is answered
with NG, and no command that reads back the active bank is known, so a client
has to remember what it last selected. Earlier code used `0x08 0xA0` as a
CALL-channel selector; the CALL channels are just memory channels 106-109
(above).

### 4. Main / Sub band opcodes

`0x07 0xD0` / `0x07 0xD1` (Main band / Sub band select) are **IC-9100**
opcodes. The IC-9100 is a dual-receiver radio; Main/Sub is the active
receive bank. The IC-7100 is single-receiver — there is no Main/Sub
concept at the CI-V level. Don't implement these. If you send them, the
IC-7100 NGs.

`ic7100ctl` deliberately does not expose a `select_band()` method for
this reason; see the comment block in
[`../ic7100ctl/radio.py`](../ic7100ctl/radio.py) just below
`equalize_vfo()`.
