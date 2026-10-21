# Python API

The `hantek_dso` package can be used directly from Python for scripting,
automated tests and data logging. This page documents the public API. For the
wire protocol underneath, see [protocol/PROTOCOL.md](../protocol/PROTOCOL.md).

Install with `pip install -e .` (add `[gui]` only if you also want the GUI);
the API itself needs only `pyusb` and `numpy`.

**Example app:** [`scope_logger.py`](scope_logger.py) in this folder is a
complete small program built on the API. See [Example app](#example-app).

## Contents

- [Quick start](#quick-start)
- [Concepts](#concepts)
- [`DSO5000P`: the scope](#dso5000p-the-scope)
- [`Settings`: the settings block](#settings-the-settings-block)
- [`Waveform`](#waveform)
- [`hantek_dso.analysis`: math, FFT, measurements](#hantek_dsoanalysis-math-fft-measurements)
- [`hantek_dso.settings`: constants and enums](#hantek_dsosettings-constants-and-enums)
- [Low-level modules](#low-level-modules)
- [Errors](#errors)
- [Full records](#full-records)
- [Recipes](#recipes)
- [Example app](#example-app)

---

## Quick start

```python
from hantek_dso import DSO5000P
from hantek_dso.analysis import measure

with DSO5000P() as scope:
    st = scope.read_settings()                 # current scope settings
    ch1, ch2 = scope.read_waveform(4)          # both channels, same acquisition
    t  = st.time_axis(len(ch1.samples))        # seconds, 0 = trigger point
    v1 = st.to_volts(1, ch1.as_int8())         # volts
    print(measure(t, v1))                      # {'Vpp': '6V', 'Freq': '1.0004kHz', ...}
```

## Concepts

- **Settings first, then data.** Waveforms arrive as raw 8-bit screen units.
  Converting them to volts and seconds needs the scope's current settings, so
  read `Settings` together with each waveform.
- **Screen units.** One vertical division is 25 counts. Samples include the
  channel's vertical position offset; the screen is 8 × 16 divisions.
- **Indices, not values.** Settings fields hold menu indices (e.g. `HORIZ-TB = 15`
  means 200 µs/div). The tables in
  [`hantek_dso.settings`](#hantek_dsosettings-constants-and-enums) map them to
  physical values.
- **Two ways to change settings.** Some fields can be written directly with
  `set()`; others are changed by simulating front-panel keys (`set_timebase()`,
  `set_trigger_mode()`, `set_memory_depth()`, …). [PROTOCOL.md §4.3](../protocol/PROTOCOL.md#43-writing-settings-0x11-hw)
  explains why: the firmware ignores some writes and crashes on others.
- **One connection at a time.** Close the GUI before scripting, and vice versa.
- **Not thread-safe.** Use one `DSO5000P` from one thread. The client already
  paces requests (≥ 80 ms apart) and retries dropped ones.

---

## `DSO5000P`: the scope

```python
from hantek_dso import DSO5000P
scope = DSO5000P(transport=None, debug=False)
```

Opens the first scope found on USB (`049f:505a`), detaches the kernel's
`cdc_subset` network driver, and pings until the scope answers.

| parameter | meaning |
|---|---|
| `device` | which scope when several are connected: USB port path such as `"1-5.2.4.3"`, or `"bus:address"`; default: first found. `hantek_dso.transport.list_devices()` lists them |
| `transport` | a custom `UsbTransport` (e.g. to pick a specific bus/address); default: first scope found |
| `debug` | print every frame sent and received as hex |

Use it as a context manager, or call `close()` when done.

### Reading

| method | returns | notes |
|---|---|---|
| `echo(data=b"\x55\xaa")` | `bytes` | round-trip test; returns `data` |
| `read_settings()` | `Settings` | decoded 208-byte settings block |
| `read_settings_raw()` | `bytes` | the block undecoded |
| `read_waveform(source=0, timeout=15.0)` | `list[Waveform]` | `source`: 0 CH1, 1 CH2, 3 scope math, 4 all (CH1, CH2 and math from one acquisition, skipping channels that are off). Raises `ScopeError` if a single requested channel has no data (channel off, or the scope is stopped) |
| `screen_traces(settings=None)` | `dict[int, numpy.ndarray]` | CH1/CH2 traces extracted from a screenshot, in the same units and density as `read_waveform()` samples. Use when the scope is stopped: it then refuses waveform reads. Accurate to ≈ 0.03 div; horizontal detail is limited to the screen's 40 px/div |
| `screenshot(timeout=30.0)` | `numpy.ndarray` (480, 800, 3) `uint8` RGB | the scope's display |
| `screenshot_raw(timeout=30.0)` | `bytes` | 768 000 bytes RGB565 little endian |
| `read_file(path, timeout=30.0)` | `bytes` | any file on the scope, e.g. `"/protocol.inf"`; checksum-verified |
| `measure_text(item, source, mode=0)` | `str` | a measurement as rendered on the scope's screen (untested) |

The waveform record is the **on-screen window**: it always spans the scope's
screen (16 divisions with its side menu shown, 19.2 with it hidden), e.g. 3200 /
6400 / 12800 points at 4K / 20K / 40K depth on slower timebases, fewer on fast
ones. Use `Settings.x_divs()` / `time_axis()` rather than assuming a density.
While the scope is **stopped** it refuses waveform reads; use `screen_traces()`.

### Changing settings

| method | returns | notes |
|---|---|---|
| `set(force=False, **fields)` | `bool` (scope accepted) | writes fields by name: `set(**{"VERT-CH1-VB": 9})`. Only fields in `SAFE_WRITE_FIELDS` (vertical + trigger) are allowed; others raise `ScopeError` unless `force=True` |
| `write_settings(raw)` | `bool` | writes a complete 208-byte block (no safety check), then re-syncs |
| `set_timebase(index)` | `int` (index reached) | index into `TIMEBASE`; steps with the time/div keys |
| `set_trigger_mode(mode)` | `int` | 0 Auto, 1 Normal; via the trigger menu |
| `set_acquire_mode(mode)` | `int` | 0 Normal, 1 Peak detect, 2 Average; via the acquire menu |
| `set_averages(index)` | `int` | index into `AVERAGES` (4…256); only in Average mode, else `ScopeError` |
| `set_memory_depth(value)` | `int` | 0 = 4K, 2 = 20K, 4 = 40K (40K needs a single channel); via the acquire menu |
| `step_to(field, target, key_up, key_down, max_steps=40)` | `int` | generic: press keys until a field reaches `target`, reading back after each press |

The key-driven setters return the value actually reached, which can differ
from the request if the scope refuses it (e.g. 40K depth with both channels on).

Writable fields, by name (see [PROTOCOL.md §4.2](../protocol/PROTOCOL.md#42-field-meanings)
for encodings):

- `VERT-CH1-…` / `VERT-CH2-…`: `DISP`, `VB`, `COUP`, `20MHZ`, `PROBE`, `POS`
- `TRIG-TYPE`, `TRIG-SRC`, `TRIG-COUP`, `TRIG-VPOS`, `TRIG-EDGE-SLOPE`, `TRIG-HOLDTIME`
- `TRIG-VIDEO-NEG/-PAL/-SYN/-LINE`, `TRIG-PULSE-NEG/-WHEN/-TIME`,
  `TRIG-SLOPE-SET/-WIN/-WHEN/-V1/-V2/-TIME`, `TRIG-OVERTIME-NEG/-TIME`

### Front panel

| method | notes |
|---|---|
| `press_key(key)` | `key` is a name from `KEYS` (e.g. `"CT-RS-KEY"` = Run/Stop) or a code 0–48; returns the scope's menu-state byte |
| `set_menu_visible(on)` / `close_menu()` / `menu_visible()` | show/hide the scope's side menu (FN-0). The key-driven setters and `capture_full()` restore the menu's visibility when they finish |
| `lock_panel(on=True)` | panel/transfer lock flag (untested) |
| `remote_mode(on=True)` | remote-mode flag (untested) |

`hantek_dso.KEYS` maps all 49 key names to codes; run `hantek-dso key list`
to print them. Common ones: `CT-RS-KEY` Run/Stop, `CT-SINGLESEQ-KEY` Single,
`CT-AUTOSET-KEY` Autoset, `TG-FORCE-KEY` force trigger, `TG-PHALF-KEY` trigger
50 %, `HZ-TBADD-KEY` / `HZ-TBSUB-KEY` time/div, `HZ-PZERO-KEY` H-position to 0.

### Low-level access

| method | notes |
|---|---|
| `request(cmd, payload=b"", timeout=2.0, marker=0x53, retries=2)` | send one command, return the matching reply `Packet` |
| `send(cmd, payload=b"", marker=0x53)` / `recv(timeout=2.0)` | raw frame I/O (pacing still applied) |
| `sync(attempts=10)` | ping until the scope answers (done automatically on connect) |
| `close()` | release the USB interface |

---

## `Settings`: the settings block

Returned by `read_settings()`. Field names are those of `/protocol.inf`
(`settings.LAYOUT` lists them in order).

```python
st = scope.read_settings()
st["HORIZ-TB"]            # 15  (raw field value)
st.get("TRIG-SRC", 0)
st.values                 # dict of all fields
st.raw                    # the original 208 bytes
print(st.as_text())       # all fields, one per line
```

| method | returns |
|---|---|
| `channel_on(ch)` | `bool` |
| `volts_per_div(ch)` | V/div including probe factor, e.g. `5.0` |
| `probe(ch)` | 1, 10, 100 or 1000 |
| `position(ch)` | vertical offset in counts (25 per division) |
| `seconds_per_div()` | e.g. `0.0004` |
| `to_volts(ch, samples)` | `numpy` array of volts from int8 samples |
| `points_per_div(n)` / `sample_interval(n)` | record density (200/400/800 per division by depth) and seconds per sample |
| `x_divs(n)` | each sample's horizontal screen position in divisions (0 = centre); the record can be wider than 16 div |
| `time_axis(n)` | `numpy` array of n sample times in seconds, 0 = trigger point (includes the horizontal offset) |
| `trigger_channel()` | 1, 2, or `None` for EXT / AC-line sources |
| `trigger_level_volts()` | trigger level in volts, or `None` |
| `trigger_frequency()` | hardware frequency-counter reading in Hz |
| `trigger_state()` | `"Stop"`, `"Ready"`, `"Auto"`, `"Trig'd"`, … |
| `counts_to_volts(ch, counts)` / `volts_to_counts(ch, volts)` | convert trigger-level / slope-threshold values |
| `encode(**fields)` | a new 208-byte block with fields replaced (for `write_settings`) |

`ch` is 1 or 2 throughout.

---

## Full records

`read_waveform()` returns only the on-screen window. `capture_full()` gets the
**whole acquisition record** (4 000 / 20 000 / 40 000 points at memory depth
4K / 20K / 40K) by making the scope export it as CSV to a **USB stick in its
front port**, downloading the file and deleting it again
([PROTOCOL.md §3.3](../protocol/PROTOCOL.md#33-full-records-via-the-scopes-csv-export-hw)).

```python
with DSO5000P() as scope:
    rec = scope.capture_full(1)                 # CH1, fresh single shot
    print(len(rec.t), rec.sample_rate)          # 20000  2000000.0  (at 20K depth)
```

| method | returns | notes |
|---|---|---|
| `capture_full(channel=1, single=True, keep_file=False, trigger_timeout=30.0, progress=None)` | `FullRecord` | `single=False` saves the current acquisition instead of taking a new one; `progress` is called with status strings. Takes 5–10 s; the scope resumes Run afterwards if it was running. Raises `ScopeError` if there's no stick, the channel is off, no trigger arrives, or the scope refuses (e.g. zoom view on) |
| `usb_stick_present()` | `bool` | is a stick mounted at `/mnt/udisk` |
| `resume()` | `bool` | put the scope back into Run if it is stopped |
| `shell(command)` | `str` | run a shell command on the scope via the factory protocol (used for `ls`/`rm` on the stick; use with care) |

`FullRecord` fields: `channel`, `t` (seconds from the start of the record),
`volts`, `timebase` (s/div), `voltbase` (V/div), `filename`, and the
property `sample_rate`. Unlike `time_axis()`, `t` is not referenced to the
trigger point.

---

## `Waveform`

```python
@dataclass
class Waveform:
    source: int            # 0 CH1, 1 CH2, 2 scope math
    samples: bytes         # signed 8-bit screen units
    declared_length: int   # sample count announced by the scope
    def as_int8(self) -> numpy.ndarray
```

Convert with `Settings.to_volts(source + 1, wf.as_int8())`.

---

## `hantek_dso.analysis`: math, FFT, measurements

These run on the PC and need only numpy.

| function | description |
|---|---|
| `measure_values(t, v) -> dict[str, float]` | numeric measurements: `vpp`, `vmax`, `vmin`, `mean`, `rms` and, when periodic, `freq`, `period`, `width`, `duty` (%), `rise` (s) |
| `measure(t, v, unit="V") -> dict[str, str]` | the same, formatted for display: Vpp, Vmax, Vmin, Mean, RMS and, when the signal is periodic, Freq, Period, +Width, Duty, Rise (10–90 %) |
| `fft(v, dt, win="Hanning", db=True) -> (f, a)` | single-sided spectrum; `a` in dBV RMS (or V RMS with `db=False`); DC removed |
| `peak(f, a) -> (freq, amp)` | largest non-DC peak with parabolic interpolation between bins |
| `window(name, n)` | window function: one of `FFT_WINDOWS` = Rectangular, Hanning, Hamming, Blackman, Flat top |
| `math_channel(op, v1, v2)` | `"CH1 + CH2"`, `"CH1 − CH2"`, `"CH2 − CH1"`, `"CH1 × CH2"` (note the Unicode − and ×) |

`Reference` stores a waveform together with its display scale, as used by
the GUI's REF A/B:

```python
from hantek_dso.analysis import Reference
ref = Reference("golden", t, v1, volts_per_div=5.0, seconds_per_div=4e-4)
ref.save_csv("golden.csv")              # CSV with a JSON metadata header line
ref = Reference.load_csv("golden.csv")  # also accepts plain time,volts CSVs
x_div, y_div = ref.to_divs()
```

---

## `hantek_dso.settings`: constants and enums

| name | meaning |
|---|---|
| `VOLTS_PER_DIV` | V/div for `VERT-CHn-VB` index (index 0 = 1 mV is not offered; `MIN_VB_INDEX = 1`) |
| `TIMEBASE` | s/div for `HORIZ-TB` index (2 ns … 40 s) |
| `PROBE`, `COUPLING` | probe factors, channel coupling names |
| `TRIG_TYPE`, `TRIG_SRC`, `TRIG_MODE`, `TRIG_COUP`, `EDGE_SLOPE`, `TRIG_STATE` | trigger enums |
| `VIDEO_POLARITY`, `VIDEO_STANDARD`, `VIDEO_SYNC`, `PULSE_POLARITY`, `WHEN` | extra trigger parameter enums |
| `ACQ_MODE`, `AVERAGES`, `STORE_DEPTH` | acquisition enums (`STORE_DEPTH` is a dict: 0→4K, 2→20K, 4→40K) |
| `COUNTS_PER_DIV` (25), `VERT_DIVS` (8), `HORIZ_DIVS` (16) | screen geometry |
| `SAFE_WRITE_FIELDS` | fields `DSO5000P.set()` accepts without `force` |
| `LAYOUT`, `LAYOUT_SIZE`, `KEYS` | parsed `protocol.inf` / `keyprotocol.inf` |
| `fmt_si(value, unit)` | format with SI prefix, e.g. `fmt_si(0.0004, "s")` → `'400µs'` |
| `fmt_fixed(value, unit, width=9, sig=4)` | fixed-width, `sig` significant digits, for live readouts: `fmt_fixed(-0.2, "V")` → `' -200.0mV'`; use `sig=3` for two-character units such as V² |

---

## Low-level modules

**`hantek_dso.protocol`**: framing, no I/O.

| name | description |
|---|---|
| `build(cmd, payload=b"", marker=MARK_NORMAL) -> bytes` | build a frame with length and checksum |
| `checksum(data) -> int` | 8-bit sum |
| `Deframer().feed(data) -> list[Packet]` | incremental parser; resynchronises after noise |
| `Packet(marker, cmd, payload)` | `.is_reply`, `.request_cmd` |
| `CMD_*`, `SUB_*`, `SRC_*`, `MARK_*` | command, chunk sub-code, waveform source and marker constants |

**`hantek_dso.transport`**: USB I/O.

| name | description |
|---|---|
| `UsbTransport(bus=None, address=None)` | open a scope (optionally a specific one) |
| `.write(data, timeout_ms=2000)`, `.read(size=65536, timeout_ms=500)` | bulk I/O; `read` returns `b""` on timeout |
| `.drain(quiet_ms=100)`, `.close()` | discard pending input / release |
| `VID`, `PID`, `EP_OUT`, `EP_IN` | `0x049F`, `0x505A`, `0x02`, `0x81` |

---

## Errors

| exception | raised when |
|---|---|
| `hantek_dso.transport.TransportError` | scope not found, or USB permission denied (install the udev rule) |
| `hantek_dso.ScopeError` | no reply / timeout, scope reports no data, transfer checksum mismatch, or a write refused by `set()` |
| `usb.core.USBError` | the USB connection dropped (e.g. cable unplugged, scope rebooted) |

---

## Recipes

**Log the frequency counter once a second**

```python
import time
from hantek_dso import DSO5000P

with DSO5000P() as scope:
    while True:
        st = scope.read_settings()
        print(time.strftime("%H:%M:%S"), st.trigger_frequency(), "Hz", st.trigger_state())
        time.sleep(1)
```

**Single-shot capture to CSV**

```python
import time
import numpy as np
from hantek_dso import DSO5000P

with DSO5000P() as scope:
    scope.set_trigger_mode(1)                    # Normal
    scope.press_key("CT-SINGLESEQ-KEY")          # arm single sequence
    while scope.read_settings().trigger_state() != "Stop":
        time.sleep(0.2)                          # wait for the trigger
    st = scope.read_settings()
    waves = [w for w in scope.read_waveform(4) if w.source in (0, 1)]
    n = min(len(w.samples) for w in waves)
    cols = [st.time_axis(n)] + [st.to_volts(w.source + 1, w.as_int8()[:n]) for w in waves]
    np.savetxt("single.csv", np.column_stack(cols), delimiter=",",
               header="time_s," + ",".join(f"CH{w.source + 1}_V" for w in waves), comments="")
```

**Sweep the timebase and screenshot each step**

```python
from PIL import Image                      # or any image library
from hantek_dso import DSO5000P
from hantek_dso.settings import TIMEBASE, fmt_si

with DSO5000P() as scope:
    for i in (10, 13, 16):
        scope.set_timebase(i)
        Image.fromarray(scope.screenshot()).save(f"tb_{fmt_si(TIMEBASE[i], 's')}.png")
```

**Configure an edge trigger at 1.5 V on CH2, falling edge**

```python
with DSO5000P() as scope:
    st = scope.read_settings()
    scope.set(**{"TRIG-TYPE": 0, "TRIG-SRC": 1, "TRIG-EDGE-SLOPE": 1,
                 "TRIG-VPOS": st.volts_to_counts(2, 1.5)})
    print(scope.read_settings().trigger_level_volts())   # e.g. 1.6
```

Trigger levels move in steps of one count (V/div ÷ 25; 0.2 V at 5 V/div), so
read the level back to see the value actually set.

---

## Example app

[`scope_logger.py`](scope_logger.py) is a small, complete program (about 130
lines) showing a typical API workflow:

1. connect with `with DSO5000P() as scope:`
2. read `Settings` and both channels from one acquisition (`read_waveform(4)`)
3. convert samples to volts and seconds (`to_volts`, `time_axis`)
4. measure on the PC and read the scope's frequency counter
5. print a live table, and optionally log to CSV and save every capture

```console
$ python api/scope_logger.py -i 1 -n 4 -o log.csv --waveforms waves/
time      state       counter    CH1 Vpp    CH1 freq    CH2 Vpp    CH2 freq
10:05:36  Trig'd         1kHz         6V        1kHz       5.8V   1.0004kHz
10:05:37  Trig'd         1kHz         6V        1kHz       5.8V        1kHz
10:05:38  Trig'd         1kHz         6V        1kHz       5.8V   1.0004kHz
10:05:39  Trig'd         1kHz         6V        1kHz       5.8V        1kHz
logged 4 rows to log.csv
```

| option | meaning |
|---|---|
| `-i, --interval SECONDS` | time between samples (default 2) |
| `-n, --count N` | number of samples; 0 = until Ctrl+C (default) |
| `-o, --output FILE` | append one row per sample to a CSV (Vpp, Vmax, Vmin, mean, RMS, frequency per channel, plus trigger state and frequency counter) |
| `--waveforms DIR` | also save each capture as `DIR/capture_NNNNN_<timestamp>.csv` (time, CH1, CH2 in volts) |

Run it from the repository root, or anywhere after `pip install -e .`.
