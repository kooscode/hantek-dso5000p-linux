#!/usr/bin/env python3
"""scope_logger - a small example app built on the hantek_dso API.

Every few seconds it reads both channels from one acquisition, measures them
on the PC, and prints a live table. Optionally it also logs each row to a CSV
file and saves the raw waveforms of every sample.

    python api/scope_logger.py                       # print every 2 s until Ctrl+C
    python api/scope_logger.py -i 1 -n 30 -o log.csv  # 30 samples, 1 s apart, to CSV
    python api/scope_logger.py --waveforms waves/      # also save each capture as CSV

Run it from the repository root (or after `pip install -e .`).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hantek_dso import DSO5000P, ScopeError  # noqa: E402
from hantek_dso.settings import fmt_si  # noqa: E402


def stats(t: np.ndarray, v: np.ndarray) -> dict[str, float]:
    """Numeric measurements of one trace (volts, hertz)."""
    vmax, vmin = float(v.max()), float(v.min())
    out = {"vpp": vmax - vmin, "vmax": vmax, "vmin": vmin,
           "mean": float(v.mean()), "rms": float(np.sqrt(np.mean(v * v))), "freq": float("nan")}
    # frequency from rising crossings of the mid level (10 % hysteresis)
    mid, hyst = (vmax + vmin) / 2, (vmax - vmin) * 0.1
    if vmax - vmin > 0:
        high = v > mid + hyst
        low = v < mid - hyst
        rises, state = [], None
        for i in range(len(v)):
            if high[i] and state is False:
                rises.append(i)
            if high[i]:
                state = True
            elif low[i]:
                state = False
        if len(rises) >= 2:
            out["freq"] = (len(rises) - 1) / (t[rises[-1]] - t[rises[0]])
    return out


def capture(scope: DSO5000P):
    """One acquisition of every enabled channel -> (settings, {ch: (t, volts)})."""
    st = scope.read_settings()
    traces = {}
    for w in scope.read_waveform(4):          # CH1, CH2 (and scope math) together
        ch = w.source + 1
        if ch in (1, 2) and w.samples:
            a = w.as_int8()
            traces[ch] = (st.time_axis(len(a)), st.to_volts(ch, a))
    return st, traces


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Log Hantek DSO5000P measurements.")
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="seconds between samples")
    ap.add_argument("-n", "--count", type=int, default=0, help="number of samples (0 = until Ctrl+C)")
    ap.add_argument("-o", "--output", help="append rows to this CSV file")
    ap.add_argument("--waveforms", metavar="DIR", help="also save each capture's waveforms here")
    args = ap.parse_args(argv)

    if args.waveforms:
        os.makedirs(args.waveforms, exist_ok=True)
    columns = ["time", "state", "counter_hz"] + [
        f"ch{c}_{k}" for c in (1, 2) for k in ("vpp", "vmax", "vmin", "mean", "rms", "freq")]
    writer = None
    if args.output:
        new = not os.path.exists(args.output)
        fh = open(args.output, "a", newline="")
        writer = csv.DictWriter(fh, fieldnames=columns)
        if new:
            writer.writeheader()

    print(f"{'time':8}  {'state':7}  {'counter':>10}  {'CH1 Vpp':>9}  {'CH1 freq':>10}  "
          f"{'CH2 Vpp':>9}  {'CH2 freq':>10}")
    with DSO5000P() as scope:
        n = 0
        try:
            while args.count == 0 or n < args.count:
                t0 = time.monotonic()
                stamp = time.strftime("%H:%M:%S")
                try:
                    st, traces = capture(scope)
                except ScopeError as e:  # e.g. Normal trigger mode with no trigger yet
                    print(f"{stamp}  no data ({e})")
                    time.sleep(args.interval)
                    continue
                row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "state": st.trigger_state(),
                       "counter_hz": st.trigger_frequency()}
                shown = []
                for ch in (1, 2):
                    if ch in traces:
                        s = stats(*traces[ch])
                        row.update({f"ch{ch}_{k}": round(v, 6) for k, v in s.items()})
                        f = fmt_si(s["freq"], "Hz") if s["freq"] == s["freq"] else "—"
                        shown += [fmt_si(round(s["vpp"], 4), "V"), f]
                    else:
                        shown += ["off", ""]
                print(f"{stamp:8}  {row['state']:7}  {fmt_si(row['counter_hz'], 'Hz'):>10}  "
                      f"{shown[0]:>9}  {shown[1]:>10}  {shown[2]:>9}  {shown[3]:>10}")
                if writer:
                    writer.writerow(row)
                    fh.flush()
                if args.waveforms:
                    t = next(iter(traces.values()))[0]
                    cols = [t] + [traces[c][1] for c in sorted(traces)]
                    np.savetxt(os.path.join(args.waveforms, f"capture_{n:05d}_{time.strftime('%Y%m%d_%H%M%S')}.csv"),
                               np.column_stack(cols), delimiter=",", comments="", fmt="%.9g",
                               header="time_s," + ",".join(f"CH{c}_V" for c in sorted(traces)))
                n += 1
                time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            pass
    if writer:
        fh.close()
        print(f"logged {n} rows to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
