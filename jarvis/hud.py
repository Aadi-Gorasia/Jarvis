"""A small floating panel that appears whenever something is running: a live elapsed timer and what each task is
doing right now, e.g.  ⠹ 1:12  github #3 · auditing my-site.  It is click-through, sits under the menu bar on the
right, shows over full-screen apps, and disappears when nothing is running. Failures disable it, never Jarvis."""
import logging
import time

log = logging.getLogger("jarvis.hud")
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
W, ROW, PAD = 420, 22, 14
MAX_ROWS = 4


def fmt_elapsed(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def row_text(job: dict, tick: int) -> str:
    text = f"{SPIN[tick % len(SPIN)]} {fmt_elapsed(job['elapsed'])}  {job['who']} · {job['detail']}"
    return text if len(text) <= 58 else text[:57] + "…"


class TaskHud:
    def __init__(self, cfg):
        self.enabled = bool(cfg.hud.enabled)
        self.broken = False
        self.win = None
        self.rows = []
        self.shown_rows = 0
        self.visible = False

    def update(self, jobs) -> None:
        """Call ~4 times a second from the main thread with Jarvis.jobs()."""
        if not self.enabled or self.broken:
            return
        try:
            jobs = list(jobs)[:MAX_ROWS]
            if not jobs:
                self._hide()
                return
            self._ensure(len(jobs))
            tick = int(time.time() * 8)
            for label, job in zip(self.rows, jobs):
                label.setStringValue_(row_text(job, tick))
            self._show()
        except Exception:
            log.exception("Task panel failed; turning it off for this session")
            self.broken = True
            self._hide()

    # ── AppKit ───────────────────────────────────────────────────
    def _ensure(self, n: int) -> None:
        from AppKit import NSColor, NSFont, NSScreen, NSTextField, NSWindow

        height = n * ROW + 2 * PAD - 6
        if self.win is None:
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(((0, 0), (W, height)), 0, 2, False)
            win.setOpaque_(False)
            win.setBackgroundColor_(NSColor.clearColor())
            win.setIgnoresMouseEvents_(True)                    # never blocks a click
            win.setHasShadow_(True)
            win.setReleasedWhenClosed_(False)
            win.setLevel_(1002)                                 # above the halo and full-screen apps
            win.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 6) | (1 << 8))
            view = win.contentView()
            view.setWantsLayer_(True)
            layer = view.layer()
            layer.setCornerRadius_(14)
            layer.setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.90).CGColor())
            self.win = win
        if n != self.shown_rows:
            for label in self.rows:
                label.removeFromSuperview()
            self.rows = []
            for i in range(n):
                label = NSTextField.labelWithString_("")
                label.setTextColor_(NSColor.whiteColor())
                label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(13, 0.3))
                label.setFrame_(((PAD, height - PAD - (i + 1) * ROW + 3), (W - 2 * PAD, ROW - 2)))
                self.win.contentView().addSubview_(label)
                self.rows.append(label)
            self.win.setContentSize_((W, height))
            self.shown_rows = n
            screen = NSScreen.mainScreen().visibleFrame()
            self.win.setFrameOrigin_((screen.origin.x + screen.size.width - W - 16, screen.origin.y + screen.size.height - height - 10))

    def _show(self) -> None:
        if not self.visible:
            self.win.orderFrontRegardless()
            self.visible = True

    def _hide(self) -> None:
        if self.win is not None and self.visible:
            self.win.orderOut_(None)
        self.visible = False
