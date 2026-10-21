"""Host-side signal processing: math channel, FFT, measurements, references."""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np

from .settings import fmt_si

MATH_OPS = ["Off", "CH1 + CH2", "CH1 − CH2", "CH2 − CH1", "CH1 × CH2", "FFT CH1", "FFT CH2"]
FFT_WINDOWS = ["Rectangular", "Hanning", "Hamming", "Blackman", "Flat top"]


def window(name: str, n: int) -> np.ndarray:
    if name == "Hanning":
        return np.hanning(n)
    if name == "Hamming":
        return np.hamming(n)
    if name == "Blackman":
        return np.blackman(n)
    if name == "Flat top":
        k = np.arange(n) / max(n - 1, 1) * 2 * np.pi
        return (0.21557895 - 0.41663158 * np.cos(k) + 0.277263158 * np.cos(2 * k)
                - 0.083578947 * np.cos(3 * k) + 0.006947368 * np.cos(4 * k))
    return np.ones(n)


def math_channel(op: str, v1: np.ndarray | None, v2: np.ndarray | None) -> np.ndarray | None:
    """Arithmetic math trace in volts (V² for multiply)."""
    if v1 is None or v2 is None:
        return None
    n = min(len(v1), len(v2))
    a, b = v1[:n], v2[:n]
    return {"CH1 + CH2": a + b, "CH1 − CH2": a - b, "CH2 − CH1": b - a,
            "CH1 × CH2": a * b}.get(op)


def fft(v: np.ndarray, dt: float, win: str = "Hanning", db: bool = True):
    """Single-sided amplitude spectrum. Returns (freq Hz, dBV RMS or V RMS)."""
    n = len(v)
    if n < 8 or dt <= 0:
        return np.array([]), np.array([])
    w = window(win, n)
    spec = np.fft.rfft((v - v.mean()) * w)
    amp = np.abs(spec) / (w.sum() / 2) / np.sqrt(2)  # RMS amplitude of each bin
    amp[0] = abs(spec[0]) / w.sum()
    f = np.fft.rfftfreq(n, dt)
    if db:
        return f, 20 * np.log10(np.maximum(amp, 1e-12))
    return f, amp


def peak(f: np.ndarray, a: np.ndarray) -> tuple[float, float]:
    """Largest non-DC spectral peak, refined by parabolic interpolation."""
    i = int(np.argmax(a[1:])) + 1
    if 1 <= i < len(a) - 1:
        y0, y1, y2 = a[i - 1], a[i], a[i + 1]
        d = y0 - 2 * y1 + y2
        if d:
            p = 0.5 * (y0 - y2) / d
            return float(f[i] + p * (f[1] - f[0])), float(y1 - 0.25 * (y0 - y2) * p)
    return float(f[i]), float(a[i])


def measure_values(t: np.ndarray, v: np.ndarray) -> dict[str, float]:
    """Scope-style automatic measurements as numbers (volts, seconds, hertz,
    percent). Keys: vpp, vmax, vmin, mean, rms, and - when the trace is a
    clean periodic signal - freq, period, width, duty, rise."""
    if len(v) < 4:
        return {}
    vmax, vmin = float(v.max()), float(v.min())
    out = {"vpp": vmax - vmin, "vmax": vmax, "vmin": vmin,
           "mean": float(v.mean()), "rms": float(np.sqrt(np.mean(v * v)))}
    if vmax - vmin <= 0:
        return out
    mid = (vmax + vmin) / 2
    hyst = (vmax - vmin) * 0.1
    # rising/falling crossings with hysteresis
    state, rises, falls = None, [], []
    above = v > mid + hyst
    below = v < mid - hyst
    for i in range(len(v)):
        if above[i] and state is not True:
            if state is False:
                rises.append(i)
            state = True
        elif below[i] and state is not False:
            if state is True:
                falls.append(i)
            state = False
    period = None
    if len(rises) >= 2:
        period = (t[rises[-1]] - t[rises[0]]) / (len(rises) - 1)
        if period > 0:
            out["freq"] = 1 / period
            out["period"] = period
            # pulse width / duty cycle from the first full cycle
            r0 = rises[0]
            f_after = [f for f in falls if f > r0]
            if f_after:
                width = t[f_after[0]] - t[r0]
                if 0 < width < period:  # otherwise the trace isn't a clean pulse train
                    out["width"] = width
                    out["duty"] = 100 * width / period
        else:
            period = None
    # 10-90 % rise time of the first rising edge
    lo, hi = vmin + 0.1 * (vmax - vmin), vmin + 0.9 * (vmax - vmin)
    if rises:
        j0 = rises[0]
        while j0 > 0 and v[j0] > lo:
            j0 -= 1
        j1 = j0
        while j1 < len(v) - 1 and v[j1] < hi:
            j1 += 1
        rise = t[j1] - t[j0]
        if j1 > j0 and (period is None or rise < period / 2):
            out["rise"] = rise
    return out


def measure(t: np.ndarray, v: np.ndarray, unit: str = "V") -> dict[str, str]:
    """Same as measure_values(), formatted for display."""
    m = measure_values(t, v)
    names = {"vpp": ("Vpp", unit), "vmax": ("Vmax", unit), "vmin": ("Vmin", unit),
             "mean": ("Mean", unit), "rms": ("RMS", unit), "freq": ("Freq", "Hz"),
             "period": ("Period", "s"), "width": ("+Width", "s"), "rise": ("Rise", "s")}
    out = {}
    for k, val in m.items():
        if k == "duty":
            out["Duty"] = f"{val:.1f}%"
        else:
            label, u = names[k]
            out[label] = fmt_si(val, u)
    return out


@dataclass
class Reference:
    """A stored waveform, kept in screen divisions with the scale it had."""
    name: str
    t: np.ndarray            # seconds
    v: np.ndarray            # volts
    volts_per_div: float
    seconds_per_div: float
    offset_div: float = 0.0  # where 0 V sat on screen when captured

    def to_divs(self) -> tuple[np.ndarray, np.ndarray]:
        return self.t / self.seconds_per_div, self.v / self.volts_per_div + self.offset_div

    def save_csv(self, path: str) -> None:
        meta = {"name": self.name, "volts_per_div": self.volts_per_div,
                "seconds_per_div": self.seconds_per_div, "offset_div": self.offset_div}
        hdr = "# hantek-dso reference " + json.dumps(meta) + "\ntime_s,volts"
        np.savetxt(path, np.column_stack([self.t, self.v]), delimiter=",", header=hdr,
                   comments="", fmt="%.9g")

    @classmethod
    def load_csv(cls, path: str, default_vdiv: float = 1.0, default_tdiv: float = 1e-3) -> "Reference":
        meta = {}
        skip = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("# hantek-dso reference "):
                    meta = json.loads(line.split("reference ", 1)[1])
                    skip += 1
                elif line[:1].isalpha() or line.startswith("#"):
                    skip += 1
                else:
                    break
        data = np.loadtxt(path, delimiter=",", skiprows=skip, ndmin=2)
        t, v = data[:, 0], data[:, 1]
        if not meta:
            # plain CSV (e.g. from `hantek-dso wave --csv`): auto-scale to fit
            span = float(np.ptp(v)) or 1.0
            meta = {"volts_per_div": span / 6, "seconds_per_div": (float(np.ptp(t)) or default_tdiv) / 16,
                    "offset_div": -float(v.mean()) / (span / 6)}
        return cls(meta.get("name", path.rsplit("/", 1)[-1]), t, v,
                   float(meta.get("volts_per_div", default_vdiv)),
                   float(meta.get("seconds_per_div", default_tdiv)),
                   float(meta.get("offset_div", 0.0)))
