"""Qt front-end for the Hantek DSO5000P.

Live traces, vertical/horizontal/trigger/acquire controls, PC-side math & FFT,
cursors, reference waveforms, screenshots and CSV export.

Run with ``hantek-dso-gui`` or ``python -m hantek_dso.gui``.
"""

from __future__ import annotations

import queue
import sys
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QObject, QSettings, Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QGroupBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStatusBar, QTabWidget,
    QVBoxLayout, QWidget,
)

from .analysis import Reference, fft, math_channel, measure, measure_values, peak
from .gui_panels import (
    CH_COLORS, MATH_COLOR, REF_COLORS, AcquirePanel, ChannelBox, CursorPanel, MathPanel,
    RefPanel, TriggerPanel,
)
from .scope import DSO5000P, SCREEN_H, SCREEN_W
from .settings import COUNTS_PER_DIV, COUPLING, HORIZ_DIVS, VERT_DIVS, Settings, fmt_fixed, fmt_si


# --------------------------------------------------------------------------- worker
class ScopeWorker(QObject):
    """Owns the USB connection. All scope I/O happens on this thread."""

    connected = Signal(str)
    disconnected = Signal(str)
    frame = Signal(object, object)       # Settings, {ch: np.int8 array}
    settings = Signal(object)
    screenshot = Signal(object)
    full_record = Signal(object)
    message = Signal(str)
    run_state = Signal(bool, bool)       # scope stopped, single shot armed

    def __init__(self):
        super().__init__()
        self.q: queue.Queue = queue.Queue()
        self.scope: DSO5000P | None = None
        self.running = False
        self.live = False
        self.interval = 0.05
        self.single_armed = False        # SingleShot pressed, waiting for the trigger
        self._stopped_key = None          # settings of the stopped frame already fetched

    def submit(self, fn_name: str, *args, **kw):
        self.q.put((fn_name, args, kw))

    @Slot()
    def run(self):
        self.running = True
        while self.running:
            try:
                item = self.q.get(timeout=self.interval if (self.live or self.single_armed) else 0.2)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    name, args, kw = item
                    getattr(self, "do_" + name)(*args, **kw)
                elif self.scope and (self.live or self.single_armed):
                    # with Stream OFF an armed single shot is still watched, so its
                    # frame appears as soon as it triggers
                    self.do_capture()
            except Exception as e:  # keep the thread alive, report the problem
                self.message.emit(f"{type(e).__name__}: {e}")
                if self.scope is None or "USBError" in type(e).__name__ or "Errno" in str(e):
                    self._drop(str(e))
        self._drop("closed")

    def _drop(self, why: str):
        if self.scope:
            try:
                self.scope.close()
            except Exception:
                pass
            self.scope = None
            self.live = False
            self.disconnected.emit(why)

    def _emit_settings(self):
        self.settings.emit(self.scope.read_settings())

    # commands -----------------------------------------------------------
    def do_connect(self, device: str | None = None):
        if self.scope:
            return
        self.scope = DSO5000P(device=device)
        st = self.scope.read_settings()
        self.connected.emit(f"connected to scope on USB port {self.scope.t.port}")
        self.settings.emit(st)
        self._emit_run_state(st)

    def do_disconnect(self):
        self._drop("disconnected")

    def do_live(self, on: bool):
        self.live = on

    def _emit_run_state(self, st):
        stopped = st.trigger_state() == "Stop"
        if stopped and self.single_armed:
            self.single_armed = False    # the single shot has triggered
        self.run_state.emit(stopped, self.single_armed)

    # settings that change on their own while the scope is stopped
    _VOLATILE = {"TRIG-STATE", "TRIG-FREQUENCY"}

    def _read_usb(self, st) -> dict:
        data = {}
        on = [ch for ch in (1, 2) if st.channel_on(ch)]
        if not on:
            return data
        try:
            # one request for both channels -> both come from the same acquisition
            src = 4 if len(on) == 2 else on[0] - 1
            for w in self.scope.read_waveform(src):
                if w.source in (0, 1) and w.samples:
                    data[w.source + 1] = w.as_int8().copy()
        except Exception:
            pass  # no frame available right now
        return data

    def do_capture(self, force: bool = False):
        """Read one frame.

        While the scope is stopped (Run/Stop or after SingleShot) it refuses to
        send waveform data, so the displayed traces are taken from a screenshot
        instead (marked "_screen"). That's done once per stop, and again if a
        setting changes while stopped (the scope redraws then)."""
        st = self.scope.read_settings()
        armed_before = self.single_armed
        self._emit_run_state(st)
        if st.trigger_state() != "Stop":
            self._stopped_key = None
            self.frame.emit(st, self._read_usb(st))
            return
        key = tuple((k, v) for k, v in st.values.items() if k not in self._VOLATILE)
        if key == self._stopped_key and not force and not armed_before:
            self.frame.emit(st, {})          # nothing new; GUI keeps the traces
            return
        data = self._read_usb(st)            # works for a moment right after stopping
        if not data:
            try:
                data = self.scope.screen_traces(st)
                data["_screen"] = True
            except Exception as e:
                self.message.emit(f"could not read the stopped frame: {e}")
        self._stopped_key = key
        self.frame.emit(st, data)

    def do_set(self, **fields):
        self.scope.set(**fields)
        self._emit_settings()

    def do_trigger_mode(self, mode: int):
        self.scope.set_trigger_mode(mode)
        self._emit_settings()

    def do_timebase(self, index: int):
        self.scope.set_timebase(index)
        self._emit_settings()

    def do_acquire_mode(self, mode: int):
        self.scope.set_acquire_mode(mode)
        self._emit_settings()

    def do_averages(self, index: int):
        self.scope.set_averages(index)
        self._emit_settings()

    def do_depth(self, value: int):
        got = self.scope.set_memory_depth(value)
        if got != value:
            self.message.emit("memory depth not available in this channel configuration")
        self._emit_settings()

    def do_key(self, name: str, repeat: int = 1):
        if name == "CT-SINGLESEQ-KEY":
            self.single_armed = True
            self._stopped_key = None
        for _ in range(repeat):
            self.scope.press_key(name)
            time.sleep(0.15)
        if name == "CT-RS-KEY":
            self.single_armed = False
            self._stopped_key = None
        st = self.scope.read_settings()
        self.settings.emit(st)
        if name == "CT-SINGLESEQ-KEY":
            self.run_state.emit(True, True)   # shows red + armed right away
        else:
            self._emit_run_state(st)

    def do_full(self, channel: int, single: bool):
        rec = self.scope.capture_full(channel, single=single,
                                      progress=lambda m: self.message.emit(f"Full record: {m}"))
        self.full_record.emit(rec)
        self._emit_settings()

    def do_screenshot(self):
        self.screenshot.emit(self.scope.screenshot())


# --------------------------------------------------------------------------- dialogs
class FullRecordDialog(QDialog):
    """Shows a full record with its own zoomable plot, measurements and CSV export."""

    def __init__(self, rec, parent=None):
        super().__init__(parent)
        self.rec = rec
        self.setWindowTitle(f"Full record - CH{rec.channel}, {len(rec.t)} points")
        self.resize(1000, 560)
        lay = QVBoxLayout(self)
        plot = pg.PlotWidget()
        plot.setBackground("k")
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.setLabel("bottom", "Time from record start", units="s")
        plot.setLabel("left", "Voltage", units="V")
        plot.plot(rec.t, rec.volts, pen=pg.mkPen(CH_COLORS[rec.channel], width=1))
        lay.addWidget(plot, 1)
        m = measure(rec.t, rec.volts)
        info = QLabel(f"{len(rec.t)} points at {fmt_si(rec.sample_rate, 'Sa/s')}, span "
                      f"{fmt_si(rec.t[-1] - rec.t[0], 's')}  (captured at "
                      f"{fmt_si(rec.timebase, 's')}/div, {fmt_si(rec.voltbase, 'V')}/div)<br>"
                      + "  ".join(f"{k}={v}" for k, v in m.items()))
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(info)
        hint = QLabel("Mouse wheel / drag to zoom and pan; right-click for more options.")
        hint.setStyleSheet("color: #666;")
        lay.addWidget(hint)
        b = QPushButton("Save CSV…")
        b.clicked.connect(self.save)
        lay.addWidget(b)

    def save(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Save full record",
                                            f"full_ch{self.rec.channel}.csv", "CSV (*.csv)")
        if fn:
            np.savetxt(fn, np.column_stack([self.rec.t, self.rec.volts]), delimiter=",",
                       header=f"time_s,CH{self.rec.channel}_V", comments="", fmt="%.9g")


class ScreenshotDialog(QDialog):
    def __init__(self, img: np.ndarray, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scope screen")
        self.q = QImage(img.tobytes(), SCREEN_W, SCREEN_H, SCREEN_W * 3, QImage.Format_RGB888).copy()
        lay = QVBoxLayout(self)
        lbl = QLabel()
        lbl.setPixmap(QPixmap.fromImage(self.q))
        lay.addWidget(lbl)
        b = QPushButton("Save PNG…")
        b.clicked.connect(self.save)
        lay.addWidget(b)

    def save(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Save screenshot", "scope.png", "PNG (*.png)")
        if fn:
            self.q.save(fn)


def _dashed(color, width=1):
    return pg.mkPen(color, width=width, style=Qt.DashLine)


# --------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self, autoconnect: bool = True):
        super().__init__()
        self.setWindowTitle("Hantek DSO5000P - Linux")
        self.resize(1320, 900)
        self.last_settings: Settings | None = None
        self.last_data: dict[int, np.ndarray] = {}
        self.refs: dict[str, Reference | None] = {"A": None, "B": None}
        self.math_v: np.ndarray | None = None
        self.math_scale = 1.0

        # worker thread ---------------------------------------------------
        self.thread = QThread(self)
        self.worker = ScopeWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.connected.connect(self.on_connected)
        self.worker.disconnected.connect(self.on_disconnected)
        self.worker.frame.connect(self.on_frame)
        self.worker.settings.connect(self.on_settings)
        self.worker.screenshot.connect(self.on_screenshot)
        self.worker.full_record.connect(self.on_full_record)
        self.worker.message.connect(self.on_message)
        self.worker.run_state.connect(self.on_run_state)
        self.thread.start()

        # plots -----------------------------------------------------------
        pg.setConfigOptions(antialias=False)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("k")
        self.plot.setXRange(-HORIZ_DIVS / 2, HORIZ_DIVS / 2, padding=0)
        self.plot.setYRange(-VERT_DIVS / 2 - 0.08, VERT_DIVS / 2 + 0.08, padding=0)
        self.plot.setMouseEnabled(False, False)
        self.plot.hideButtons()
        for ax in ("left", "bottom"):
            self.plot.getAxis(ax).setTicks([[(v, "") for v in range(-10, 11)]])
        self._x_half = HORIZ_DIVS / 2
        self.plot.showGrid(x=True, y=True, alpha=0.35)
        self.curves = {ch: self.plot.plot(pen=pg.mkPen(CH_COLORS[ch], width=1)) for ch in (1, 2)}
        self.math_curve = self.plot.plot(pen=pg.mkPen(MATH_COLOR, width=1))
        self.ref_curves = {n: self.plot.plot(pen=pg.mkPen(c, width=1)) for n, c in REF_COLORS.items()}
        self.trig_line = pg.InfiniteLine(0, angle=90, pen=_dashed((120, 120, 255)))
        self.plot.addItem(self.trig_line)
        # bottom-right overlay shown while the traces come from a screenshot
        self.screen_note = pg.TextItem("[Data Transfer Loss - Scope Screen Scraped]",
                                       color=(255, 152, 0), anchor=(1, 1))
        self.screen_note.setZValue(10)
        self.screen_note.hide()
        self.plot.addItem(self.screen_note)
        self.level_line = pg.InfiniteLine(0, angle=0, movable=True,
                                          pen=pg.mkPen(CH_COLORS[1], style=Qt.DotLine),
                                          hoverPen=pg.mkPen("w", width=2))
        self.level_line.sigPositionChangeFinished.connect(self.on_level_dragged)
        self.plot.addItem(self.level_line)
        self.gnd = {ch: pg.ScatterPlotItem(symbol="t2", size=12, brush=pg.mkBrush(CH_COLORS[ch]), pen=None)
                    for ch in (1, 2)}
        for g in self.gnd.values():
            self.plot.addItem(g)
        # cursors (positions in divisions)
        cur_pen, hover = _dashed((220, 220, 220)), pg.mkPen("w", width=2)
        self.cx = [pg.InfiniteLine(x, angle=90, movable=True, pen=cur_pen, hoverPen=hover,
                                   label=n, labelOpts={"position": 0.97, "color": "w"})
                   for x, n in ((-3, "X1"), (3, "X2"))]
        self.cy = [pg.InfiniteLine(y, angle=0, movable=True, pen=cur_pen, hoverPen=hover,
                                   label=n, labelOpts={"position": 0.03, "color": "w"})
                   for y, n in ((1.5, "Y1"), (-1.5, "Y2"))]
        for line in self.cx + self.cy:
            line.setVisible(False)
            line.sigPositionChanged.connect(self.update_cursor_readout)
            self.plot.addItem(line)

        self.fft_plot = pg.PlotWidget()
        self.fft_plot.setBackground("k")
        self.fft_plot.showGrid(x=True, y=True, alpha=0.3)
        self.fft_plot.setLabel("bottom", "Frequency", units="Hz")
        self.fft_curve = self.fft_plot.plot(pen=pg.mkPen(MATH_COLOR, width=1))
        self.fft_peak = pg.TextItem(color=MATH_COLOR, anchor=(-0.05, 0))
        self.fft_plot.addItem(self.fft_peak)
        self.fft_plot.setVisible(False)

        # status strip under the plot: settings line + one live line per channel
        self.info = QLabel("not connected")
        self.live = {k: QLabel("") for k in (1, 2, "math")}
        self.live["math"].setVisible(False)  # shown while a math function is on
        strip = QWidget()
        strip.setObjectName("strip")
        strip.setStyleSheet("#strip { background: black; } QLabel { color: #ddd; "
                            "font-family: monospace; font-size: 9pt; padding: 0 2px; }")
        sl = QVBoxLayout(strip)
        sl.setContentsMargins(4, 4, 4, 4)
        sl.setSpacing(2)
        for lbl in (self.info, *self.live.values()):
            lbl.setTextFormat(Qt.RichText)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            # don't let long lines force the window wider; they clip instead
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lbl.setMinimumWidth(1)
            sl.addWidget(lbl)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.plot)
        split.addWidget(self.fft_plot)
        split.setSizes([600, 300])
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(split, 1)
        ll.addWidget(strip)

        # controls --------------------------------------------------------
        ctl = QWidget()
        cl = QVBoxLayout(ctl)
        # which scope (USB port path; stays the same across replugs)
        devrow = QHBoxLayout()
        self.dev_combo = QComboBox()
        self.dev_combo.setToolTip("Scope to connect to, by USB port. 'Auto' uses the first one found.")
        self.btn_refresh = QPushButton("↻")
        self.btn_refresh.setFixedWidth(32)
        self.btn_refresh.setToolTip("Rescan USB for scopes")
        self.btn_refresh.clicked.connect(self.refresh_devices)
        self.dev_combo.activated.connect(self._remember_device)
        devrow.addWidget(QLabel("Scope"))
        devrow.addWidget(self.dev_combo, 1)
        devrow.addWidget(self.btn_refresh)
        cl.addLayout(devrow)
        # row 1: connection, USB streaming, one frame into the display
        self.connected = False
        row1 = QHBoxLayout()
        self.btn_conn = QPushButton()
        self.btn_conn.clicked.connect(self.toggle_connect)
        self.btn_live = QPushButton()
        self.btn_live.setCheckable(True)
        self.btn_live.toggled.connect(self._stream_toggled)
        self.btn_step = QPushButton("SingleFrame")
        self.btn_step.setToolTip("Read one frame from the scope into this display "
                                 "(use while Stream is OFF). Doesn't change the scope.")
        self.btn_step.clicked.connect(lambda: self.worker.submit("capture", True))
        for b in (self.btn_conn, self.btn_live, self.btn_step):
            row1.addWidget(b)
        cl.addLayout(row1)

        def key_button(label, key, tip):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, k=key: self.worker.submit("key", k))
            return b
        self.key_button = key_button

        # row 2: scope acquisition keys
        row2 = QHBoxLayout()
        self.btn_run = key_button("Run/Stop", "CT-RS-KEY",
                                  "Green: the scope captures continuously - click to stop.\n"
                                  "Red: stopped (or waiting for SingleShot) - click to run.")
        self.btn_single = key_button("SingleShot", "CT-SINGLESEQ-KEY",
                                     "Stop continuous capture and wait for one trigger; the new "
                                     "waveform is shown and the scope stays stopped")
        self.key_buttons = [key_button("Autoset", "CT-AUTOSET-KEY", "Scope Autoset"),
                            self.btn_run, self.btn_single]
        for b in self.key_buttons:
            row2.addWidget(b)
        cl.addLayout(row2)
        self._style_state_buttons()

        self.tabs = QTabWidget()
        # 7 tabs: keep them all visible without scroll arrows
        self.tabs.setUsesScrollButtons(False)
        self.tabs.tabBar().setStyleSheet("QTabBar::tab { padding: 4px 6px; }")
        vert = QWidget()
        vl = QVBoxLayout(vert)
        self.ch_boxes = {ch: ChannelBox(ch, self) for ch in (1, 2)}
        for b in self.ch_boxes.values():
            vl.addWidget(b)
        vl.addStretch(1)
        self.trig = TriggerPanel(self)
        self.acq = AcquirePanel(self)
        self.math = MathPanel(self)
        self.cursors = CursorPanel(self)
        self.refpanel = RefPanel(self)

        # "More…" tab: less frequent actions
        more = QWidget()
        ml = QVBoxLayout(more)
        mb = QGroupBox("Scope")
        mg = QVBoxLayout(mb)
        self.btn_defaults = QPushButton("Reset Defaults")
        self.btn_defaults.setToolTip("Scope 'Default Setup': resets ALL scope settings (asks first)")
        self.btn_defaults.clicked.connect(self._defaults)
        self.btn_menu = key_button("Scope Menu on/off", "FN-0-KEY", "Show / hide the scope's side menu "
                                   "(the scope's screen is 16 divisions wide with it, 19.2 without)")
        self.btn_shot = QPushButton("Scope Screenshot")
        self.btn_shot.setToolTip("Grab the scope's 800×480 screen")
        self.btn_shot.clicked.connect(lambda: self.worker.submit("screenshot"))
        for b in (self.btn_defaults, self.btn_menu, self.btn_shot):
            mg.addWidget(b)
        ml.addWidget(mb)
        db = QGroupBox("Data")
        dg = QVBoxLayout(db)
        self.btn_csv = QPushButton("Save CSV…")
        self.btn_csv.setToolTip("Save the displayed waveforms (and math) as CSV")
        self.btn_csv.clicked.connect(self.save_csv)
        dg.addWidget(self.btn_csv)
        ml.addWidget(db)
        ml.addStretch(1)

        for w, name in ((vert, "Vertical"), (self.trig, "Trigger"), (self.acq, "Acquire"),
                        (self.math, "Math"), (self.cursors, "Measure"), (self.refpanel, "Refs"),
                        (more, "More…")):
            self.tabs.addTab(w, name)
        cl.addWidget(self.tabs, 1)

        scroll = QScrollArea()
        scroll.setWidget(ctl)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(440)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        central = QWidget()
        cen = QHBoxLayout(central)
        cen.addWidget(left, 1)
        cen.addWidget(scroll)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        self.addAction(quit_act)
        self.trace_source = "usb"   # or "screen": traces extracted from a screenshot
        self.pos_preview: dict[int, int | None] = {1: None, 2: None}
        self.level_preview: int | None = None
        self._level_since = 0.0
        self._preview_since: dict[int, float] = {1: 0.0, 2: 0.0}
        self.scope_widgets = [self.btn_live, self.btn_step, *self.key_buttons, self.btn_defaults,
                              self.btn_menu, self.btn_shot, self.trig, self.acq,
                              *self.ch_boxes.values()]
        self.set_controls_enabled(False)
        self.prefs = QSettings("hantek-dso", "gui")
        self.refresh_devices()
        if autoconnect:
            self.worker.submit("connect", self.selected_device())

    # ---------------------------------------------------------------- slots
    @Slot(object)
    def on_full_record(self, rec):
        self.statusBar().showMessage(f"Full record: {len(rec.t)} points", 6000)
        FullRecordDialog(rec, self).show()

    @Slot(object)
    def on_screenshot(self, img):
        ScreenshotDialog(img, self).exec()

    @Slot(str)
    def on_message(self, m):
        self.statusBar().showMessage(m, 6000)

    def set_controls_enabled(self, on: bool):
        for w in self.scope_widgets:
            w.setEnabled(on)

    # ---------------------------------------------------------------- device
    def refresh_devices(self):
        from .transport import list_devices
        wanted = self.selected_device() or self.prefs.value("device", "", str)
        self.dev_combo.clear()
        self.dev_combo.addItem("Auto (first scope found)", "")
        for d in list_devices(with_product=False):
            self.dev_combo.addItem(f"USB port {d['port']}  (bus {d['bus']}, addr {d['address']})", d["port"])
        i = self.dev_combo.findData(wanted)
        if i < 0 and wanted:  # remembered scope not plugged in right now
            self.dev_combo.addItem(f"USB port {wanted}  (not found)", wanted)
            i = self.dev_combo.count() - 1
        self.dev_combo.setCurrentIndex(max(i, 0))

    def selected_device(self) -> str | None:
        if not hasattr(self, "dev_combo") or self.dev_combo.count() == 0:
            return None
        return self.dev_combo.currentData() or None

    def _remember_device(self, *_):
        self.prefs.setValue("device", self.dev_combo.currentData() or "")

    # ---------------------------------------------------------------- state buttons
    GREEN = "QPushButton { background: #2e7d32; color: white; font-weight: bold; }"
    RED = "QPushButton { background: #c62828; color: white; font-weight: bold; }"
    GREY = "QPushButton { background: #9e9e9e; color: #eee; font-weight: bold; }"

    def _style_state_buttons(self):
        if self.connected:
            self.btn_conn.setText("Connected")
            self.btn_conn.setStyleSheet(self.GREEN)
            self.btn_conn.setToolTip("Connected to the scope - click to disconnect")
        else:
            self.btn_conn.setText("Disconnected")
            self.btn_conn.setStyleSheet(self.RED)
            self.btn_conn.setToolTip("Not connected - click to connect")
        on = self.btn_live.isChecked()
        self.btn_live.setText("Stream ON" if on else "Stream OFF")
        self.btn_live.setStyleSheet(self.GREY if not self.connected else self.GREEN if on else self.RED)
        self.btn_live.setToolTip("Streaming waveforms from the scope over USB - click to pause"
                                 if on else "Display paused - click to stream from the scope "
                                 "(Single Step reads one frame)")

    ORANGE = "QPushButton { background: #ef6c00; color: white; font-weight: bold; }"

    @Slot(bool, bool)
    def on_run_state(self, stopped: bool, armed: bool):
        """Mirror the scope's Run/Stop light: green = continuous, red = stopped
        or waiting for a single trigger. SingleShot glows orange while armed."""
        running = not stopped and not armed
        self.btn_run.setStyleSheet(self.GREEN if running else self.RED)
        self.btn_run.setText("Run/Stop")
        self.btn_single.setStyleSheet(self.ORANGE if armed else "")
        self.btn_single.setText("Waiting for trigger…" if armed else "SingleShot")

    def _stream_toggled(self, on: bool):
        self.worker.submit("live", on)
        self._style_state_buttons()

    def _defaults(self):
        if QMessageBox.question(
                self, "Default setup",
                "Reset ALL scope settings to the factory default setup?\n"
                "(Same as the scope's Default Setup key.)") == QMessageBox.Yes:
            self.worker.submit("key", "CT-DS-KEY")

    def toggle_connect(self):
        if not self.connected:
            self.worker.submit("connect", self.selected_device())
        else:
            self.btn_live.setChecked(False)
            self.worker.submit("disconnect")

    @Slot(str)
    def on_connected(self, msg):
        self.connected = True
        self.dev_combo.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.set_controls_enabled(True)
        self.statusBar().showMessage(msg, 5000)
        self.btn_live.setChecked(True)
        self._style_state_buttons()

    @Slot(str)
    def on_disconnected(self, why):
        self.connected = False
        self.btn_run.setStyleSheet("")
        self.btn_single.setStyleSheet("")
        self.btn_single.setText("SingleShot")
        self.dev_combo.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.btn_live.setChecked(False)
        self.set_controls_enabled(False)
        self.info.setText(f"not connected ({why})")
        for lbl in self.live.values():
            lbl.setText("")
        self.live["math"].setVisible(False)
        self._style_state_buttons()

    def preview_position(self, ch: int, counts: int):
        """Show where channel ch will sit (slider being dragged / update in flight)."""
        self.pos_preview[ch] = counts
        self._preview_since[ch] = time.monotonic()
        if self.last_settings:
            self.redraw()
            self.update_markers(self.last_settings)

    def preview_level(self, counts: int):
        """Show where the trigger level line will be (slider dragged / update in flight)."""
        self.level_preview = counts
        self._level_since = time.monotonic()
        if self.last_settings:
            self.update_markers(self.last_settings)

    def _pos_offset(self, ch: int, st: Settings) -> float:
        """Display offset (divisions) between the previewed and the scope's position."""
        p = self.pos_preview.get(ch)
        return 0.0 if p is None else (p - st.position(ch)) / COUNTS_PER_DIV

    def _expire_previews(self, st: Settings):
        """Drop a position preview once the scope reports that position (or after 3 s)."""
        for ch, p in self.pos_preview.items():
            if p is None or self.ch_boxes[ch].pos_busy():
                continue
            if st.position(ch) == p or time.monotonic() - self._preview_since[ch] > 3:
                self.pos_preview[ch] = None
        lp = self.level_preview
        if lp is not None and not self.trig.level_busy() and \
                (st["TRIG-VPOS"] == lp or time.monotonic() - self._level_since > 3):
            self.level_preview = None

    @Slot(object)
    def on_settings(self, st: Settings):
        self._expire_previews(st)
        self.last_settings = st
        for b in self.ch_boxes.values():
            b.update_from(st)
        self.trig.update_from(st)
        self.acq.update_from(st)
        self.update_markers(st)
        self.update_info()

    @Slot(object, object)
    def on_frame(self, st: Settings, data: dict):
        self._expire_previews(st)
        if self.last_settings is None or st.raw != self.last_settings.raw:
            self.on_settings(st)
        self.last_settings = st
        self.trig.update_status(st)
        data = dict(data)
        from_screen = data.pop("_screen", False)
        if from_screen:
            self.trace_source = "screen"
        elif data:
            self.trace_source = "usb"
        # hold the previous trace of an enabled channel that returned no new data
        merged = {ch: a for ch, a in self.last_data.items() if st.channel_on(ch)}
        merged.update(data)
        self.last_data = merged
        self.redraw()

    # ---------------------------------------------------------------- drawing
    def record_length(self) -> int:
        return max((len(a) for a in self.last_data.values()), default=0)

    def volts(self, ch: int) -> np.ndarray | None:
        a = self.last_data.get(ch)
        if a is None or self.last_settings is None:
            return None
        return self.last_settings.to_volts(ch, a)

    def _match_scope_width(self, st: Settings, n: int):
        """The scope shows 16 divisions with its side menu open and 19.2 with it
        hidden, and the USB record follows the screen; show the same width."""
        half = max(HORIZ_DIVS / 2, round(n / st.points_per_div(n) / 2, 1))
        if half != self._x_half:
            self._x_half = half
            self.plot.setXRange(-half, half, padding=0)
            self.update_markers(st)

    def redraw(self, *_):
        st = self.last_settings
        if st is None:
            return
        n = max((len(a) for a in self.last_data.values()), default=0)
        if n:
            self._match_scope_width(st, n)
        for ch, curve in self.curves.items():
            a = self.last_data.get(ch)
            if a is None or not len(a):
                curve.setData([], [])
                self.live[ch].setText(f"{self._ch_tag(ch)}  <span style='color:#777'>off</span>")
                continue
            x = st.x_divs(len(a))
            # clamp to the screen like the scope does, so saturated traces stay visible
            y = a.astype(float) / COUNTS_PER_DIV + self._pos_offset(ch, st)
            curve.setData(x, np.clip(y, -VERT_DIVS / 2, VERT_DIVS / 2))
            m = measure_values(st.time_axis(len(a)), st.to_volts(ch, a))
            clipped = bool((np.abs(a.astype(int)) >= 126).any())
            self.live[ch].setText(self._live_line(self._ch_tag(ch), m, clipped))
        self.draw_math(st)
        self.draw_refs()
        self.update_cursor_readout()
        self.acq.update_info(st)
        self.update_info()

    @staticmethod
    def _tag(text: str, color) -> str:
        """Coloured 4-character line label ('CH1 ', 'CH2 ', 'MATH') so all
        readout lines share the same columns."""
        r, g, b = color
        return f"<b style='color:rgb({r},{g},{b})'>{text.ljust(4).replace(' ', '&nbsp;')}</b>"

    @classmethod
    def _ch_tag(cls, ch: int) -> str:
        return cls._tag(f"CH{ch}", CH_COLORS[ch])

    def _live_line(self, tag: str, m: dict, clipped: bool = False, unit: str = "V",
                   slot: str | None = None) -> str:
        """One fixed-width line of live values; every field keeps its column.
        `slot` is the 4-character field after the tag (CLIP warning on channel
        lines, function code on the math line)."""
        def f(label, key, u):
            # 2-character units (V²) get 3 significant digits to keep 8 columns
            return f"{label} {fmt_fixed(m.get(key), u, 8, 4 if len(u) == 1 or u == 'Hz' else 3)}"
        duty = m.get("duty")
        fields = [
            f("Vpp", "vpp", unit), f("Max", "vmax", unit), f("Min", "vmin", unit),
            f("Avg", "mean", unit), f("RMS", "rms", unit), f("Freq", "freq", "Hz"),
            "Duty " + ("—".rjust(6) if duty is None else f"{duty:5.1f}%"),
            f("Rise", "rise", "s"),
        ]
        text = " ".join(fields).replace(" ", "&nbsp;")
        # fixed 4-char slot so the warning never shifts the columns or gets clipped off
        if slot is None:
            slot = "<b style='color:#f44'>CLIP</b>" if clipped else "&nbsp;" * 4
        return f"{tag}&nbsp;{slot}&nbsp;{text}"

    def _math_line(self, html: str):
        self.live["math"].setText(f"{self._tag('MATH', MATH_COLOR)}&nbsp;" + "&nbsp;" * 4 + "&nbsp;" + html)
        self.live["math"].setVisible(True)

    def draw_math(self, st: Settings):
        op = self.math.op.currentText()
        self.math_v = None
        self.math_curve.setData([], [])
        is_fft = op.startswith("FFT")
        self.fft_plot.setVisible(is_fft)
        if op == "Off":
            self.math.meas.setText("")
            self.live["math"].setVisible(False)
            return
        if is_fft:
            ch = 1 if op.endswith("CH1") else 2
            v = self.volts(ch)
            if v is None:
                self.math.meas.setText(f"CH{ch} is off")
                self._math_line(f"<span style='color:#777'>{op}: CH{ch} is off</span>")
                return
            dt = st.sample_interval(len(v))
            db = self.math.fft_db.isChecked()
            log = self.math.fft_log.isChecked()
            f, a = fft(v, dt, self.math.win_fn.currentText(), db)
            if not len(f):
                return
            self.fft_plot.setLogMode(x=log, y=False)
            self.fft_curve.setData(f[1:] if log else f, a[1:] if log else a)
            self.fft_plot.setLabel("left", "dBV" if db else "V rms")
            pf, pa = peak(f, a)
            txt = f"{fmt_si(round(pf, 4 - int(np.floor(np.log10(pf))) if pf > 0 else 0), 'Hz')}  " + \
                (f"{pa:.2f} dBV" if db else fmt_si(pa, "V"))
            self.fft_peak.setText("peak " + txt)
            self.fft_peak.setPos(np.log10(pf) if log and pf > 0 else pf, pa)
            self.math.meas.setText(f"Readout: MATH line under the display.<br>"
                                   f"Bin width {fmt_si(f[1], 'Hz')}, span {fmt_si(f[-1], 'Hz')}")
            amp = f"{pa:7.2f} dBV" if db else fmt_fixed(pa, "Vrms", 11)
            line = (f"{op:<8}  Peak {fmt_fixed(pf, 'Hz', 8)} {amp}  Bin {fmt_fixed(f[1], 'Hz', 8)}  "
                    f"Span {fmt_fixed(f[-1], 'Hz', 8)}  Window {self.math.win_fn.currentText()}")
            self._math_line(line.replace(" ", "&nbsp;"))
            return
        v = math_channel(op, self.volts(1), self.volts(2))
        if v is None:
            self.math.meas.setText("needs CH1 and CH2 on")
            self._math_line(f"<span style='color:#777'>{op}: needs CH1 and CH2 on</span>")
            return
        self.math_v = v
        unit = "V²" if "×" in op else "V"
        scale = self.math.scale_value()
        if scale is None:  # auto: fit about 6 divisions
            span = float(np.ptp(v)) or 1.0
            steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]
            scale = next((s for s in steps if span / s <= 6), steps[-1])
        self.math_scale = scale
        x = st.x_divs(len(v))
        y = np.clip(v / scale + self.math.offset.value(), -VERT_DIVS / 2, VERT_DIVS / 2)
        self.math_curve.setData(x, y)
        m = measure_values(st.time_axis(len(v)), v)
        self.math.meas.setText(f"<b>{op}</b> displayed at {fmt_si(scale, unit)}/div.<br>"
                               f"Readout: MATH line under the display.")
        # same columns as the CH1/CH2 lines; the 4-char slot (CLIP on channel
        # lines) shows the function as a short code: 1+2, 1−2, 2−1, 1×2
        code = op.replace("CH", "").replace(" ", "")
        r, g, b = MATH_COLOR
        slot = f"<span style='color:rgb({r},{g},{b})'>{code.ljust(4).replace(' ', '&nbsp;')}</span>"
        self.live["math"].setText(self._live_line(self._tag("MATH", MATH_COLOR), m, unit=unit, slot=slot))
        self.live["math"].setVisible(True)

    def draw_refs(self):
        for name, curve in self.ref_curves.items():
            ref = self.refs[name]
            row = self.refpanel.rows[name]
            if ref is None or not row["show"].isChecked():
                curve.setData([], [])
                continue
            x, y = ref.to_divs()
            curve.setData(x, np.clip(y, -VERT_DIVS / 2, VERT_DIVS / 2))

    def update_markers(self, st: Settings):
        # trigger point: HORIZ-TRIGTIME (ps) shifts it left of screen centre
        tdiv = st.seconds_per_div() or 1.0
        x = -st.get("HORIZ-TRIGTIME", 0) * 1e-12 / tdiv
        self.trig_line.setValue(max(-self._x_half, min(self._x_half, x)))
        ch = st.trigger_channel()
        if ch:
            self.level_line.show()
            self.level_line.setPen(pg.mkPen(CH_COLORS[ch], style=Qt.DotLine))
            if not self.level_line.moving:
                vpos = self.level_preview if self.level_preview is not None else st["TRIG-VPOS"]
                self.level_line.setValue(vpos / COUNTS_PER_DIV)
        else:
            self.level_line.hide()
        for c, g in self.gnd.items():
            if st.channel_on(c):
                y = st.position(c) / COUNTS_PER_DIV + self._pos_offset(c, st)
                y = max(-VERT_DIVS / 2, min(VERT_DIVS / 2, y))
                g.setData([-self._x_half + 0.15], [y])
            else:
                g.setData([], [])

    def on_level_dragged(self):
        st = self.last_settings
        if st and st.trigger_channel():
            c = max(-250, min(250, int(round(self.level_line.value() * COUNTS_PER_DIV))))
            self.preview_level(c)              # keep the line where it was dropped
            self.trig._updating = True         # move the slider without sending again
            self.trig.level.setValue(max(-100, min(100, c)))
            self.trig._updating = False
            self.trig._level_text(c)
            self.worker.submit("set", **{"TRIG-VPOS": c})

    # ---------------------------------------------------------------- cursors
    def update_cursors(self, *_):
        for line in self.cx:
            line.setVisible(self.cursors.time_on.isChecked())
        for line in self.cy:
            line.setVisible(self.cursors.volt_on.isChecked())
        self.update_cursor_readout()

    def _div_to_volts(self, ydiv: float) -> tuple[float, str] | None:
        st = self.last_settings
        src = self.cursors.src.currentText()
        if st is None:
            return None
        if src == "Math":
            if self.math_v is None:
                return None
            unit = "V²" if "×" in self.math.op.currentText() else "V"
            return (ydiv - self.math.offset.value()) * self.math_scale, unit
        ch = int(src[-1])
        vdiv = st.volts_per_div(ch) or 0
        return (ydiv - st.position(ch) / COUNTS_PER_DIV) * vdiv, "V"

    def update_cursor_readout(self, *_):
        st = self.last_settings
        lines = []
        if st and self.cursors.time_on.isChecked():
            tdiv = st.seconds_per_div() or 0
            t1, t2 = (c.value() * tdiv for c in self.cx)
            dt = t2 - t1
            lines.append(f"X1 {fmt_si(t1, 's'):>10}  X2 {fmt_si(t2, 's'):>10}")
            lines.append(f"ΔT {fmt_si(dt, 's'):>10}  1/ΔT {fmt_si(1 / dt, 'Hz') if dt else '—':>10}")
            # trace values at the time cursors
            for ch in (1, 2):
                v = self.volts(ch)
                if v is not None and len(v):
                    ppd = st.points_per_div(len(v))
                    idx = [int(np.clip(round(c.value() * ppd + len(v) / 2), 0, len(v) - 1))
                           for c in self.cx]
                    lines.append(f"CH{ch} @X1 {fmt_si(v[idx[0]], 'V'):>9}  @X2 {fmt_si(v[idx[1]], 'V'):>9}")
        if st and self.cursors.volt_on.isChecked():
            r1, r2 = (self._div_to_volts(c.value()) for c in self.cy)
            if r1 and r2:
                unit = r1[1]
                lines.append(f"Y1 {fmt_si(r1[0], unit):>10}  Y2 {fmt_si(r2[0], unit):>10}")
                lines.append(f"ΔV {fmt_si(r1[0] - r2[0], unit):>10}")
            else:
                lines.append("voltage source has no data")
        self.cursors.readout.setText("\n".join(lines))

    # ---------------------------------------------------------------- references
    def _trace_for(self, src: str) -> tuple[np.ndarray, float, float] | None:
        """(volts, volts/div, offset_div) of a live trace."""
        st = self.last_settings
        if st is None:
            return None
        if src == "Math":
            if self.math_v is None:
                return None
            return self.math_v, self.math_scale, self.math.offset.value()
        ch = int(src[-1])
        v = self.volts(ch)
        if v is None:
            return None
        return v, st.volts_per_div(ch) or 1.0, st.position(ch) / COUNTS_PER_DIV

    def store_ref(self, name: str, src: str):
        tr = self._trace_for(src)
        if tr is None:
            QMessageBox.information(self, "Reference", f"{src} has no data.")
            return
        v, vdiv, off = tr
        st = self.last_settings
        self.refs[name] = Reference(f"{src} {time.strftime('%H:%M:%S')}", st.time_axis(len(v)), v.copy(),
                                    vdiv, st.seconds_per_div() or 1.0, off)
        self._ref_changed(name)

    def load_ref(self, name: str):
        fn, _ = QFileDialog.getOpenFileName(self, f"Load REF {name}", "", "CSV (*.csv);;All files (*)")
        if not fn:
            return
        try:
            st = self.last_settings
            self.refs[name] = Reference.load_csv(fn, default_tdiv=(st.seconds_per_div() if st else 1e-3) or 1e-3)
        except Exception as e:
            QMessageBox.warning(self, "Reference", f"Cannot load {fn}:\n{e}")
            return
        self._ref_changed(name)

    def export_ref(self, name: str):
        ref = self.refs[name]
        if ref is None:
            return
        fn, _ = QFileDialog.getSaveFileName(self, f"Export REF {name}", f"ref_{name}.csv", "CSV (*.csv)")
        if fn:
            ref.save_csv(fn)

    def clear_ref(self, name: str):
        self.refs[name] = None
        self._ref_changed(name)

    def _ref_changed(self, name: str):
        ref = self.refs[name]
        row = self.refpanel.rows[name]
        if ref is None:
            row["info"].setText("empty")
        else:
            row["info"].setText(f"{ref.name}: {fmt_si(ref.volts_per_div, 'V')}/div, "
                                f"{fmt_si(ref.seconds_per_div, 's')}/div, {len(ref.v)} pts")
            row["show"].setChecked(True)
        self.redraw()

    # ---------------------------------------------------------------- misc
    def update_info(self):
        st = self.last_settings
        if not st:
            return
        parts = []
        for ch in (1, 2):
            if st.channel_on(ch):
                parts.append(f"{self._ch_tag(ch)} {fmt_si(st.volts_per_div(ch), 'V')}/div "
                             f"{COUPLING[min(st[f'VERT-CH{ch}-COUP'], 2)]} {st.probe(ch)}×")
            else:
                parts.append(f"{self._ch_tag(ch)} <span style='color:#777'>off</span>")
        tdiv = st.seconds_per_div()
        if tdiv:
            parts.append(f"{fmt_si(tdiv, 's')}/div")
        n = self.record_length()
        if n and tdiv and self.trace_source == "usb":
            parts.append(f"{n} pts @ {fmt_si(1 / st.sample_interval(n), 'Sa/s')}")
        lv = st.trigger_level_volts()
        src = ("CH1", "CH2", "EXT", "EXT/5", "AC50")[min(st["TRIG-SRC"], 4)]
        trig = f"Trigger {st.trigger_state()} · {src}"
        if lv is not None:
            trig += f" {fmt_si(round(lv, 3), 'V')}"
        parts.append(trig)
        self.screen_note.setPos(self._x_half - 0.1, -VERT_DIVS / 2 + 0.1)
        self.screen_note.setVisible(self.trace_source == "screen")
        self.info.setText("&nbsp;&nbsp;&nbsp;".join(parts))

    def save_csv(self):
        st, data = self.last_settings, self.last_data
        if not st or not data:
            QMessageBox.information(self, "Save CSV", "No waveform captured yet.")
            return
        fn, _ = QFileDialog.getSaveFileName(self, "Save waveform", "waveform.csv", "CSV (*.csv)")
        if not fn:
            return
        n = min(len(a) for a in data.values())
        cols, hdr = [st.time_axis(n)], ["time_s"]
        for ch in sorted(data):
            cols.append(st.to_volts(ch, data[ch][:n]))
            hdr.append(f"CH{ch}_V")
        if self.math_v is not None and len(self.math_v) >= n:
            cols.append(self.math_v[:n])
            hdr.append("MATH")
        np.savetxt(fn, np.column_stack(cols), delimiter=",", header=",".join(hdr),
                   comments="", fmt="%.9g")
        self.statusBar().showMessage(f"saved {fn}", 5000)

    def closeEvent(self, ev):
        self.worker.running = False
        self.thread.quit()
        self.thread.wait(3000)
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
