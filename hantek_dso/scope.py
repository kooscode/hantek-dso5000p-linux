"""High-level client for the Hantek DSO5000P series over USB."""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import protocol as P
from .settings import (ACQUIRE_MENU_ID, KEYS, LAYOUT_SIZE, SAFE_WRITE_FIELDS, TRIGGER_MENU_ID,
                       TRIGGER_MENU_MODE_KEY, Settings)
from .transport import UsbTransport

SCREEN_W, SCREEN_H = 800, 480


class ScopeError(Exception):
    pass


SR_MENU_ID = 47      # Save/Recall menu
CSV_MENU_ID = 48     # Save/Recall > CSV submenu


@dataclass
class FullRecord:
    """A complete acquisition record saved by the scope's CSV export."""
    channel: int            # 1 or 2
    t: "object"             # numpy array, seconds from the start of the record
    volts: "object"         # numpy array
    timebase: float         # s/div when captured
    voltbase: float         # V/div when captured (as written by the scope)
    filename: str           # name it had on the USB stick

    @property
    def sample_rate(self) -> float:
        return 1.0 / (self.t[1] - self.t[0]) if len(self.t) > 1 else 0.0


@dataclass
class Waveform:
    """One channel's record. source: 0 CH1, 1 CH2, 2 scope math."""
    source: int
    samples: bytes  # signed 8-bit, screen units (25 per division)
    declared_length: int = 0

    def as_int8(self):
        import numpy as np
        return np.frombuffer(self.samples, dtype=np.int8)


class DSO5000P:
    def __init__(self, transport: UsbTransport | None = None, debug: bool = False,
                 device: str | None = None):
        """device: which scope to open when several are connected - a USB port
        path like "1-5.2.4.3", "bus:address", or None/"auto" for the first one
        (see transport.list_devices())."""
        self.t = transport or UsbTransport(device)
        self.debug = debug
        self.deframer = P.Deframer()
        self._pending: list[P.Packet] = []
        self.min_gap = 0.08
        self._last_io = 0.0
        self.t.drain(50)
        self.sync()

    def sync(self, attempts: int = 10) -> None:
        """The first frame after claiming the interface is often lost; ping
        until the scope answers so later requests start from a clean state."""
        for i in range(attempts):
            try:
                if self._request_once(P.CMD_ECHO, bytes([0xA5, i]), 0.8, P.MARK_NORMAL).payload == bytes([0xA5, i]):
                    return
            except ScopeError:
                self.t.drain(50)
        raise ScopeError("scope does not answer (is it running / cable on the rear USB device port?)")

    # -- low level ------------------------------------------------------
    def send(self, cmd: int, payload: bytes = b"", marker: int = P.MARK_NORMAL) -> None:
        frame = P.build(cmd, payload, marker)
        # The firmware polls /dev/ttyGS0 and treats a pause as end-of-request;
        # back-to-back frames get merged and rejected. Windows app waits 80 ms.
        gap = self.min_gap - (time.monotonic() - self._last_io)
        if gap > 0:
            time.sleep(gap)
        if self.debug:
            print(">>", frame.hex(" "))
        self.t.write(frame)
        self._last_io = time.monotonic()

    def recv(self, timeout: float = 2.0) -> P.Packet:
        deadline = time.monotonic() + timeout
        while not self._pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScopeError("timeout waiting for reply")
            data = self.t.read(65536, max(1, int(min(remaining, 0.5) * 1000)))
            if data:
                if self.debug:
                    print("<<", len(data), data[:32].hex(" "))
                self._pending.extend(self.deframer.feed(data))
                self._last_io = time.monotonic()
        return self._pending.pop(0)

    def request(self, cmd: int, payload: bytes = b"", timeout: float = 2.0,
                marker: int = P.MARK_NORMAL, retries: int = 2) -> P.Packet:
        for attempt in range(retries + 1):
            try:
                return self._request_once(cmd, payload, timeout, marker)
            except ScopeError:
                if attempt == retries:
                    raise
                self.t.drain(100)
        raise AssertionError

    def _request_once(self, cmd, payload, timeout, marker) -> P.Packet:
        self.send(cmd, payload, marker)
        deadline = time.monotonic() + timeout
        while True:
            pkt = self.recv(max(0.05, deadline - time.monotonic()))
            if pkt.request_cmd == cmd or pkt.cmd == 0x92 and cmd in (P.CMD_READ_WAVE, P.CMD_LOCK):
                return pkt
            if self.debug:
                print("ignoring", pkt)

    def _chunked(self, cmd: int, payload: bytes, timeout: float = 10.0,
                 first_timeout: float = 2.0, retries: int = 2, strict: bool = True) -> bytes:
        """Requests answered as SUB_DATA chunks then SUB_END <sum8> (0x10, 0x20)."""
        for attempt in range(retries + 1):
            self.send(cmd, payload)
            data = bytearray()
            got_any = False
            deadline = time.monotonic() + first_timeout
            try:
                while True:
                    pkt = self.recv(max(0.05, deadline - time.monotonic()))
                    if pkt.request_cmd != cmd or not pkt.payload:
                        continue
                    got_any = True
                    deadline = time.monotonic() + timeout
                    sub = pkt.payload[0]
                    if sub == P.SUB_DATA:
                        data += pkt.payload[1:]
                    elif sub == P.SUB_END:
                        want = pkt.payload[1] if len(pkt.payload) > 1 else None
                        if strict and want is not None and (sum(data) & 0xFF) != want:
                            raise ScopeError("transfer checksum mismatch")
                        return bytes(data)
            except ScopeError:
                if got_any or attempt == retries:
                    raise
                self.t.drain(100)
        raise AssertionError

    # -- commands -------------------------------------------------------
    def echo(self, data: bytes = b"\x55\xaa") -> bytes:
        return self.request(P.CMD_ECHO, data).payload

    def read_settings_raw(self) -> bytes:
        return self.request(P.CMD_READ_SETTINGS).payload

    def read_settings(self) -> Settings:
        raw = self.read_settings_raw()
        if len(raw) != LAYOUT_SIZE:
            # Layout differs from bundled protocol.inf; still decode what fits.
            pass
        return Settings(raw)

    def write_settings(self, raw: bytes) -> bool:
        """Apply a full settings block. Scope replies 0x00 ok / 0xff rejected."""
        depth_field = next(f for f in Settings(raw).layout if f.name == "ACQURIE-STORE-DEPTH")
        depth = raw[depth_field.offset]
        if depth not in (0, 2, 4, 6, 7):
            # Deep memory (512K/1M, only selectable with the patched firmware)
            # reads back as 255. Writing an unmapped depth value back is what
            # crashed the scope once, so don't risk it.
            raise ScopeError("settings writes are disabled while a deep memory depth "
                             "(512K/1M) is selected; switch to 4K/20K/40K first")
        rep = self.request(P.CMD_WRITE_SETTINGS, raw, timeout=5.0, retries=0)
        ok = bool(rep.payload) and rep.payload[0] == 0
        # The scope restarts its acquisition threads after applying settings
        # and ignores requests for a moment.
        time.sleep(0.3)
        self.t.drain(50)
        self.sync(10)
        return ok

    def set(self, force: bool = False, **changes: int) -> bool:
        """Change settings fields via command 0x11.

        Only fields in SAFE_WRITE_FIELDS are allowed unless force=True: other
        fields are either ignored by the firmware or (memory depth) crash it.
        """
        bad = [k for k in changes if k not in SAFE_WRITE_FIELDS]
        if bad and not force:
            raise ScopeError(f"refusing to write {bad}: not verified safe (use force=True or keys)")
        s = self.read_settings()
        return self.write_settings(s.encode(**changes))

    def _wait_change(self, field: str, old: int, timeout: float = 1.0) -> int:
        """Poll a field until it differs from old (the scope updates the
        settings block a little after a key press) or the timeout expires."""
        end = time.monotonic() + timeout
        cur = old
        while time.monotonic() < end:
            time.sleep(0.1)
            cur = self.read_settings()[field]
            if cur != old:
                break
        return cur

    def step_to(self, field: str, target: int, key_up: str, key_down: str,
                max_steps: int = 40) -> int:
        """Drive an indexed setting to target with front-panel keys, waiting
        for each press to show up in the read-back before the next one."""
        cur = self.read_settings()[field]
        for _ in range(max_steps):
            if cur == target:
                break
            key = key_up if target > cur else key_down
            self.press_key(key)
            new = self._wait_change(field, cur)
            if new == cur:  # dropped press or at the limit: one more try
                self.press_key(key)
                new = self._wait_change(field, cur)
                if new == cur:
                    break
            cur = new
        return cur

    def _menu_cycle(self, menu_key: str, menu_id: int, soft_key: str, field: str,
                    target: int, max_presses: int = 8) -> int:
        """Open a menu and press one of its soft keys until field == target.
        For settings that command 0x11 ignores or that are unsafe to write."""
        st = self.read_settings()
        cur = st[field]
        if cur == target:
            return cur
        was_visible = st["CONTROL-DISP-MENU"] == 1
        self.open_menu(menu_key, menu_id)
        try:
            for _ in range(max_presses):
                self.press_key(soft_key)
                cur = self._wait_change(field, cur, 1.5)
                if cur == target:
                    break
        finally:
            self.set_menu_visible(was_visible)
        return cur

    def menu_visible(self) -> bool:
        return self.read_settings()["CONTROL-DISP-MENU"] == 1

    def set_menu_visible(self, on: bool, timeout: float = 1.5) -> bool:
        """Show or hide the side menu with FN-0 (it toggles the menu)."""
        if self.menu_visible() == on:
            return True
        for _ in range(2):
            self.press_key("FN-0-KEY")
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                time.sleep(0.15)
                if self.menu_visible() == on:
                    return True
        return False

    def close_menu(self) -> bool:
        return self.set_menu_visible(False)

    def open_menu(self, menu_key: str, menu_id: int, timeout: float = 2.0) -> None:
        """Press a menu key and wait until CONTROL-MENUID shows that menu (the
        scope can take a while, e.g. after closing a dialog)."""
        self.press_key(menu_key)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            time.sleep(0.15)
            if self.read_settings()["CONTROL-MENUID"] == menu_id:
                return
        raise ScopeError(f"{menu_key} did not open menu {menu_id}")

    def set_memory_depth(self, value: int) -> int:
        """ACQURIE-STORE-DEPTH: 0 = 4K, 2 = 20K, 4 = 40K (40K needs a single
        channel). Done via the Acquire menu: writing the field crashes the scope."""
        return self._menu_cycle("MENU-ACQUIRE-KEY", ACQUIRE_MENU_ID, "FN-5-KEY",
                                "ACQURIE-STORE-DEPTH", value, 4)

    def set_acquire_mode(self, mode: int) -> int:
        """0 = Normal (sample), 1 = Peak detect, 2 = Average."""
        return self._menu_cycle("MENU-ACQUIRE-KEY", ACQUIRE_MENU_ID, "FN-2-KEY",
                                "ACQURIE-MODE", mode, 4)

    def set_averages(self, index: int) -> int:
        """Index into AVERAGES (4 … 256). The count reads back as 0 unless the
        acquisition mode is Average, so it can only be set in that mode."""
        if self.read_settings()["ACQURIE-MODE"] != 2:
            raise ScopeError("averages can only be changed in Average mode")
        return self._menu_cycle("MENU-ACQUIRE-KEY", ACQUIRE_MENU_ID, "FN-3-KEY",
                                "ACQURIE-AVG-CNT", index, 8)

    def set_trigger_mode(self, mode: int) -> int:
        """0 = Auto, 1 = Normal. 0x11 ignores TRIG-MODE, so open the trigger
        menu (the key always opens it, never toggles) and press its Mode soft key."""
        st = self.read_settings()
        if st["TRIG-MODE"] == mode:
            return mode
        was_visible = st["CONTROL-DISP-MENU"] == 1
        self.open_menu("TG-MENU-KEY", TRIGGER_MENU_ID)
        try:
            for _ in range(2):
                self.press_key(TRIGGER_MENU_MODE_KEY)
                time.sleep(0.3)
                cur = self.read_settings()["TRIG-MODE"]
                if cur == mode:
                    break
        finally:
            self.set_menu_visible(was_visible)
        return cur

    def set_timebase(self, index: int) -> int:
        return self.step_to("HORIZ-TB", index, "HZ-TBADD-KEY", "HZ-TBSUB-KEY")

    def press_key(self, key: int | str) -> int:
        code = KEYS[key] if isinstance(key, str) else int(key)
        rep = self.request(P.CMD_KEY, bytes([code, 1]))
        return rep.payload[0] if rep.payload else -1

    def lock_panel(self, on: bool = True) -> None:
        self.request(P.CMD_LOCK, bytes([1, 1 if on else 0]))

    def remote_mode(self, on: bool = True) -> None:
        self.request(P.CMD_LOCK, bytes([0, 1 if on else 0]))

    def measure_text(self, item: int, source: int, mode: int = 0) -> str:
        rep = self.request(P.CMD_MEASURE, bytes([item, source, mode]))
        return rep.payload.decode("latin-1", "replace").strip("\x00")

    def read_file(self, path: str, timeout: float = 30.0) -> bytes:
        return self._chunked(P.CMD_READ_FILE, b"\x00" + path.encode(), timeout)

    def screenshot_raw(self, timeout: float = 30.0) -> bytes:
        # Firmware sums the live framebuffer after sending each chunk, so the
        # checksum legitimately mismatches whenever the screen redraws.
        return self._chunked(P.CMD_SCREENSHOT, b"", timeout, strict=False)

    def screenshot(self, timeout: float = 30.0):
        """Return the 800x480 screen as an (H, W, 3) uint8 numpy array."""
        import numpy as np
        raw = self.screenshot_raw(timeout)
        px = np.frombuffer(raw[: SCREEN_W * SCREEN_H * 2], dtype="<u2")
        px = px.reshape(SCREEN_H, SCREEN_W)
        r = ((px >> 11) & 0x1F) << 3
        g = ((px >> 5) & 0x3F) << 2
        b = (px & 0x1F) << 3
        return np.dstack([r, g, b]).astype(np.uint8)

    # Scope screen geometry (fitted against USB data, mean error 0.03 div):
    # 40 px per division horizontally, 50 px vertically; centre row 237. The plot is
    # 16 div wide centred at x=335 with the side menu shown, 19.2 div centred at
    # x=399 with it hidden.
    _SCREEN_CY = 237
    _SCREEN_PX_PER_DIV_X = 40.0
    _SCREEN_PX_PER_DIV_Y = 50.0
    _TRACE_COLOURS = {1: lambda r, g, b: (r > 170) & (g > 170) & (b < 90),     # yellow
                      2: lambda r, g, b: (r < 90) & (g > 170) & (b > 170)}     # cyan

    def screen_traces(self, st: Settings | None = None) -> dict[int, "object"]:
        """Extract the displayed CH1/CH2 traces from a screenshot.

        Fallback for when the scope won't send waveform data (it refuses while
        stopped, e.g. after a single shot). Returns {channel: float array} in
        the same units and density as read_waveform() samples (25 counts per
        division, centred on the screen), resampled from ~40 pixels per division.
        """
        import numpy as np
        st = st or self.read_settings()
        img = self.screenshot().astype(int)
        menu = st.get("CONTROL-DISP-MENU", 1) == 1
        cx, half_div = (335, 8.0) if menu else (399, 9.6)
        x0 = int(round(cx - half_div * self._SCREEN_PX_PER_DIV_X)) + 1
        x1 = int(round(cx + half_div * self._SCREEN_PX_PER_DIV_X))
        y0, y1 = 30, 446                              # plot rows (±4 div + margin)
        out = {}
        for ch in (1, 2):
            if not st.channel_on(ch):
                continue
            r, g, b = (img[y0:y1, x0:x1, k] for k in range(3))
            mask = self._TRACE_COLOURS[ch](r, g, b)
            # ignore filled overlays (e.g. a measurement bar): long runs of rows
            # that are coloured across most of the width
            full = mask.mean(axis=1) > 0.6
            run = 0
            for i, f in enumerate(full):
                run = run + 1 if f else 0
                if run > 6:
                    mask[i - run + 1:i + 1] = False
            ys = np.full(x1 - x0, np.nan)
            for j in range(x1 - x0):
                rows = np.flatnonzero(mask[:, j])
                if len(rows):
                    ys[j] = np.median(rows) + y0
            # the trigger-level arrow is drawn in the channel colour over the
            # right-most ~16 px of the plot; don't mistake it for the trace
            ys[-16:] = np.nan
            good = ~np.isnan(ys)
            if good.sum() < 10:
                continue
            cols = np.arange(x1 - x0)
            ys = np.interp(cols, cols[good], ys[good])
            counts = (self._SCREEN_CY - ys) / self._SCREEN_PX_PER_DIV_Y * 25.0
            # resample to the USB record density so the rest of the app is unchanged
            n = int(round(2 * half_div * 200))      # 200 points per division
            xs_div = (cols + x0 - cx) / self._SCREEN_PX_PER_DIV_X
            out[ch] = np.interp((np.arange(n) - n / 2) / 200.0, xs_div, counts)
        return out

    def read_waveform(self, source: int = P.SRC_CH1, timeout: float = 15.0) -> list[Waveform]:
        """Read the displayed record of CH1 (0), CH2 (1), scope MATH (3) or all (4).

        Reply per channel: ``0x82 00 <u24 count>`` then ``0x82 01 <src> <int8…>``
        chunks, or ``0x82 03 <ch>`` if that channel has no data; the request
        ends with ``0x82 02 <src>``. The ``<src>`` byte in data chunks only echoes
        the request, so with source 4 the channels (CH1, CH2, MATH) are told apart
        by the order of their start packets.
        """
        order = {P.SRC_ALL: [0, 1, 2], P.SRC_MATH: [2]}.get(source, [source])
        self.send(P.CMD_READ_WAVE, bytes([1, source]))
        waves: list[Waveform] = []
        pending = list(order)
        current: Waveform | None = None
        deadline = time.monotonic() + timeout
        while True:
            pkt = self.recv(max(0.05, deadline - time.monotonic()))
            if pkt.request_cmd != P.CMD_READ_WAVE or not pkt.payload:
                continue
            sub = pkt.payload[0]
            if sub in (P.SUB_DATA, P.SUB_END, P.SUB_ERROR) and len(pkt.payload) < 2:
                continue  # malformed / unrelated (e.g. 0x92 status) - ignore
            deadline = time.monotonic() + timeout
            if sub == P.SUB_START:
                ch = pending.pop(0) if pending else -1
                current = Waveform(source=ch, samples=b"",
                                   declared_length=int.from_bytes(pkt.payload[1:4], "little"))
                waves.append(current)
            elif sub == P.SUB_DATA:
                if current is None:  # data without a start packet
                    current = Waveform(source=order[0], samples=b"")
                    waves.append(current)
                current.samples += pkt.payload[2:]
            elif sub == P.SUB_ERROR:
                ch = pkt.payload[1]
                if ch in pending:
                    pending.remove(ch)
                if source != P.SRC_ALL and not waves:
                    raise ScopeError(f"scope reported no data for source {ch}")
            elif sub == P.SUB_END:
                return waves

    # -- full records via the scope's CSV export ------------------------
    def shell(self, command: str, timeout: float = 10.0) -> str:
        """Run a shell command on the scope through the factory protocol
        ('C' 0x11) and return its output. Used only for ls/rm on the USB stick."""
        rep = self.request(0x11, command.encode(), timeout=timeout, marker=P.MARK_DEBUG)
        return rep.payload.decode("latin-1", "replace")

    def resume(self, attempts: int = 3) -> bool:
        """Put the scope back into Run if it is stopped (e.g. after Single)."""
        for _ in range(attempts):
            if self.read_settings().trigger_state() != "Stop":
                return True
            self.press_key("CT-RS-KEY")
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                time.sleep(0.2)
                if self.read_settings().trigger_state() != "Stop":
                    return True
        return False

    def usb_stick_present(self) -> bool:
        return "/dev/sd" in self.shell("df -k /mnt/udisk 2>/dev/null")

    def _stick_csvs(self) -> dict[str, int]:
        out = self.shell("ls -l /mnt/udisk/*.csv 2>/dev/null")
        files = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[-1].endswith(".csv"):
                files[parts[-1]] = int(parts[4])
        return files

    def _csv_source_selected(self) -> int | None:
        """Which source radio button (1/2) is selected in the CSV menu, read
        from a screenshot of the side menu (orange = selected)."""
        img = self.screenshot()
        def orange(y0, y1):
            r = img[y0:y1, 656:680].astype(int)
            return bool(((r[..., 0] > 200) & (r[..., 1] > 90) & (r[..., 1] < 200) & (r[..., 2] < 90)).any())
        ch1, ch2 = orange(55, 72), orange(84, 101)
        return 1 if ch1 and not ch2 else 2 if ch2 and not ch1 else None

    def capture_full(self, channel: int = 1, single: bool = True, keep_file: bool = False,
                     trigger_timeout: float = 30.0, progress=None) -> FullRecord:
        """Capture the complete acquisition record of one channel (4 000 / 20 000 /
        40 000 points at memory depth 4K / 20K / 40K) using the scope's own CSV export.

        Needs a FAT USB stick in the scope's front port. Sequence: optional
        Single capture -> Save/Recall > CSV > Save to the stick -> download with
        command 0x10 -> delete the file. Uses only key presses and file
        transfers; no settings writes. Takes a few seconds (≈ 0.8 MB per 40K).

        single=True takes a fresh single-shot acquisition (waits for a
        trigger); False saves the current acquisition. The scope is put back
        into Run afterwards if it was running.
        """
        import numpy as np

        def note(msg):
            if progress:
                progress(msg)

        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        st = self.read_settings()
        if not st.channel_on(channel):
            raise ScopeError(f"CH{channel} is off")
        if not self.usb_stick_present():
            raise ScopeError("no USB stick in the scope's front port (needed for the CSV export)")
        was_running = st.trigger_state() != "Stop"
        menu_was_visible = st["CONTROL-DISP-MENU"] == 1
        try:
            # 1. freeze an acquisition
            note("capturing")
            self.press_key("CT-SINGLESEQ-KEY" if single else "CT-RS-KEY")
            end = time.monotonic() + trigger_timeout
            while self.read_settings().trigger_state() != "Stop":
                if time.monotonic() > end:
                    raise ScopeError("no trigger within the timeout (check trigger settings)")
                time.sleep(0.2)
            # 2. Save/Recall > CSV, pick the source channel
            note("saving on the scope")
            self.open_menu("MENU-SR-KEY", SR_MENU_ID)
            self.press_key("FN-3-KEY")
            end = time.monotonic() + 2.0
            while self.read_settings()["CONTROL-MENUID"] != CSV_MENU_ID:
                if time.monotonic() > end:
                    raise ScopeError("CSV menu did not open")
                time.sleep(0.15)
            time.sleep(0.3)
            if self._csv_source_selected() != channel:
                self.press_key("FN-1-KEY")
                time.sleep(0.4)
                if self._csv_source_selected() != channel:
                    raise ScopeError("could not select the CSV source channel")
            # 3. Save: the first press may only open the file list
            before = self._stick_csvs()
            new: list[str] = []
            for _ in range(2):
                self.press_key("FN-2-KEY")
                end = time.monotonic() + 3.0
                while time.monotonic() < end and not new:
                    time.sleep(0.5)
                    new = [f for f in self._stick_csvs() if f not in before]
                if new:
                    break
            if not new:
                img = self.screenshot()
                banner = img[428:445, 100:560].astype(int)
                refused = ((banner[..., 0] > 200) & (banner[..., 2] < 80)).mean() > 0.3
                raise ScopeError("the scope refused the CSV export (it does so e.g. while the zoom "
                                 "window is on; FN-7 toggles it)" if refused
                                 else "the scope did not write a CSV file")
            name = new[0]
            # 4. wait until the file has stopped growing
            last, end = -1, time.monotonic() + 120
            while time.monotonic() < end:
                size = self._stick_csvs().get(name, 0)
                if size and size == last:
                    break
                last = size
                time.sleep(1.0)
            # 5. download and parse
            note(f"downloading {last // 1024} KB")
            raw = self.read_file(name, timeout=120)
            if not keep_file:
                self.shell(f"rm -f {name}")
        finally:
            # leave the CSV/file-list screen and resume acquisition
            try:
                if self.read_settings()["CONTROL-MENUID"] == CSV_MENU_ID:
                    self.press_key("FN-6-KEY")  # Back (closes the file list too)
                    time.sleep(0.3)
                self.set_menu_visible(menu_was_visible)
                if was_running:
                    self.resume()
            except ScopeError:
                pass

        header = {}
        rows = []
        for line in raw.decode("latin-1").splitlines():
            line = line.strip().lstrip(",")
            if line.startswith("#"):
                k, _, v = line[1:].partition("=")
                header[k] = v
            elif line:
                rows.append(line)
        data = np.loadtxt(rows, delimiter=",", ndmin=2)
        # The scope writes the nominal depth + 64 samples; the tail of that
        # padding is invalid (seen as -8..-9 V spikes), so keep the nominal depth.
        nominal = len(data) - len(data) % 1000
        if nominal >= 1000:
            data = data[:nominal]
        tb = float(header.get("timebase", "0(ps)").split("(")[0]) * 1e-12
        vb = float(header.get("voltbase", "0(uV)").split("(")[0]) * 1e-6
        note(f"done: {len(data)} points")
        return FullRecord(channel, data[:, 0], data[:, 1] / 1000.0, tb, vb, name)  # mV -> V

    def close(self) -> None:
        self.t.close()

    def __enter__(self) -> "DSO5000P":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
