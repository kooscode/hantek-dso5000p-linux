"""Scope settings block (reply to command 0x01, input to command 0x11).

The firmware builds the block from /protocol.inf on the scope: each
``[NAME] size`` line is one field, packed little endian, back to back. The copy
shipped next to this module was read from a DSO5102P running firmware
3.40.0 (210225); 208 bytes total.

Verified on hardware (see protocol/PROTOCOL.md):
  * VERT-CHn-VB      index into VOLTS_PER_DIV (1 mV entry is clamped to 2 mV)
  * VERT-CHn-PROBE   index into PROBE
  * VERT-CHn-POS     signed, same units as samples (25 counts / division)
  * HORIZ-TB         index into TIMEBASE (2 ns ... 40 s, 1-2-4 steps)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_HERE = os.path.dirname(os.path.abspath(__file__))

COUNTS_PER_DIV = 25      # sample units per vertical division
VERT_DIVS = 8
HORIZ_DIVS = 16
POINTS_PER_DIV = 200     # at 4K memory depth
POINTS_PER_DIV_BY_DEPTH = {0: 200, 2: 400, 4: 800}  # USB record density per depth [HW]


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    size: int


def load_layout(path: str | None = None) -> list[Field]:
    path = path or os.path.join(_HERE, "protocol.inf")
    fields: list[Field] = []
    off = 0
    started = False
    with open(path, encoding="latin-1") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith(("#", "/")):
                continue
            if line.startswith("[START]"):
                started = True
                continue
            if line.startswith("[END]"):
                break
            if not started or not line.startswith("["):
                continue
            name, _, rest = line[1:].partition("]")
            fields.append(Field(name, off, int(rest.split()[0])))
            off += fields[-1].size
    return fields


def load_keys(path: str | None = None) -> dict[str, int]:
    path = path or os.path.join(_HERE, "keyprotocol.inf")
    return {f.name: i for i, f in enumerate(load_layout(path))}


LAYOUT = load_layout()
LAYOUT_SIZE = sum(f.size for f in LAYOUT)
KEYS = load_keys()

# Fields that the scope applies immediately when written with command 0x11 and
# that were tested safe. Writing other fields can be ignored by the firmware
# (HORIZ-TB) or crash it (ACQURIE-STORE-DEPTH rebooted the scope).
SAFE_WRITE_FIELDS = {
    f"VERT-CH{c}-{n}" for c in (1, 2)
    for n in ("DISP", "VB", "COUP", "20MHZ", "PROBE", "POS")
} | {
    # trigger fields tested live 2026-10-20 (applied immediately, no crash)
    "TRIG-TYPE", "TRIG-SRC", "TRIG-COUP", "TRIG-VPOS", "TRIG-EDGE-SLOPE", "TRIG-HOLDTIME",
    "TRIG-VIDEO-NEG", "TRIG-VIDEO-PAL", "TRIG-VIDEO-SYN", "TRIG-VIDEO-LINE",
    "TRIG-PULSE-NEG", "TRIG-PULSE-WHEN", "TRIG-PULSE-TIME",
    "TRIG-SLOPE-SET", "TRIG-SLOPE-WIN", "TRIG-SLOPE-WHEN", "TRIG-SLOPE-V1",
    "TRIG-SLOPE-V2", "TRIG-SLOPE-TIME", "TRIG-OVERTIME-NEG", "TRIG-OVERTIME-TIME",
}
# TRIG-MODE is accepted but ignored by 0x11 -> use DSO5000P.set_trigger_mode().

# --- enumerations ----------------------------------------------------------
VOLTS_PER_DIV = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0]
MIN_VB_INDEX = 1  # 1 mV/div is not offered on the DSO5102P
COUPLING = ["DC", "AC", "GND"]
PROBE = [1, 10, 100, 1000]
TIMEBASE = [m * 10.0 ** e for e in range(-9, 2) for m in (2, 4, 8)]
TIMEBASE = [round(t, 12) for t in TIMEBASE if t <= 40.0]  # 2 ns .. 40 s, 32 steps
# Trigger enums, in the order of the scope's trigger menu [HW]
TRIG_TYPE = ["Edge", "Video", "Pulse", "Slope", "Overtime", "Alternate"]
TRIG_MODE = ["Auto", "Normal"]
TRIG_SRC = ["CH1", "CH2", "EXT", "EXT/5", "AC50"]
TRIG_COUP = ["DC", "AC", "Noise reject", "HF reject", "LF reject"]
EDGE_SLOPE = ["Rising", "Falling"]
TRIG_STATE = {0: "Stop", 1: "Ready", 2: "Auto", 3: "Trig'd", 4: "Scan", 5: "Stop", 6: "Armed"}
TRIG_STATE_VERIFIED = {0, 1, 2, 3}  # others inferred from firmware table, not observed
TRIGGER_MENU_ID = 5  # CONTROL-MENUID while the trigger menu is shown
TRIGGER_MENU_MODE_KEY = "FN-4-KEY"  # soft keys FN-1..5 = type, source, slope, mode, coupling
# Acquire menu [HW]. Depth wire values: 0 = 4K, 2 = 20K, 4 = 40K (40K needs a
# single channel). The 512K/1M/2M menu entries are disabled unconditionally by
# firmware 3.40 and can't be selected via the protocol either (see PROTOCOL.md).
# USB waveform reads return the on-screen record: 200 / 400 / 800 points/div.
STORE_DEPTH = {0: "4K", 2: "20K", 4: "40K"}
DEEP_DEPTH = 255  # read back while 512K/1M is selected (patched firmware only)
ACQ_MODE = ["Normal", "Peak detect", "Average"]
AVERAGES = [4, 8, 16, 32, 64, 128, 256]
ACQUIRE_MENU_ID = 17

# Extra trigger parameters, in scope-menu order [HW]
VIDEO_POLARITY = ["Normal", "Inverted"]
VIDEO_STANDARD = ["NTSC", "PAL/SECAM"]
VIDEO_SYNC = ["All lines", "Line number", "Odd field", "Even field", "All fields"]
PULSE_POLARITY = ["Positive", "Negative"]
WHEN = ["=", "≠", ">", "<"]


def fmt_si(value: float, unit: str) -> str:
    if value == 0:
        return f"0{unit}"
    v = abs(value)
    for scale, prefix in ((1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if v >= scale * 0.9999 or scale == 1e-9:
            if v >= 1000 and scale == 1:
                return f"{value / 1000:g}k{unit}"
            return f"{value / scale:g}{prefix}{unit}"
    return f"{value:g}{unit}"


def fmt_fixed(value: float | None, unit: str, width: int = 9, sig: int = 4) -> str:
    """Fixed-width engineering format for live readouts, e.g. ' 5.800 V',
    '-200.0mV', ' 1.000kHz'. Always 4 significant digits, so a column of
    readings doesn't shift as values change (use sig=3 for two-character units
    such as V² to keep the same width). None/NaN -> a dash."""
    if value is None or value != value:
        return "—".rjust(width)
    prefixes = ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, " "), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"))
    v = abs(value)
    scale, p = 1, " "
    if v > 0:
        for scale, p in prefixes:
            if v >= scale * 0.99995:
                break
    x = value / scale
    lim = 10 ** -(sig - 1) / 2  # rounding threshold for sig digits
    digits = sig - 1 if abs(x) < 10 - lim * 10 else sig - 2 if abs(x) < 100 - lim * 100 else sig - 3
    digits = max(digits, 0)
    return f"{x:.{digits}f}{p}{unit}".rjust(width)


class Settings:
    """Decoded settings block with dict-style access by field name."""

    def __init__(self, raw: bytes, layout: list[Field] | None = None):
        self.raw = bytes(raw)
        self.layout = layout or LAYOUT
        self.values: dict[str, int] = {}
        for f in self.layout:
            chunk = self.raw[f.offset:f.offset + f.size]
            if len(chunk) < f.size:
                break
            self.values[f.name] = int.from_bytes(chunk, "little", signed=f.size in (2, 8))

    def __getitem__(self, name: str) -> int:
        return self.values[name]

    def get(self, name: str, default=None):
        return self.values.get(name, default)

    def encode(self, **changes: int) -> bytes:
        buf = bytearray(self.raw)
        names = {f.name: f for f in self.layout}
        for k, v in changes.items():
            f = names[k]
            buf[f.offset:f.offset + f.size] = int(v).to_bytes(f.size, "little", signed=f.size in (2, 8))
        return bytes(buf)

    # convenience -------------------------------------------------------
    def channel_on(self, ch: int) -> bool:
        return bool(self.get(f"VERT-CH{ch}-DISP", 0))

    def probe(self, ch: int) -> int:
        return PROBE[min(max(self.get(f"VERT-CH{ch}-PROBE", 0), 0), 3)]

    def volts_per_div(self, ch: int) -> float | None:
        i = self.get(f"VERT-CH{ch}-VB")
        if i is None or not 0 <= i < len(VOLTS_PER_DIV):
            return None
        return VOLTS_PER_DIV[i] * self.probe(ch)

    def position(self, ch: int) -> int:
        return self.get(f"VERT-CH{ch}-POS", 0)

    def seconds_per_div(self) -> float | None:
        i = self.get("HORIZ-TB")
        if i is None or not 0 <= i < len(TIMEBASE):
            return None
        return TIMEBASE[i]

    def to_volts(self, ch: int, samples):
        """Convert int8 sample array of channel ch (1/2) to volts."""
        import numpy as np
        vdiv = self.volts_per_div(ch) or 1.0
        return (np.asarray(samples, dtype=float) - self.position(ch)) * vdiv / COUNTS_PER_DIV

    def screen_divs(self) -> float:
        """Width of the scope's waveform area in divisions: 16 with the side
        menu shown, 19.2 with it hidden (the plot widens) [HW]."""
        return HORIZ_DIVS if self.get("CONTROL-DISP-MENU", 1) == 1 else HORIZ_DIVS * 1.2

    def points_per_div(self, n: int | None = None) -> float:
        """Samples per division of a USB waveform record of n points.

        The record always spans the scope's screen, so this is n / screen
        width. (It is 200 / 400 / 800 at 4K / 20K / 40K depth on slower
        timebases, but much less on fast ones, e.g. 640 points at 2 µs/div.)"""
        if n:
            return n / self.screen_divs()
        return POINTS_PER_DIV_BY_DEPTH.get(self.get("ACQURIE-STORE-DEPTH", 0), POINTS_PER_DIV)

    def sample_interval(self, n: int | None = None) -> float:
        """Seconds between samples of a USB waveform record of n points."""
        return (self.seconds_per_div() or 1.0) / self.points_per_div(n)

    def x_divs(self, n: int):
        """Horizontal position of each sample in screen divisions (0 = screen
        centre); the record spans the whole screen (±8 or ±9.6 div)."""
        import numpy as np
        return (np.arange(n) - n / 2) / self.points_per_div(n)

    def time_axis(self, n: int):
        """Time of each sample relative to the trigger point. The screen centre
        is at +HORIZ-TRIGTIME (the horizontal position)."""
        return (self.x_divs(n) * (self.seconds_per_div() or 1.0)
                + self.get("HORIZ-TRIGTIME", 0) * 1e-12)

    def trigger_channel(self) -> int | None:
        src = self.get("TRIG-SRC", 0)
        return src + 1 if src in (0, 1) else None

    def counts_to_volts(self, ch: int, counts: int) -> float:
        """Screen counts (trigger level, slope thresholds) -> volts on channel ch."""
        return (counts - self.position(ch)) * (self.volts_per_div(ch) or 0) / COUNTS_PER_DIV

    def volts_to_counts(self, ch: int, volts: float) -> int:
        vdiv = self.volts_per_div(ch) or 1.0
        return max(-250, min(250, int(round(volts * COUNTS_PER_DIV / vdiv + self.position(ch)))))

    def trigger_level_volts(self) -> float | None:
        """Trigger level in volts for CH1/CH2 sources (VPOS is in screen counts
        of the source channel, relative to that channel's ground marker)."""
        src = self.get("TRIG-SRC", 0)
        if src not in (0, 1):
            return None
        ch = src + 1
        return (self["TRIG-VPOS"] - self.position(ch)) * (self.volts_per_div(ch) or 0) / COUNTS_PER_DIV

    def trigger_frequency(self) -> float:
        """Hardware frequency counter, Hz (field is millihertz)."""
        return self.get("TRIG-FREQUENCY", 0) / 1000.0

    def trigger_state(self) -> str:
        v = self.get("TRIG-STATE", -1)
        return TRIG_STATE.get(v, f"state {v}")

    def as_text(self) -> str:
        return "\n".join(f"{k:28s} {v}" for k, v in self.values.items())
