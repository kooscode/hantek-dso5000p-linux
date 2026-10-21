"""Control panels for the GUI. Each panel reads its state from a Settings
object (update_from) and sends changes through the window's worker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QSlider, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .analysis import FFT_WINDOWS, MATH_OPS
from .settings import (
    ACQ_MODE, AVERAGES, COUNTS_PER_DIV, COUPLING, EDGE_SLOPE, MIN_VB_INDEX, PROBE,
    PULSE_POLARITY, STORE_DEPTH, TIMEBASE, TRIG_COUP, TRIG_MODE, TRIG_SRC, TRIG_TYPE,
    VIDEO_POLARITY, VIDEO_STANDARD, VIDEO_SYNC, VOLTS_PER_DIV, WHEN, Settings, fmt_si,
)

if TYPE_CHECKING:
    from .gui import MainWindow

CH_COLORS = {1: (255, 230, 0), 2: (0, 220, 255)}
MATH_COLOR = (255, 80, 220)
REF_COLORS = {"A": (255, 160, 60), "B": (150, 255, 120)}


def combo(items) -> QComboBox:
    c = QComboBox()
    c.addItems(list(items))
    return c


def us_spin(lo_us: float = 0.01, hi_us: float = 10_000_000) -> QDoubleSpinBox:
    """Spin box for a time in microseconds (scope stores picoseconds)."""
    s = QDoubleSpinBox()
    s.setDecimals(3)
    s.setRange(lo_us, hi_us)
    s.setSuffix(" µs")
    s.setKeyboardTracking(False)
    return s


def volt_spin() -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setDecimals(3)
    s.setRange(-1000, 1000)
    s.setSuffix(" V")
    s.setKeyboardTracking(False)
    return s


class Panel(QWidget):
    """Base: holds the window, guards against echoing updates back to the scope."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self._updating = False

    def send(self, field: str, value: int):
        if not self._updating:
            self.win.worker.submit("set", **{field: int(value)})

    def op(self, name: str, *args):
        if not self._updating:
            self.win.worker.submit(name, *args)

    def update_from(self, st: Settings):
        self._updating = True
        try:
            self._update(st)
        finally:
            self._updating = False

    def _update(self, st: Settings):  # pragma: no cover - overridden
        pass


# ----------------------------------------------------------------- vertical
class ChannelBox(QGroupBox):
    def __init__(self, ch: int, win: "MainWindow"):
        super().__init__(f"CH{ch}")
        self.ch, self.win = ch, win
        r, g, b = CH_COLORS[ch]
        self.setStyleSheet(f"QGroupBox {{ color: rgb({r},{g},{b}); font-weight: bold; }}")
        lay = QGridLayout(self)
        self.on = QCheckBox("On")
        self.bw = QCheckBox("20 MHz limit")
        self.vb = QComboBox()
        self.coup = combo(COUPLING)
        self.probe = combo(f"{p}×" for p in PROBE)
        lay.addWidget(self.on, 0, 0)
        lay.addWidget(self.bw, 0, 1)
        for i, (label, w) in enumerate([("V/div", self.vb), ("Coupling", self.coup),
                                        ("Probe", self.probe)], 1):
            lay.addWidget(QLabel(label), i, 0)
            lay.addWidget(w, i, 1)

        # vertical position: full-width slider, label above, Zero button beside.
        # Dragging only previews (trace + ground marker move on screen); the
        # scope gets one update when the slider is released.
        self.pos_label = QLabel("Position")
        self.pos_label.setStyleSheet("color: palette(text); font-weight: normal;")
        self.pos = QSlider(Qt.Horizontal)
        self.pos.setRange(-200, 200)          # screen counts, 25 per division (±8 div)
        self.pos.setPageStep(25)              # one division
        self.pos.setTracking(False)           # valueChanged only on release / keys
        self.pos.setToolTip("Vertical position (25 steps per division). Drag to preview, "
                            "release to apply.")
        self.pos_zero = QPushButton("Zero")
        self.pos_zero.setToolTip(f"Put CH{ch}'s ground level back at the screen centre")
        pr = QHBoxLayout()
        pr.addWidget(self.pos, 1)
        pr.addWidget(self.pos_zero)
        lay.addWidget(self.pos_label, 4, 0, 1, 2)
        lay.addLayout(pr, 5, 0, 1, 2)
        self._send_timer = QTimer(self)
        self._send_timer.setSingleShot(True)
        self._send_timer.setInterval(150)     # coalesce keyboard / wheel steps
        self._send_timer.timeout.connect(self._send_pos)
        self._updating = False
        p = f"VERT-CH{ch}-"
        self.on.toggled.connect(lambda v: self._send(p + "DISP", int(v)))
        self.bw.toggled.connect(lambda v: self._send(p + "20MHZ", int(v)))
        self.vb.currentIndexChanged.connect(lambda i: i >= 0 and self._send(p + "VB", i + MIN_VB_INDEX))
        self.coup.currentIndexChanged.connect(lambda i: self._send(p + "COUP", i))
        self.probe.currentIndexChanged.connect(lambda i: self._send(p + "PROBE", i))
        self.pos.sliderMoved.connect(self._pos_preview)
        self.pos.valueChanged.connect(self._pos_changed)
        self.pos_zero.clicked.connect(lambda: self.pos.setValue(0) if self.pos.value() else self._pos_changed(0))

    def _send(self, field, value):
        if not self._updating:
            self.win.worker.submit("set", **{field: value})

    # -- position slider ----------------------------------------------------
    def _pos_text(self, counts: int) -> None:
        st = self.win.last_settings
        vdiv = st.volts_per_div(self.ch) if st else None
        volts = f", {fmt_si(counts / COUNTS_PER_DIV * vdiv, 'V')}" if vdiv else ""
        self.pos_label.setText(f"Position  {counts:+d}  ({counts / COUNTS_PER_DIV:+.2f} div{volts})")

    def _pos_preview(self, value: int) -> None:
        """While dragging: move the trace on screen only."""
        self._pos_text(value)
        self.win.preview_position(self.ch, value)

    def _pos_changed(self, value: int) -> None:
        """Slider released, Zero clicked or keyboard/wheel step: send once."""
        if self._updating:
            return
        self._pos_text(value)
        self.win.preview_position(self.ch, value)
        self._send_timer.start()

    def _send_pos(self) -> None:
        self.win.worker.submit("set", **{f"VERT-CH{self.ch}-POS": self.pos.value()})

    def pos_busy(self) -> bool:
        return self.pos.isSliderDown() or self._send_timer.isActive()

    def update_from(self, st: Settings):
        self._updating = True
        try:
            p = f"VERT-CH{self.ch}-"
            self.on.setChecked(bool(st[p + "DISP"]))
            self.bw.setChecked(bool(st[p + "20MHZ"]))
            probe = st.probe(self.ch)
            labels = [fmt_si(v * probe, "V") for v in VOLTS_PER_DIV[MIN_VB_INDEX:]]
            if [self.vb.itemText(i) for i in range(self.vb.count())] != labels:
                self.vb.clear()
                self.vb.addItems(labels)
            self.vb.setCurrentIndex(max(0, st[p + "VB"] - MIN_VB_INDEX))
            self.coup.setCurrentIndex(min(st[p + "COUP"], len(COUPLING) - 1))
            self.probe.setCurrentIndex(min(st[p + "PROBE"], 3))
            if not self.pos_busy() and self.win.pos_preview.get(self.ch) is None:
                self.pos.setValue(max(-200, min(200, st[p + "POS"])))
                self._pos_text(st[p + "POS"])
        finally:
            self._updating = False


# ----------------------------------------------------------------- trigger
class TriggerPanel(Panel):
    """Common trigger settings plus a page of extra parameters per type."""

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        outer = QVBoxLayout(self)
        common = QGroupBox("Trigger")
        g = QGridLayout(common)
        self.type = combo(TRIG_TYPE)
        self.src = combo(TRIG_SRC)
        self.mode = combo(TRIG_MODE)
        self.coup = combo(TRIG_COUP)
        # trigger level: slider in screen counts (25 per division, 0 = screen centre),
        # previewed on the plot while dragging, sent once on release
        self.level_label = QLabel("Level")
        self.level = QSlider(Qt.Horizontal)
        self.level.setRange(-100, 100)        # ±4 divisions = the visible screen
        self.level.setPageStep(COUNTS_PER_DIV)
        self.level.setTracking(False)
        self.level.setToolTip("Trigger level. Drag to preview, release to apply.")
        self.level_center = QPushButton("Center")
        self.level_center.setToolTip("Put the trigger level at the screen centre")
        self._level_timer = QTimer(self)
        self._level_timer.setSingleShot(True)
        self._level_timer.setInterval(150)
        self._level_timer.timeout.connect(self._send_level)
        self.half = QPushButton("50%")
        self.holdoff = us_spin(0.1)
        rows = [("Type", self.type), ("Source", self.src), ("Mode", self.mode), ("Coupling", self.coup)]
        for i, (label, w) in enumerate(rows):
            g.addWidget(QLabel(label), i, 0)
            g.addWidget(w, i, 1, 1, 2)
        self.half.setToolTip("Set the trigger level to the middle of the signal")
        self.level_zero = QPushButton("Level → 0 V")
        self.level_zero.setToolTip("Set the trigger level to 0 V (the source channel's ground)")
        self.force = QPushButton("Force trigger")
        self.force.setToolTip("Trigger once now, even without a trigger condition")
        g.addWidget(self.level_label, 4, 0, 1, 3)
        lr = QHBoxLayout()
        lr.addWidget(self.level, 1)
        lr.addWidget(self.level_center)
        g.addLayout(lr, 5, 0, 1, 3)
        g.addWidget(self.half, 6, 0)
        g.addWidget(self.level_zero, 6, 1)
        g.addWidget(self.force, 6, 2)
        g.addWidget(QLabel("Holdoff"), 7, 0)
        g.addWidget(self.holdoff, 7, 1, 1, 2)
        self.status = QLabel("")
        self.status.setStyleSheet("font-weight: bold;")
        g.addWidget(self.status, 8, 0, 1, 3)
        outer.addWidget(common)

        # horizontal trigger position (where the trigger point sits on screen)
        pb = QGroupBox("Trigger position (horizontal)")
        pg_ = QGridLayout(pb)
        self.hpos_label = QLabel("")
        self.hpos_left = QPushButton("◀")
        self.hpos_centre = QPushButton("To centre")
        self.hpos_right = QPushButton("▶")
        self.hpos_left.setToolTip("Move the trigger point left (more time after the trigger)")
        self.hpos_right.setToolTip("Move the trigger point right (more time before the trigger)")
        self.hpos_centre.setToolTip("Put the trigger point back at the screen centre "
                                    "(nothing moves if it is already there)")
        pg_.addWidget(self.hpos_label, 0, 0, 1, 3)
        pg_.addWidget(self.hpos_left, 1, 0)
        pg_.addWidget(self.hpos_centre, 1, 1)
        pg_.addWidget(self.hpos_right, 1, 2)
        outer.addWidget(pb)

        # per-type parameter pages ------------------------------------
        self.pages = QStackedWidget()
        box = QGroupBox("Parameters")
        QVBoxLayout(box).addWidget(self.pages)
        outer.addWidget(box)
        outer.addStretch(1)

        # Edge
        self.edge_slope = combo(EDGE_SLOPE)
        self.pages.addWidget(self._page([("Slope", self.edge_slope)]))
        # Video
        self.v_pol = combo(VIDEO_POLARITY)
        self.v_std = combo(VIDEO_STANDARD)
        self.v_sync = combo(VIDEO_SYNC)
        self.v_line = QSpinBox()
        self.v_line.setRange(1, 625)
        self.v_line.setKeyboardTracking(False)
        self.pages.addWidget(self._page([("Polarity", self.v_pol), ("Standard", self.v_std),
                                         ("Sync", self.v_sync), ("Line", self.v_line)]))
        # Pulse
        self.p_pol = combo(PULSE_POLARITY)
        self.p_when = combo(WHEN)
        self.p_width = us_spin()
        self.pages.addWidget(self._page([("Polarity", self.p_pol), ("When", self.p_when),
                                         ("Pulse width", self.p_width)]))
        # Slope
        self.s_slope = combo(EDGE_SLOPE)
        self.s_when = combo(WHEN)
        self.s_v1 = volt_spin()
        self.s_v2 = volt_spin()
        self.s_time = us_spin()
        self.pages.addWidget(self._page([("Slope", self.s_slope), ("When", self.s_when),
                                         ("V1 (upper)", self.s_v1), ("V2 (lower)", self.s_v2),
                                         ("Time", self.s_time)]))
        # Overtime
        self.o_pol = combo(PULSE_POLARITY)
        self.o_time = us_spin()
        self.pages.addWidget(self._page([("Polarity", self.o_pol), ("Overtime", self.o_time)]))
        # Alternate
        lbl = QLabel("Alternate trigger switches between CH1 and CH2, each with its "
                     "own trigger setup. Configure the per-channel setups on the scope.")
        lbl.setWordWrap(True)
        self.pages.addWidget(lbl)

        # wiring ------------------------------------------------------
        self.type.currentIndexChanged.connect(lambda i: (self.pages.setCurrentIndex(i), self.send("TRIG-TYPE", i)))
        self.src.currentIndexChanged.connect(lambda i: self.send("TRIG-SRC", i))
        self.coup.currentIndexChanged.connect(lambda i: self.send("TRIG-COUP", i))
        self.mode.currentIndexChanged.connect(lambda i: self.op("trigger_mode", i))
        self.level.sliderMoved.connect(self._level_preview)
        self.level.valueChanged.connect(self._level_changed)
        self.level_center.clicked.connect(
            lambda: self.level.setValue(0) if self.level.value() else self._level_changed(0))
        self.half.clicked.connect(lambda: win.worker.submit("key", "TG-PHALF-KEY"))
        self.level_zero.clicked.connect(lambda: win.worker.submit("key", "TG-PZERO-KEY"))
        self.force.clicked.connect(lambda: win.worker.submit("key", "TG-FORCE-KEY"))
        # one key press moves the trigger point ~1/40 division, so step 10 at a time
        self.hpos_left.clicked.connect(lambda: win.worker.submit("key", "HZ-PADD-KEY", 10))
        self.hpos_right.clicked.connect(lambda: win.worker.submit("key", "HZ-PSUB-KEY", 10))
        self.hpos_centre.clicked.connect(lambda: win.worker.submit("key", "HZ-PZERO-KEY"))
        self.holdoff.valueChanged.connect(lambda us: self.send("TRIG-HOLDTIME", round(us * 1e6)))
        self.edge_slope.currentIndexChanged.connect(lambda i: self.send("TRIG-EDGE-SLOPE", i))
        self.v_pol.currentIndexChanged.connect(lambda i: self.send("TRIG-VIDEO-NEG", i))
        self.v_std.currentIndexChanged.connect(lambda i: self.send("TRIG-VIDEO-PAL", i))
        self.v_sync.currentIndexChanged.connect(lambda i: self.send("TRIG-VIDEO-SYN", i))
        self.v_line.valueChanged.connect(lambda v: self.send("TRIG-VIDEO-LINE", v))
        self.p_pol.currentIndexChanged.connect(lambda i: self.send("TRIG-PULSE-NEG", i))
        self.p_when.currentIndexChanged.connect(lambda i: self.send("TRIG-PULSE-WHEN", i))
        self.p_width.valueChanged.connect(lambda us: self.send("TRIG-PULSE-TIME", round(us * 1e6)))
        self.s_slope.currentIndexChanged.connect(lambda i: self.send("TRIG-SLOPE-SET", i))
        self.s_when.currentIndexChanged.connect(lambda i: self.send("TRIG-SLOPE-WHEN", i))
        self.s_v1.valueChanged.connect(lambda v: self._send_volts("TRIG-SLOPE-V1", v))
        self.s_v2.valueChanged.connect(lambda v: self._send_volts("TRIG-SLOPE-V2", v))
        self.s_time.valueChanged.connect(lambda us: self.send("TRIG-SLOPE-TIME", round(us * 1e6)))
        self.o_pol.currentIndexChanged.connect(lambda i: self.send("TRIG-OVERTIME-NEG", i))
        self.o_time.valueChanged.connect(lambda us: self.send("TRIG-OVERTIME-TIME", round(us * 1e6)))

    @staticmethod
    def _page(rows) -> QWidget:
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(0, 0, 0, 0)
        for i, (label, widget) in enumerate(rows):
            g.addWidget(QLabel(label), i, 0)
            g.addWidget(widget, i, 1)
        g.setRowStretch(len(rows), 1)
        return w

    # -- trigger level slider ----------------------------------------------------
    def _level_text(self, counts: int) -> None:
        st = self.win.last_settings
        ch = st.trigger_channel() if st else None
        volts = f"{fmt_si(round(st.counts_to_volts(ch, counts), 4), 'V')}  " if ch else ""
        self.level_label.setText(f"Level  {volts}({counts / COUNTS_PER_DIV:+.2f} div from centre)")

    def _level_preview(self, counts: int) -> None:
        self._level_text(counts)
        self.win.preview_level(counts)

    def _level_changed(self, counts: int) -> None:
        if self._updating:
            return
        self._level_preview(counts)
        self._level_timer.start()

    def _send_level(self) -> None:
        self.win.worker.submit("set", **{"TRIG-VPOS": self.level.value()})

    def level_busy(self) -> bool:
        return self.level.isSliderDown() or self._level_timer.isActive()

    def _send_volts(self, field: str, volts: float):
        st = self.win.last_settings
        ch = st.trigger_channel() if st else None
        if ch and not self._updating:
            self.send(field, st.volts_to_counts(ch, volts))

    def _set_volts(self, spin: QDoubleSpinBox, st: Settings, counts: int):
        ch = st.trigger_channel()
        spin.setEnabled(ch is not None)
        if ch:
            spin.setSingleStep((st.volts_per_div(ch) or 1) / COUNTS_PER_DIV)
            spin.setValue(st.counts_to_volts(ch, counts))

    def _update(self, st: Settings):
        t = min(st["TRIG-TYPE"], len(TRIG_TYPE) - 1)
        self.type.setCurrentIndex(t)
        self.pages.setCurrentIndex(t)
        self.src.setCurrentIndex(min(st["TRIG-SRC"], len(TRIG_SRC) - 1))
        self.mode.setCurrentIndex(min(st["TRIG-MODE"], 1))
        self.coup.setCurrentIndex(min(st["TRIG-COUP"], len(TRIG_COUP) - 1))
        if not self.level_busy() and self.win.level_preview is None:
            self.level.setValue(max(-100, min(100, st["TRIG-VPOS"])))
            self._level_text(st["TRIG-VPOS"])
        self.holdoff.setValue(st["TRIG-HOLDTIME"] / 1e6)
        self.edge_slope.setCurrentIndex(min(st["TRIG-EDGE-SLOPE"], 1))
        self.v_pol.setCurrentIndex(min(st["TRIG-VIDEO-NEG"], 1))
        self.v_std.setCurrentIndex(min(st["TRIG-VIDEO-PAL"], 1))
        self.v_sync.setCurrentIndex(min(st["TRIG-VIDEO-SYN"], len(VIDEO_SYNC) - 1))
        self.v_line.setRange(1, 625 if st["TRIG-VIDEO-PAL"] else 525)
        self.v_line.setValue(st["TRIG-VIDEO-LINE"])
        self.p_pol.setCurrentIndex(min(st["TRIG-PULSE-NEG"], 1))
        self.p_when.setCurrentIndex(min(st["TRIG-PULSE-WHEN"], 3))
        self.p_width.setValue(st["TRIG-PULSE-TIME"] / 1e6)
        self.s_slope.setCurrentIndex(min(st["TRIG-SLOPE-SET"], 1))
        self.s_when.setCurrentIndex(min(st["TRIG-SLOPE-WHEN"], 3))
        self._set_volts(self.s_v1, st, st["TRIG-SLOPE-V1"])
        self._set_volts(self.s_v2, st, st["TRIG-SLOPE-V2"])
        self.s_time.setValue(st["TRIG-SLOPE-TIME"] / 1e6)
        self.o_pol.setCurrentIndex(min(st["TRIG-OVERTIME-NEG"], 1))
        self.o_time.setValue(st["TRIG-OVERTIME-TIME"] / 1e6)
        hpos = st.get("HORIZ-TRIGTIME", 0) * 1e-12
        tdiv = st.seconds_per_div() or 1
        where = "at the screen centre" if hpos == 0 else \
            f"{abs(hpos / tdiv):.2f} div {'left' if hpos > 0 else 'right'} of centre"
        self.hpos_label.setText(f"Trigger point: {fmt_si(hpos, 's')} ({where})")
        self.update_status(st)

    def update_status(self, st: Settings):
        f = st.trigger_frequency()
        color = {"Trig'd": "#2a2", "Stop": "#d33"}.get(st.trigger_state(), "#c80")
        self.status.setText(f"<span style='color:{color}'>{st.trigger_state()}</span>"
                            f"   counter: {fmt_si(f, 'Hz') if f else '—'}")


# ----------------------------------------------------------------- horizontal / acquire
class AcquirePanel(Panel):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        outer = QVBoxLayout(self)
        hb = QGroupBox("Horizontal")
        g = QGridLayout(hb)
        self.tb = combo(fmt_si(t, "s") for t in TIMEBASE)
        g.addWidget(QLabel("Time/div"), 0, 0)
        g.addWidget(self.tb, 0, 1)
        outer.addWidget(hb)

        ab = QGroupBox("Acquisition")
        g = QGridLayout(ab)
        self.mode = combo(ACQ_MODE)
        self.avg = combo(str(a) for a in AVERAGES)
        self.depth = combo(STORE_DEPTH.values())
        self.info = QLabel("")
        self.info.setWordWrap(True)
        for i, (label, w) in enumerate([("Mode", self.mode), ("Averages", self.avg),
                                        ("Memory depth", self.depth)]):
            g.addWidget(QLabel(label), i, 0)
            g.addWidget(w, i, 1)
        g.addWidget(self.info, 3, 0, 1, 2)
        outer.addWidget(ab)

        fb = QGroupBox("Full record (via USB stick)")
        g = QGridLayout(fb)
        self.full_ch = combo(["CH1", "CH2"])
        self.full_single = QCheckBox("Take a fresh single shot")
        self.full_single.setChecked(True)
        self.full_btn = QPushButton("Capture full record")
        note = QLabel("The scope saves its whole record (4 000 / 20 000 / 40 000 points "
                      "at 4K / 20K / 40K depth) to a USB stick in its front port; it is "
                      "downloaded and deleted again. Takes about 5–10 s.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        g.addWidget(QLabel("Channel"), 0, 0)
        g.addWidget(self.full_ch, 0, 1)
        g.addWidget(self.full_single, 1, 0, 1, 2)
        g.addWidget(self.full_btn, 2, 0, 1, 2)
        g.addWidget(note, 3, 0, 1, 2)
        outer.addWidget(fb)
        outer.addStretch(1)
        self._depth_values = list(STORE_DEPTH)
        self.full_btn.clicked.connect(
            lambda: win.worker.submit("full", self.full_ch.currentIndex() + 1, self.full_single.isChecked()))

        # timebase/acquire changes go through front-panel keys: use 'activated'
        # (user action only) so programmatic updates never trigger key presses
        self.tb.activated.connect(lambda i: win.worker.submit("timebase", i))
        self.mode.activated.connect(lambda i: win.worker.submit("acquire_mode", i))
        self.avg.activated.connect(lambda i: win.worker.submit("averages", i))
        self.depth.activated.connect(lambda i: win.worker.submit("depth", self._depth_values[i]))

    def _update(self, st: Settings):
        self.tb.setCurrentIndex(st["HORIZ-TB"])
        mode = min(st["ACQURIE-MODE"], 2)
        self.mode.setCurrentIndex(mode)
        self.avg.setEnabled(mode == 2)
        if mode == 2:
            self.avg.setCurrentIndex(min(st["ACQURIE-AVG-CNT"], len(AVERAGES) - 1))
        d = st["ACQURIE-STORE-DEPTH"]
        if d in self._depth_values:
            self.depth.setCurrentIndex(self._depth_values.index(d))
        # 40K is single-channel only
        both = st.channel_on(1) and st.channel_on(2)
        item = self.depth.model().item(self._depth_values.index(4))
        item.setEnabled(not both)
        self.update_info(st)

    def update_info(self, st: Settings):
        n = self.win.record_length()
        tdiv = st.seconds_per_div()
        if n and tdiv:
            ppd = st.points_per_div(n)
            self.info.setText(f"USB record: {n} points ({ppd:g}/div, {n / ppd:.1f} div wide), "
                              f"{fmt_si(1 / st.sample_interval(n), 'Sa/s')}")


# ----------------------------------------------------------------- math / FFT
class MathPanel(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        box = QGroupBox("Math (computed on the PC)")
        g = QGridLayout(box)
        self.op = combo(MATH_OPS)
        self.scale = combo(["Auto"] + [fmt_si(v, "V") for v in
                                       (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100)])
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-4, 4)
        self.offset.setSingleStep(0.2)
        self.offset.setSuffix(" div")
        self.win_fn = combo(FFT_WINDOWS)
        self.win_fn.setCurrentText("Hanning")
        self.fft_db = QCheckBox("dBV (else Vrms)")
        self.fft_db.setChecked(True)
        self.fft_log = QCheckBox("Log frequency axis")
        rows = [("Function", self.op), ("Math scale/div", self.scale), ("Math offset", self.offset),
                ("FFT window", self.win_fn)]
        for i, (label, w) in enumerate(rows):
            g.addWidget(QLabel(label), i, 0)
            g.addWidget(w, i, 1)
        g.addWidget(self.fft_db, 4, 0, 1, 2)
        g.addWidget(self.fft_log, 5, 0, 1, 2)
        self.meas = QLabel("")
        self.meas.setWordWrap(True)
        self.meas.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g.addWidget(self.meas, 6, 0, 1, 2)
        outer.addWidget(box)
        outer.addStretch(1)
        for w in (self.op, self.scale, self.win_fn):
            w.currentIndexChanged.connect(win.redraw)
        for w in (self.fft_db, self.fft_log):
            w.toggled.connect(win.redraw)
        self.offset.valueChanged.connect(win.redraw)

    def scale_value(self) -> float | None:
        t = self.scale.currentText()
        if t == "Auto":
            return None
        return [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100][self.scale.currentIndex() - 1]


# ----------------------------------------------------------------- cursors
class CursorPanel(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        box = QGroupBox("Cursors")
        g = QGridLayout(box)
        self.time_on = QCheckBox("Time cursors (X1, X2)")
        self.volt_on = QCheckBox("Voltage cursors (Y1, Y2)")
        self.src = combo(["CH1", "CH2", "Math"])
        g.addWidget(self.time_on, 0, 0, 1, 2)
        g.addWidget(self.volt_on, 1, 0, 1, 2)
        g.addWidget(QLabel("Voltage source"), 2, 0)
        g.addWidget(self.src, 2, 1)
        self.readout = QLabel("")
        self.readout.setStyleSheet("font-family: monospace;")
        self.readout.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g.addWidget(self.readout, 3, 0, 1, 2)
        outer.addWidget(box)
        hint = QLabel("Drag the dashed cursor lines on the plot.")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addStretch(1)
        self.time_on.toggled.connect(win.update_cursors)
        self.volt_on.toggled.connect(win.update_cursors)
        self.src.currentIndexChanged.connect(win.update_cursors)


# ----------------------------------------------------------------- references
class RefPanel(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        self.rows = {}
        for name in ("A", "B"):
            r, g_, b = REF_COLORS[name]
            box = QGroupBox(f"REF {name}")
            box.setStyleSheet(f"QGroupBox {{ color: rgb({r},{g_},{b}); font-weight: bold; }}")
            g = QGridLayout(box)
            show = QCheckBox("Show")
            src = combo(["CH1", "CH2", "Math"])
            save = QPushButton("Store")
            load = QPushButton("Load CSV…")
            export = QPushButton("Export CSV…")
            clear = QPushButton("Clear")
            info = QLabel("empty")
            info.setWordWrap(True)
            g.addWidget(QLabel("From"), 0, 0)
            g.addWidget(src, 0, 1)
            g.addWidget(save, 0, 2)
            g.addWidget(load, 1, 0)
            g.addWidget(export, 1, 1)
            g.addWidget(clear, 1, 2)
            g.addWidget(show, 2, 0)
            g.addWidget(info, 2, 1, 1, 2)
            outer.addWidget(box)
            self.rows[name] = dict(show=show, src=src, info=info)
            save.clicked.connect(lambda _=False, n=name: win.store_ref(n, self.rows[n]["src"].currentText()))
            load.clicked.connect(lambda _=False, n=name: win.load_ref(n))
            export.clicked.connect(lambda _=False, n=name: win.export_ref(n))
            clear.clicked.connect(lambda _=False, n=name: win.clear_ref(n))
            show.toggled.connect(win.redraw)
        outer.addStretch(1)
