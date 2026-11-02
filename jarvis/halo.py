"""Screen-edge halo: a soft black/grey vignette that fades in around every display while you talk to Jarvis.
It is click-through (never blocks the mouse), shows over full-screen apps, and fades out when Jarvis is idle.
Everything touching AppKit happens lazily and inside try/except: if the halo ever fails it turns itself off
and Jarvis keeps working."""
import logging
import math
import time

log = logging.getLogger("jarvis.halo")

# state -> (strength 0-1, pulses per second). Steady while listening to you, breathing while thinking,
# a quicker pulse while speaking. Anything else (idle, paused, error) = no halo.
TARGETS = {"recording": (1.0, 0.0), "thinking": (0.85, 0.5), "speaking": (0.95, 1.4)}
_view_class = None


def target_for(state: str):
    return TARGETS.get(state, (0.0, 0.0))


def _get_view_class():
    global _view_class
    if _view_class is None:
        from AppKit import NSColor, NSGradient, NSMakeRect, NSView

        class HaloView(NSView):
            def isOpaque(self):
                return False

            def drawRect_(self, rect):
                r, g, b, strength, thick = self.spec
                bounds = self.bounds()
                w, h = bounds.size.width, bounds.size.height
                t = min(w, h) * thick
                dark = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, strength)
                clear = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 0.0)
                grad = NSGradient.alloc().initWithStartingColor_endingColor_(dark, clear)
                grad.drawInRect_angle_(NSMakeRect(0, h - t, w, t), 270.0)   # top edge, dark at the screen edge
                grad.drawInRect_angle_(NSMakeRect(0, 0, w, t), 90.0)        # bottom
                grad.drawInRect_angle_(NSMakeRect(0, 0, t, h), 0.0)         # left
                grad.drawInRect_angle_(NSMakeRect(w - t, 0, t, h), 180.0)   # right

        _view_class = HaloView
    return _view_class


class Halo:
    def __init__(self, cfg):
        self.cfg = cfg.halo
        self.enabled = bool(self.cfg.enabled)
        self.level = 0.0
        self.visible = False
        self.broken = False
        self.windows = []
        self._t0 = time.time()

    def tick(self, state: str) -> None:
        """Call ~30 times a second from the main thread."""
        if not self.enabled or self.broken:
            return
        strength, hz = target_for(state)
        self.level += (strength - self.level) * (0.28 if strength > self.level else 0.10)  # quick in, slow out
        pulse = 1.0 if hz == 0 else 0.80 + 0.20 * math.sin((time.time() - self._t0) * hz * 2 * math.pi)
        alpha = max(0.0, min(1.0, self.level * pulse))
        try:
            self._apply(alpha)
        except Exception:
            log.exception("Halo failed; turning it off for this session")
            self.broken = True
            self._hide()

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if not on:
            self.level = 0.0
            self._hide()

    # ── AppKit ───────────────────────────────────────────────────
    def _apply(self, alpha: float) -> None:
        if alpha < 0.01:
            if self.visible:
                self._hide()
            return
        if not self.visible:
            self._build()
        for win in self.windows:
            win.setAlphaValue_(alpha)

    def _build(self) -> None:
        from AppKit import NSColor, NSScreen, NSWindow

        r, g, b = (c / 255.0 for c in self.cfg.color)
        view_cls = _get_view_class()
        self.windows = []
        for screen in NSScreen.screens():
            frame = screen.frame()
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(frame, 0, 2, False, screen)
            win.setOpaque_(False)
            win.setBackgroundColor_(NSColor.clearColor())
            win.setIgnoresMouseEvents_(True)                    # clicks pass straight through
            win.setHasShadow_(False)
            win.setReleasedWhenClosed_(False)
            win.setLevel_(1000)                                 # above normal windows, even full-screen apps
            win.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 6) | (1 << 8))  # all Spaces, stationary, full-screen aux
            view = view_cls.alloc().initWithFrame_(((0, 0), (frame.size.width, frame.size.height)))
            view.spec = (r, g, b, float(self.cfg.intensity), float(self.cfg.thickness))
            win.setContentView_(view)
            win.setAlphaValue_(0.0)
            win.orderFrontRegardless()
            self.windows.append(win)
        self.visible = True

    def _hide(self) -> None:
        for win in self.windows:
            try:
                win.orderOut_(None)
            except Exception:
                pass
        self.windows = []
        self.visible = False
