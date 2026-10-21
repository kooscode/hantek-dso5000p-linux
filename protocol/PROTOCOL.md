# Hantek DSO5000P USB protocol

Reverse engineered from:

* scope firmware `dso.exe` 3.40.0 (210225), ARM, module `opt/pc_uart.c`
  (dispatchers `FUN_000e3760` = normal protocol, `FUN_000e3a80` = debug protocol),
  analysed with REA + Ghidra 12.1.4
* the Windows "Scope" application (Wise installer); receive loop `FUN_0048d378`,
  command builder `FUN_0043da00`
* live tests on a DSO5102P (modified to 200 MHz), October 2026

Status legend: **[HW]** verified on hardware · **[FW]** read from firmware code only.

---

## 1. Transport

| item | value |
|---|---|
| USB ID | `049f:505a`, product "Gadget Serial v2.4" (Linux `g_serial`, vendor mode) **[HW]** |
| endpoints | bulk OUT `0x02`, bulk IN `0x81`, 512 B max packet **[HW]** |
| scope side | `/dev/ttyGS0`, read one byte at a time; a pause ends a request **[FW]** |
| Linux host | binds `cdc_subset` (usbnet → `usb0`) to this ID, and NetworkManager then sends DHCP/IPv6 frames that the scope rejects. Detach the driver (libusb) and mark the device unmanaged (udev rule) **[HW]** |
| Windows host | vendor driver `dstusb.sys`; IOCTL `0x22200d` = bulk write, `0x222012` = bulk read **[FW]** |

### Pacing and recovery [HW]

* Send every request as **one** bulk write and leave **≥ 80 ms** between
  requests (the Windows app sleeps 80 ms). Frames sent back-to-back are merged and
  rejected; the scope console then prints `PcUartDoScan: rec data is not right`.
* The **first frame after claiming the interface is often lost**. Ping with
  echo (0x00) until it answers.
* Don't send `SET_CONFIGURATION` if the device is already configured: it resets
  the gadget and drops the next request.
* After a settings write (0x11) the scope restarts its acquisition threads and
  ignores requests for about 0.3 s. Wait, then re-sync with echo.
* While the user turns knobs, requests are occasionally dropped. Retry on timeout.

## 2. Framing [HW]

```
offset:  0        1       2       3     4 … len+1   len+2
        'S'|'C'  len_lo  len_hi  cmd   payload…    sum
```

* `len` (little endian) counts everything after the length field:
  cmd + payload + sum. The frame is `len + 3` bytes.
* `sum` = 8-bit sum of bytes `0 … len+1`. The firmware also accepts `0x66` ('f')
  as a wildcard checksum.
* Replies use `cmd | 0x80`. The firmware builds a reply by copying the
  request header and setting bit 7 of `cmd`.
* `'S'` (0x53) = normal protocol, `'C'` (0x43) = factory/debug protocol.

## 3. Normal commands (`'S'`)

| cmd | request payload | reply | notes |
|---|---|---|---|
| 0x00 | any bytes | 0x80 + same bytes | echo **[HW]** |
| 0x01 | – | 0x81 + 208-byte settings block | see §4 **[HW]** |
| 0x02 | `00` | 0x92 `01 01` | sets the "PC transfer" flag [FW] |
| 0x02 | `01 src` | waveform stream, §3.1 | src 0 CH1, 1 CH2, 4 all **[HW]**; 2 = CH1, or CH2 if CH1 has no data; 3 = scope math [FW] |
| 0x10 | `00 <path>` | 0x90 `01 data…` chunks, then 0x90 `02 sum8` | read any file on the scope; 10208-byte chunks; path ≤ 32 chars **[HW]** |
| 0x11 | 208-byte block | 0x91 `00` ok / `ff` wrong length | apply settings, see §4.3 **[HW]** |
| 0x12 | `00 0/1` | 0x92 `00` | remote-mode flag (internal 0x8a4) [FW] |
| 0x12 | `01 0/1` | 0x92 `01 state` | panel/transfer lock flag (internal 0x870) [FW] |
| 0x13 | `key` (Windows sends `key count`) | 0x93 `menu-state` | press a front-panel key, §5 **[HW]** |
| 0x14 | `u16 b b b b b` | 0x94 | sets an internal 5-field struct (0x602c4); purpose unknown [FW] |
| 0x20 | – | 0xA0 `01 data…` chunks, then 0xA0 `02 sum8` | screenshot, §3.2 **[HW]** |
| 0x21 | – | 0xA1 `u16 b b b b b` | reads the 0x14 struct **[HW]** |
| 0x22 | `item src mode` | 0xA2 ASCII text | a measurement value as shown on screen [FW] |

### 3.1 Waveform stream (reply to `0x02 01 src`) [HW]

For each channel in the request (src 0 → CH1; 1 → CH2; 3 → scope math;
4 → CH1, CH2, scope math, in that order), the scope sends either:

1. `0x82 00 <u24 LE count>`: start, with the sample count. The 4th count byte
   is overwritten by the checksum, so only 24 bits are usable.
2. `0x82 01 <src> <int8 × n>`: data, n ≤ 10000 per frame, repeated until
   `count` samples have arrived.

or, if the channel is off or has no new acquisition:

3. `0x82 03 <ch>`.

The request ends with `0x82 02 <src>`.

**The `<src>` byte in data frames echoes the request, not the channel.** With
src 4, tell channels apart by the order of their start frames. Src 4 returns
CH1 and CH2 from the **same acquisition**; two separate requests may not.

**Record length:** the record is the **on-screen window** only, centred on the
screen. Its density depends on memory depth, its width on the scope's screen:
**16 divisions with the side menu shown, 19.2 with it hidden** (FN-0) **[HW]**.

| ACQURIE-STORE-DEPTH | points/div | points (menu shown) | points (menu hidden) |
|---|---|---|---|
| 0 (4K) | 200 | 3200 | 3840 **[HW]** |
| 2 (20K) | 400 | 6400 | 7680 |
| 4 (40K, single channel) | 800 | 12800 | 15360 |

The table holds for slower timebases. On fast timebases the record has fewer
points (e.g. 640 at 2 µs/div) but **still spans the whole screen**, so the
reliable rule is: points per division = record length ÷ screen width (16 or
19.2 div).

Limits of this command:

* The off-screen part of a deep record isn't returned. The firmware sends a
  window (start, length) of the display-processed buffer **[HW]**.
* **The scope only hands out a frame while one is queued for display**
  (`FUN_0003f808`: frame count at `+0x1808` ≥ 1; the handler retries 10× over
  ~100 µs, then replies `0x82 03`). While running, a frame is nearly always
  available. **Once stopped (Run/Stop or after Single), reads fail** apart from a
  brief moment right after the stop, and redraws don't re-queue it **[HW]**. The
  client then extracts the traces from a screenshot instead
  (`DSO5000P.screen_traces()`; screen geometry 40 px/div horizontally, 50 px/div
  vertically, centre row 237, centre column 335 with the side menu / 399
  without; agrees with USB data to ≈ 0.03 div).
* The Windows application reads waveforms with this same command (it sends
  `0x12 01 01` to lock the panel, then `0x02 01 <ch>` per channel;
  `FUN_00456226`), so it has the same limits **[FW]**. Its `g_DepthSize` is
  only its receive-buffer size.
* Untested: whether the window grows in zoom-window mode (`HORIZ-WIN-STATE`).
* Full records (up to 40 000 points) are available through the scope's CSV
  export, see §3.3.

**Sample scaling:** samples are signed 8-bit screen units, 25 per division,
and include the channel's vertical offset. They are clipped to ±127 (≈ ±5 div;
the screen shows ±4 div).

```
volts(i) = (sample[i] − VERT-CHn-POS) / 25 × V/div × probe
dt       = (s/div) × screen_divs / N      (screen_divs = 16, or 19.2 with the menu hidden)
time(i)  = HORIZ-TRIGTIME + (i − N/2) × dt      (0 s = trigger point)
```

In scan/roll mode the firmware replaces the not-yet-acquired part of the
record with `0xff` (−1) [FW].

### 3.2 Screenshot (0x20) [HW]

800 × 480 pixels, RGB565 little endian, 768 000 bytes in 10208-byte chunks.
The firmware sums each chunk *after* sending it, reading the live
framebuffer, so the final checksum doesn't match whenever the screen redraws
during the transfer. Ignore it for screenshots, but check it for file reads (0x10).

### 3.3 Full records via the scope's CSV export [HW]

The full acquisition record (not just the screen window) can be fetched by
having the scope save it as CSV to a USB stick, then downloading the file:

1. Single capture (key 18), wait until TRIG-STATE = Stop.
2. Save/Recall menu (key 11, menu id 47) → FN-3 "CSV" (menu id 48).
   FN-1 toggles the source (CH1/CH2); the selected radio button is drawn
   orange (no settings field reflects it, so read it from a screenshot).
   FN-2 = Save: if the file list is closed, the first press only opens it and
   a second press saves `/mnt/udisk/WaveDataNN.csv`. FN-6 = Back.
3. Wait until the file stops growing, download it with 0x10 (≈ 750 KB/s),
   then delete it.

`DSO5000P.capture_full()` implements this using only key presses, settings
reads and the stick shell. A typical 4K/20K capture takes 5–8 s.

Needs a FAT USB stick in the front port; without one the scope shows "Please
insert USB device first". The firmware (`FUN_0005b97c`) writes the raw channel
buffer: a header `#timebase=<ps>`, `#voltbase=<µV>`, `#size=<n>`, then one
`time_s,millivolts` line per sample (note: **millivolts**; time from the start
of the record, not from the trigger). It writes the nominal depth **+ 64
samples**, and the last ~16 of those are invalid (−8 … −9 V spikes), so trim
to 4 000 / 20 000 / 40 000. Example: at 4 ms/div, depth 40K → 40 000 points
at 200 kSa/s covering 200 ms (the screen shows 64 ms).

The scope refuses the export with "This function can not be used within this
condition!" in some states, e.g. while the zoom (window) view is on.

Finding and deleting the file without the serial console needs a directory
listing; the debug command `'C' 0x11 "ls -l /mnt/udisk/*.csv"` works.

**Record length.** It equals the internal memory depth: 4 000 / 20 000 /
40 000. Internally the firmware also has 400 000 (shown as "512K"),
800 000 ("1M") and 1 600 000 ("2M") depths, but in firmware 3.40:

* the Acquire menu disables those three entries **unconditionally**
  (`FUN_0002b978`), whatever the channel or timebase settings, and
* the 0x11 wire→internal depth table only reaches the 4K/20K/40K slots.

So **records longer than 40 000 points are not available**. An experimental
firmware patch that re-enabled the 512K/1M menu entries made them selectable,
but the acquisition setup code (`FUN_00085458` "store depth not find!",
`FUN_000850d0`, per-depth tables at `DAT_00a2b1b4`) only implements three depths,
so those settings produced noise **[HW]**. The scope was restored to stock firmware.

**Caution [HW]:** a 0x11 settings write (turning CH2 back on) made with the
S/R or Acquire menu open and a USB stick mounted froze the scope application
completely (screen and USB) and needed a power cycle. Prefer key presses for
changes during such sequences, and leave the menus first.

## 4. Settings block (0x01 / 0x11)

### 4.1 Layout

The firmware builds the block from `/protocol.inf` on the scope: each
`[NAME] size` line is one field, packed little endian, back to back (copy in
`hantek_dso/protocol.inf`; 208 bytes for firmware 3.40.0). 2- and 8-byte fields
are signed. 8-byte time fields are **picoseconds**.

### 4.2 Field meanings

| field | meaning | status |
|---|---|---|
| VERT-CHn-DISP | 0 off, 1 on | **[HW]** |
| VERT-CHn-VB | 0 1 mV (not offered; clamps to 1), 1 2 mV, 2 5 mV, 3 10 mV, … 12 10 V (1-2-5), before probe factor | **[HW]** |
| VERT-CHn-COUP | 0 DC, 1 AC, 2 GND | **[HW]** for 0, menu order |
| VERT-CHn-20MHZ | 0 off, 1 bandwidth limit on | menu order |
| VERT-CHn-PROBE | 0 1×, 1 10×, 2 100×, 3 1000× | **[HW]** |
| VERT-CHn-POS | vertical offset, signed, 25 per division | **[HW]** |
| TRIG-STATE | 0 Stop, 1 Ready, 2 Auto, 3 Trig'd (4 Scan, 6 Armed inferred), read-only | **[HW]** for 0–3 |
| TRIG-TYPE | 0 Edge, 1 Video, 2 Pulse, 3 Slope, 4 Overtime, 5 Alternate | **[HW]** for 0–4 |
| TRIG-SRC | 0 CH1, 1 CH2, 2 EXT, 3 EXT/5, 4 AC50 (line) | **[HW]** for 0/1, menu order |
| TRIG-MODE | 0 Auto, 1 Normal (0x11 ignores it, see §4.3) | **[HW]** |
| TRIG-COUP | 0 DC, 1 AC, 2 Noise reject, 3 HF reject, 4 LF reject | **[HW]** for 0–2 |
| TRIG-VPOS | trigger level in source-channel counts: V = (VPOS − POS_src) / 25 × V/div_src | **[HW]** |
| TRIG-FREQUENCY | hardware frequency counter, **millihertz**, read-only | **[HW]** |
| TRIG-HOLDTIME(-MIN/-MAX) | holdoff, ps; limits 100 ns … 10 s | **[HW]** |
| TRIG-EDGE-SLOPE | 0 Rising, 1 Falling | **[HW]** |
| TRIG-VIDEO-NEG | 0 Normal, 1 Inverted | **[HW]** |
| TRIG-VIDEO-PAL | 0 NTSC, 1 PAL/SECAM | **[HW]** |
| TRIG-VIDEO-SYN | 0 All lines, 1 Line number, 2 Odd field, 3 Even field, 4 All fields | **[HW]** |
| TRIG-VIDEO-LINE | line number | **[HW]** |
| TRIG-PULSE-NEG | 0 Positive, 1 Negative | **[HW]** |
| TRIG-PULSE-WHEN | 0 `=`, 1 `≠`, 2 `>`, 3 `<` | **[HW]** |
| TRIG-PULSE-TIME | pulse width, ps | **[HW]** |
| TRIG-SLOPE-SET | 0 Rising, 1 Falling | **[HW]** |
| TRIG-SLOPE-WIN | which threshold the knob edits: 0 V1, 1 V2 | **[HW]** |
| TRIG-SLOPE-WHEN | 0 `=`, 1 `≠`, 2 `>`, 3 `<` | **[HW]** |
| TRIG-SLOPE-V1 / -V2 | thresholds, counts (same conversion as VPOS) | **[HW]** |
| TRIG-SLOPE-TIME | slope time, ps | **[HW]** |
| TRIG-OVERTIME-NEG / -TIME | 0 Positive, 1 Negative / time, ps | **[HW]** |
| TRIG-SWAP-CHn-* | per-channel setups for Alternate trigger (same encodings) | [FW] |
| HORIZ-TB | 0 = 2 ns/div … 31 = 40 s/div, 1-2-4 steps (15 = 200 µs, 16 = 400 µs) | **[HW]** |
| HORIZ-TRIGTIME | horizontal position, ps; positive moves the trigger left of centre (screen centre = +value) | **[HW]** |
| HORIZ-WIN-TB / -WIN-STATE | zoom-window timebase / zoom on | [FW] |
| ACQURIE-MODE | 0 Normal, 1 Peak detect, 2 Average | **[HW]** |
| ACQURIE-AVG-CNT | 0…6 = 4, 8, 16, 32, 64, 128, 256; reads 0 unless mode = Average | **[HW]** |
| ACQURIE-TYPE | 0 Real time, 1 Equivalent time | menu order |
| ACQURIE-STORE-DEPTH | 0 4K, 2 20K, 4 40K (40K single-channel only). Other values are invalid: 1 maps to internal depth −1 and crashed the scope. 512K/1M/2M can't be selected, see §3.3 | **[HW]** |
| CONTROL-MENUID | menu on screen: 5 trigger (edge), 6/7 pulse page 1/2, 22/23 slope page 1/2, 17 acquire | **[HW]** |
| CONTROL-DISP-MENU | 1 = side menu visible; FN-0 toggles it | **[HW]** |
| MATH-*, DISPLAY-*, MEASURE-* | scope math/FFT, display and on-screen measurement settings | [FW] |

(The spelling "ACQURIE" is the firmware's.)

### 4.3 Writing settings (0x11) [HW]

0x11 replaces the whole block; the firmware applies only fields flagged
writable when `/protocol.inf` is parsed. Tested results:

| fields | result of writing via 0x11 |
|---|---|
| VERT-CHn-DISP / VB / COUP / 20MHZ / PROBE / POS | applied immediately |
| all TRIG-* listed above except STATE/MODE/FREQUENCY | applied immediately |
| HORIZ-TB | stored and read back, **but not applied** → use keys HZ-TBADD/TBSUB |
| TRIG-MODE | **ignored** → use the trigger menu, soft key FN-4 |
| ACQURIE-STORE-DEPTH | **crashed and rebooted the scope** → use the acquire menu, FN-5 |

Always read the block, change fields, and write it back; never send stale
values. `DSO5000P.set()` allows only the tested fields unless `force=True`.

## 5. Keys (0x13)

The key code is an index into `/keyprotocol.inf` (copy in
`hantek_dso/keyprotocol.inf`):

| code | key | code | key |
|---|---|---|---|
| 0–7 | FN-0 menu on/off, FN-1…6 side-menu soft keys, FN-7 zoom view on/off | 24–29 | CH1 menu, pos−, pos+, pos 0, V/div−, V/div+ |
| 8–10 | multipurpose knob left / right / push | 30–35 | same for CH2 |
| 11 | Save/Recall menu | 36 | horizontal menu |
| 12 | Measure menu | 37–39 | H-pos −, +, 0 |
| 13 | Acquire menu | 40–41 | time/div −, + (faster / slower) |
| 14 | Utility menu | 42 | trigger menu (always opens, never toggles) |
| 15 | Cursor menu | 43–45 | trigger level −, +, 0 |
| 16 | Display menu | 46 | trigger level 50 % |
| 17 | Autoset | 47 | force trigger |
| 18 | Single | 48 | probe check |
| 19 | Run/Stop | 20–23 | Help, Default setup, Save to USB, Math menu |

**[HW]**: 11, 13, 18, 19, 26, 27, 36, 38–42, FN-0 (shows/hides the side menu;
CONTROL-DISP-MENU reflects it), soft keys FN-1…FN-6, and FN-7 (toggles the
zoom/window view; HORIZ-WIN-STATE does not reflect it). The other key meanings
come from the key names in `/keyprotocol.inf`.

**Soft keys [HW].** FN-1…FN-6 are the side-menu buttons, top to bottom.

* Trigger menu: FN-1 type, FN-2 source, FN-3 slope/polarity, FN-4 mode,
  FN-5 coupling, FN-6 next page. Page 2 holds "When" and the width/time
  parameters for Pulse and Slope.
* Acquire menu: FN-2 mode, FN-3 averages, FN-5 memory depth (each press
  cycles to the next option).
* Save/Recall menu (47): FN-3 CSV submenu (48): FN-1 source, FN-2 save,
  FN-6 back. Horizontal menu (3): page 2 is menu 40.

Read `CONTROL-MENUID` to confirm which menu is open before pressing soft keys.

**Timing [HW].** Presses less than about 120 ms apart can be dropped, and the
settings block updates a little after a press. Poll the field until it changes
before pressing again, or you will overshoot.

## 6. Debug protocol (`'C'`) [FW]

Used by the factory tools. This client uses only `0x11` (shell) for `ls`/`rm`
on the USB stick during full-record capture, plus the read-only `0x02` dump in
an investigation. **Several of these can brick the scope or wipe its
calibration.**

| cmd | payload | function |
|---|---|---|
| 0x00 | `00 reg` / `01 start count` | read one FPGA register / read `count` 16-bit words of the FPGA register map |
| 0x01 | `u16 len` | wait for an acquisition, then read up to 4096 bytes from the ADC capture FIFO |
| 0x02 / 0x03 | – | dump the internal settings struct (0x1188 B) / calibration table (0x8a60 B) |

Single-shot state **[HW]**: nothing in the settings block, the 0x1188-byte
internal dump (`'C' 0x02`, read-only) or the scope's screen distinguishes
"single sequence armed" from "running, waiting for a trigger" (both report
TRIG-STATE = Ready). Internal offset 0x888 holds the last front-panel key
(key code + 2, e.g. 20 after SingleSeq), which would only allow a heuristic.
| 0x10 | `00 <path>` | read a file (as normal 0x10) |
| 0x11 | `<command>` | **run a shell command as root**, reply = its output |
| 0x40 / 0x41 | struct | overwrite the settings struct / calibration table |
| 0x42 | `reg u32` | hardware register write |
| 0x43 | `key n` | press a key n times |
| 0x44 / 0x45 | `b` / `i16 + 8 B` | internal setters (purpose unknown) |
| 0x50 | `00 name`, `01 data…`, `02 sum8` | upload a file to `/tmpp`, then move it to `name` |
| 0x60 | `00 dev`, `01 data…`, `02 sum8` | write to a flash/MTD device |
| 0x7f | – | restart the application |
