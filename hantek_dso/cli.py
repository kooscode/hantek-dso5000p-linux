"""Command line front-end: ``python -m hantek_dso <command>``."""

from __future__ import annotations

import argparse
import sys

from . import protocol as P
from .scope import DSO5000P, SCREEN_H, SCREEN_W
from .settings import KEYS


def _scope(args) -> DSO5000P:
    return DSO5000P(debug=args.debug, device=args.device)


def cmd_devices(args):
    from .transport import list_devices
    devs = list_devices()
    if not devs:
        print("no DSO5000P (049f:505a) found")
        return
    print(f"{'port':14s} {'bus:addr':9s} product")
    for d in devs:
        print(f"{d['port']:14s} {d['bus']}:{d['address']:<7} {d['product']}")


def cmd_info(args):
    s = _scope(args)
    print("echo:", s.echo(b"\x12\x34").hex())
    st = s.read_settings()
    print(f"settings block: {len(st.raw)} bytes")
    for ch in (1, 2):
        print(f"CH{ch}: on={st.channel_on(ch)} V/div={st.volts_per_div(ch)} pos={st.position(ch)}")
    print("timebase s/div:", st.seconds_per_div(), "(index", st.get("HORIZ-TB"), ")")


def cmd_settings(args):
    s = _scope(args)
    st = s.read_settings()
    if args.raw:
        sys.stdout.buffer.write(st.raw)
    else:
        print(st.as_text())


def cmd_set(args):
    s = _scope(args)
    changes = {}
    for kv in args.assign:
        k, _, v = kv.partition("=")
        changes[k.upper()] = int(v, 0)
    print("ok" if s.set(**changes) else "rejected")


def cmd_wave(args):
    import numpy as np
    s = _scope(args)
    src = {"ch1": P.SRC_CH1, "ch2": P.SRC_CH2, "math": P.SRC_MATH, "all": P.SRC_ALL}[args.source]
    st = s.read_settings()
    waves = s.read_waveform(src)
    for w in waves:
        a = w.as_int8()
        name = {0: "CH1", 1: "CH2", 2: "MATH(scope)"}.get(w.source, str(w.source))
        print(f"{name}: {len(a)} samples (declared {w.declared_length}) "
              f"min={a.min() if len(a) else '-'} max={a.max() if len(a) else '-'}")
    if args.csv and waves:
        n = min(len(w.samples) for w in waves)
        cols, hdr = [st.time_axis(n)], ["time_s"]
        for w in waves:
            ch = w.source + 1
            a = w.as_int8()[:n]
            if ch in (1, 2) and not args.counts:
                cols.append(st.to_volts(ch, a))
                hdr.append(f"CH{ch}_V")
            else:
                cols.append(a.astype(float))
                hdr.append(f"SRC{w.source}_counts")
        np.savetxt(args.csv, np.column_stack(cols), delimiter=",", header=",".join(hdr),
                   comments="", fmt="%.9g")
        print("wrote", args.csv)


def cmd_screenshot(args):
    s = _scope(args)
    img = s.screenshot()
    try:
        from PySide6.QtGui import QImage
        q = QImage(img.tobytes(), SCREEN_W, SCREEN_H, SCREEN_W * 3, QImage.Format_RGB888)
        q.save(args.output)
    except ImportError:
        import zlib, struct
        raw = b"".join(b"\x00" + img[y].tobytes() for y in range(SCREEN_H))
        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", SCREEN_W, SCREEN_H, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
        open(args.output, "wb").write(png)
    print("wrote", args.output)


def cmd_key(args):
    s = _scope(args)
    if args.key == "list":
        for k, v in KEYS.items():
            print(f"{v:3d} {k}")
        return
    key = int(args.key) if args.key.isdigit() else args.key.upper()
    print("reply:", s.press_key(key))


def cmd_lock(args):
    s = _scope(args)
    s.lock_panel(args.state == "on")


def cmd_file(args):
    s = _scope(args)
    data = s.read_file(args.path)
    if args.output:
        open(args.output, "wb").write(data)
        print(f"wrote {len(data)} bytes to {args.output}")
    else:
        sys.stdout.buffer.write(data)


def cmd_full(args):
    import numpy as np
    from .analysis import measure
    from .settings import fmt_si
    s = _scope(args)
    ch = int(args.channel[-1])
    rec = s.capture_full(ch, single=not args.current, keep_file=args.keep,
                         progress=lambda m: print(f"  {m}", file=sys.stderr))
    print(f"CH{ch}: {len(rec.t)} points, {fmt_si(rec.sample_rate, 'Sa/s')}, "
          f"span {fmt_si(rec.t[-1] - rec.t[0], 's')}")
    print("  " + "  ".join(f"{k}={v}" for k, v in measure(rec.t, rec.volts).items()))
    if args.output:
        np.savetxt(args.output, np.column_stack([rec.t, rec.volts]), delimiter=",",
                   header=f"time_s,CH{ch}_V", comments="", fmt="%.9g")
        print("wrote", args.output)


def cmd_raw(args):
    s = _scope(args)
    payload = bytes.fromhex(args.payload) if args.payload else b""
    s.send(int(args.cmd, 0), payload)
    import time
    end = time.monotonic() + args.wait
    while time.monotonic() < end:
        try:
            print(s.recv(max(0.05, end - time.monotonic())))
        except Exception:
            break


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hantek-dso", description="Hantek DSO5000P USB client")
    ap.add_argument("--debug", action="store_true", help="hex-dump USB traffic")
    ap.add_argument("--device", "-d", metavar="PORT",
                    help="which scope to use when several are connected: USB port path "
                         "(e.g. 1-5.2.4.3, see 'devices') or bus:address; default: first found")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="list connected scopes").set_defaults(fn=cmd_devices)
    sub.add_parser("info", help="ping scope and show a settings summary").set_defaults(fn=cmd_info)
    p = sub.add_parser("settings", help="dump all settings fields")
    p.add_argument("--raw", action="store_true", help="write raw block to stdout")
    p.set_defaults(fn=cmd_settings)
    p = sub.add_parser("set", help="change settings fields, e.g. HORIZ-TB=12")
    p.add_argument("assign", nargs="+")
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser("wave", help="read waveform data")
    p.add_argument("source", choices=["ch1", "ch2", "math", "all"], nargs="?", default="ch1")
    p.add_argument("--csv", help="save to CSV")
    p.add_argument("--counts", action="store_true", help="raw ADC counts instead of volts")
    p.set_defaults(fn=cmd_wave)
    p = sub.add_parser("screenshot", help="save screen as PNG")
    p.add_argument("output", nargs="?", default="screen.png")
    p.set_defaults(fn=cmd_screenshot)
    p = sub.add_parser("key", help="press a front-panel key (name, number, or 'list')")
    p.add_argument("key")
    p.set_defaults(fn=cmd_key)
    p = sub.add_parser("lock", help="lock/unlock front panel")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(fn=cmd_lock)
    p = sub.add_parser("file", help="download a file from the scope filesystem")
    p.add_argument("path")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_file)
    p = sub.add_parser("full", help="capture a full record (up to 40 000 points) via the "
                                    "scope's CSV export; needs a USB stick in the scope")
    p.add_argument("channel", choices=["ch1", "ch2"], nargs="?", default="ch1")
    p.add_argument("-o", "--output", help="save to CSV (time_s, volts)")
    p.add_argument("--current", action="store_true",
                   help="save the current acquisition instead of taking a single shot")
    p.add_argument("--keep", action="store_true", help="leave the scope's CSV on the stick")
    p.set_defaults(fn=cmd_full)
    p = sub.add_parser("raw", help="send a raw command and print replies")
    p.add_argument("cmd")
    p.add_argument("payload", nargs="?", default="")
    p.add_argument("--wait", type=float, default=2.0)
    p.set_defaults(fn=cmd_raw)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
