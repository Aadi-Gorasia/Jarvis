"""Screen-edge halo that moves with your voice. A dark vignette hugs every display; silver/graphite light
swells inward and ripples around the edges in step with the loudness of what you say, then follows Jarvis's own
voice while he answers, and swirls while he thinks. It is click-through, shows over full-screen apps and all
Spaces, and fades away when idle. Everything touching AppKit is lazy and guarded: if drawing ever fails the
halo turns itself off and Jarvis keeps working."""
import logging
import math
import time

log = logging.getLogger("jarvis.halo")

LAYERS = 6        # stacked translucent layers = a soft gradient with a rippling inner edge
SAMPLES = 36      # points per screen edge
# state -> window opacity target. Anything else (idle, paused, error) = no halo.
SHOW = {"recording": 1.0, "thinking": 0.9, "speaking": 1.0}


def wave(u: float, phase: float) -> float:
    """Travelling ripple pattern around the screen perimeter, 0..1."""
    return 0.5 + 0.5 * (0.55 * math.sin(2 * math.pi * 3 * u + phase * 2.1) + 0.45 * math.sin(2 * math.pi * 7 * u - phase * 3.3))


def edge_shape(edge: int, energy: float, phase: float, n: int = SAMPLES):
    """Ripple profile along one edge (computed once per frame, then scaled for every layer)."""
    amp = 0.25 + 0.75 * energy
    return [(1 - amp) + amp * wave((edge + i / n) / 4, phase) for i in range(n + 1)]


def edge_boundary(edge: int, w: float, h: float, depth: float, energy: float, burst: float, phase: float, n: int = SAMPLES, shape=None):
    """Inner boundary of one edge's glow as (x, y) points, running clockwise (0 top, 1 right, 2 bottom, 3 left).
    Quiet: a thin even glow. Loud: it surges inward and ripples."""
    shape = shape or edge_shape(edge, energy, phase, n)
    reach = depth * (0.20 + 0.80 * energy) * (1 + 0.5 * burst)
    pts = []
    for i in range(n + 1):
        f, d = i / n, reach * shape[i]
        if edge == 0:
            pts.append((w * f, h - d))
        elif edge == 1:
            pts.append((w - d, h * (1 - f)))
        elif edge == 2:
            pts.append((w * (1 - f), d))
        else:
            pts.append((d, h * f))
    return pts


def edge_outer(edge: int, w: float, h: float):
    """The screen-edge side of the strip, closing the polygon (corner to corner)."""
    return [[(w, h), (0, h)], [(w, 0), (w, h)], [(0, 0), (w, 0)], [(0, h), (0, 0)]][edge]


class Halo:
    def __init__(self, cfg):
        self.cfg = cfg.halo
        self.enabled = bool(self.cfg.enabled)
        self.alpha = 0.0         # window fade
        self.energy = 0.0        # smoothed voice level, 0..1
        self.burst = 0.0         # brief surge when a new phase starts (you begin, he begins)
        self.phase = 0.0
        self.state = "idle"
        self.visible = False
        self.broken = False
        self.windows = []
        self.views = []
        self._last = time.time()

    def tick(self, state: str, level: float = 0.0) -> None:
        """Call ~30 times a second from the main thread. level = loudness 0..1 of whoever is talking."""
        if not self.enabled or self.broken:
            return
        now = time.time()
        dt, self._last = min(now - self._last, 0.2), now
        if state in SHOW and state != self.state:
            self.burst = 1.0                       # you started talking / he started answering: surge
        self.state = state if state in SHOW else self.state
        target = SHOW.get(state, 0.0)
        self.alpha += (target - self.alpha) * (0.25 if target > self.alpha else 0.10)
        if state == "thinking":
            level = 0.30 + 0.12 * math.sin(now * 3.0)            # calm swirl while he thinks
        level = max(0.0, min(1.0, level * float(getattr(self.cfg, "sensitivity", 1.0))))
        self.energy += (level - self.energy) * (0.60 if level > self.energy else 0.14)   # snappy attack, soft release
        self.burst *= 0.90
        self.phase += dt * (0.8 + 2.4 * self.energy + (1.4 if state == "thinking" else 0.0)) * float(getattr(self.cfg, "speed", 1.0))
        try:
            self._apply()
        except Exception:
            log.exception("Halo failed; turning it off for this session")
            self.broken = True
            self._hide()

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if not on:
            self.alpha = self.energy = 0.0
            self._hide()

    # ── AppKit ───────────────────────────────────────────────────
    def _apply(self) -> None:
        if self.alpha < 0.01:
            if self.visible:
                self._hide()
            return
        if not self.visible:
            self._build()
        for win in self.windows:
            win.setAlphaValue_(self.alpha)
        for view in self.views:
            view.energy, view.burst, view.phase = self.energy, self.burst, self.phase
            view.setNeedsDisplay_(True)

    def _build(self) -> None:
        from AppKit import NSColor, NSScreen, NSWindow

        view_cls = _get_view_class()
        r, g, b = (c / 255.0 for c in self.cfg.color)
        self.windows, self.views = [], []
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
            view.energy, view.burst, view.phase = self.energy, self.burst, self.phase
            win.setContentView_(view)
            win.setAlphaValue_(0.0)
            win.orderFrontRegardless()
            self.windows.append(win)
            self.views.append(view)
        self.visible = True

    def _hide(self) -> None:
        for win in self.windows:
            try:
                win.orderOut_(None)
            except Exception:
                pass
        self.windows, self.views = [], []
        self.visible = False


_view_class = None


def _get_view_class():
    global _view_class
    if _view_class is None:
        from AppKit import NSBezierPath, NSColor, NSView

        class HaloView(NSView):
            def isOpaque(self):
                return False

            def drawRect_(self, rect):
                r, g, b, strength, thick = self.spec
                bounds = self.bounds()
                w, h = bounds.size.width, bounds.size.height
                reach = min(w, h) * thick * 2.2            # how deep the glow can swell when you are loud
                shapes = [edge_shape(e, self.energy, self.phase) for e in range(4)]   # trig once per frame, not per layer
                for k in range(LAYERS):
                    frac = (k + 1) / LAYERS
                    silver = frac ** 1.4                    # black at the screen edge, silver toward the inside
                    col = [c + (0.86 - c) * silver for c in (r, g, b)]
                    alpha = strength * 0.34 * (1 - frac) ** 1.15 + 0.22 * self.energy * frac * (1 - frac) * 2
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(col[0], col[1], col[2], min(alpha, 0.9)).setFill()
                    for edge in range(4):
                        pts = edge_boundary(edge, w, h, reach * frac, self.energy, self.burst, self.phase, shape=shapes[edge])
                        path = NSBezierPath.bezierPath()
                        a, bpt = edge_outer(edge, w, h)
                        path.moveToPoint_(a)
                        path.lineToPoint_(bpt)
                        for p in reversed(pts):
                            path.lineToPoint_(p)
                        path.closePath()
                        path.fill()
                # a thin bright rim along the rippling inner edge makes the motion easy to see
                NSColor.colorWithCalibratedWhite_alpha_(0.9, 0.12 + 0.45 * self.energy).setStroke()
                for edge in range(4):
                    pts = edge_boundary(edge, w, h, reach, self.energy, self.burst, self.phase, shape=shapes[edge])
                    rim = NSBezierPath.bezierPath()
                    rim.moveToPoint_(pts[0])
                    for p in pts[1:]:
                        rim.lineToPoint_(p)
                    rim.setLineWidth_(1.5)
                    rim.stroke()

        _view_class = HaloView
    return _view_class
