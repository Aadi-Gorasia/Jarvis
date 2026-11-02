"""Menu bar app. This is the normal way to run Jarvis: the same assistant, plus a status icon, a pause
switch, a list of what the agents are doing, one-click approvals, and a box to type to Jarvis."""
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

import rumps

from .config import load_config
from .halo import Halo
from .util import LOG_PATH, PROJECT_ROOT, acquire_single_instance, notify, setup_logging

log = logging.getLogger("jarvis.menubar")

ICONS = {"starting": "⏳", "listening": "🎙", "recording": "🔴", "thinking": "🧠",
         "speaking": "🔊", "paused": "⏸", "error": "⚠️"}
LABELS = {"starting": "Starting...", "listening": "Listening for “Hey Jarvis”", "recording": "Listening to you...",
          "thinking": "Thinking...", "speaking": "Speaking...", "paused": "Paused (microphone off)",
          "error": "Problem: choose Open log"}
BRAINS = {"gemini": "Gemini", "openai": "ChatGPT", "ollama": "On-device model"}
MODES = {"ask": "Ask me for everything", "trusted": "Trusted (ask only for risky things)", "full": "Full (never ask)"}


def title_for(state: str, running: int, pending: int) -> str:
    icon = "🔔" if pending else ICONS.get(state, "🎙")
    return f"{icon} {running}" if running else icon


class JarvisMenu(rumps.App):
    def __init__(self, cfg):
        super().__init__("Jarvis", title=ICONS["starting"], quit_button=None)
        from .assistant import Jarvis

        self.cfg = cfg
        self.jarvis = Jarvis(cfg)
        # items without a callback show greyed out: used as plain status lines
        self.status_item = rumps.MenuItem("Starting...")
        self.brain_item = rumps.MenuItem("Brain: -")
        self.pause_item = rumps.MenuItem("Pause listening", callback=self.toggle_pause)
        self.agents_menu = rumps.MenuItem("Agents")
        self.halo = Halo(cfg)
        self.halo_item = rumps.MenuItem("Screen halo", callback=self.toggle_halo)
        self.halo_item.state = 1 if self.halo.enabled else 0
        self.mode_menu = rumps.MenuItem("Agent permissions")
        self.mode_items = {}
        for key, label in MODES.items():
            item = rumps.MenuItem(label, callback=lambda sender, k=key: self.set_mode(k))
            self.mode_items[key] = item
            self.mode_menu.add(item)
        self._mark_mode()
        self.menu = [
            self.status_item, self.brain_item, None,
            self.pause_item, rumps.MenuItem("Type to Jarvis...", callback=self.type_to_jarvis),
            rumps.MenuItem("Stop speaking", callback=self.stop_speaking), None,
            self.agents_menu, self.mode_menu, self.halo_item, None,
            rumps.MenuItem("Open workspace folder", callback=self.open_workspace),
            rumps.MenuItem("Open log", callback=self.open_log), None,
            rumps.MenuItem("Quit Jarvis", callback=self.quit_app),
        ]
        self._agents_sig = None
        self._timer = rumps.Timer(self.refresh, 0.7)  # UI updates happen on the main thread
        self._timer.start()
        self._halo_timer = rumps.Timer(self._halo_tick, 1 / 30)
        self._halo_timer.start()

    # ── live status ──────────────────────────────────────────────
    def refresh(self, _sender=None) -> None:
        j = self.jarvis
        running = sum(1 for t in list(j.manager.tasks.values()) if t.status in ("queued", "running"))
        pending = len(j.broker.pending)
        self.title = title_for(j.state, running, pending)
        label = LABELS.get(j.state, j.state)
        self.status_item.title = f"{label}: {j.error}" if j.state == "error" and j.error else label
        self.brain_item.title = "Brain: " + BRAINS.get(j.brain.last_provider, "ready")
        self.pause_item.title = "Resume listening" if j.paused else "Pause listening"
        self._refresh_agents()

    def _refresh_agents(self) -> None:
        j = self.jarvis
        tasks = sorted(list(j.manager.tasks.values()), key=lambda t: -t.id)[:8]
        pending = list(j.broker.pending.values())
        sig = (tuple((t.id, t.status) for t in tasks), tuple(e["id"] for e in pending))
        if sig == self._agents_sig:
            return
        self._agents_sig = sig
        self.agents_menu.clear()
        for e in pending:  # one-click approvals for anything an agent is waiting on
            short = e["action"][:55]
            self.agents_menu.add(rumps.MenuItem(f"Approve: {short}",
                                                callback=lambda s, i=e["id"]: j.broker.resolve(i, True)))
            self.agents_menu.add(rumps.MenuItem(f"Deny: {short}",
                                                callback=lambda s, i=e["id"]: j.broker.resolve(i, False)))
            self.agents_menu.add(rumps.separator)
        for t in tasks:
            line = f"#{t.id} {t.agent}: {t.brief[:38]}"
            if t.status in ("queued", "running"):
                self.agents_menu.add(rumps.MenuItem(f"Stop {line}", callback=lambda s, i=t.id: j.manager.cancel(i)))
            else:
                self.agents_menu.add(rumps.MenuItem(f"{line} ({t.status})"))
        if not tasks and not pending:
            self.agents_menu.add(rumps.MenuItem("No agent tasks yet"))

    def _halo_tick(self, _sender=None) -> None:
        self.halo.tick(self.jarvis.state)

    # ── actions ──────────────────────────────────────────────────
    def toggle_pause(self, _sender) -> None:
        self.jarvis.paused = not self.jarvis.paused

    def stop_speaking(self, _sender) -> None:
        self.jarvis.speaker.stop()

    def toggle_halo(self, sender) -> None:
        on = not self.halo.enabled
        self.halo.set_enabled(on)
        sender.state = 1 if on else 0
        cfg_file = PROJECT_ROOT / "config.yaml"
        try:
            cfg_file.write_text(re.sub(r"(halo:\s*\n\s*enabled:\s*)(true|false)", rf"\g<1>{'true' if on else 'false'}",
                                       cfg_file.read_text(), count=1))
        except OSError:
            pass

    def type_to_jarvis(self, _sender) -> None:
        win = rumps.Window(message="What should Jarvis do?", title="Jarvis", default_text="",
                           ok="Send", cancel="Cancel", dimensions=(340, 24))
        resp = win.run()
        if resp.clicked and resp.text.strip():
            threading.Thread(target=self._ask_in_background, args=(resp.text.strip(),), daemon=True).start()

    def _ask_in_background(self, text: str) -> None:
        reply = self.jarvis.ask_text(text)
        notify("Jarvis", reply[:200])

    def set_mode(self, mode: str) -> None:
        if mode == "full" and not rumps.alert(
                title="Turn off approvals?",
                message="In Full mode agents act without ever asking you first. Backups and the hard safety blocks still apply.",
                ok="Enable Full mode", cancel="Cancel"):
            return
        self.cfg.agents.permission_mode = mode
        cfg_file = PROJECT_ROOT / "config.yaml"
        try:  # remember the choice across restarts
            cfg_file.write_text(re.sub(r'(permission_mode:\s*")[a-z]+(")', rf"\g<1>{mode}\g<2>", cfg_file.read_text(), count=1))
        except OSError:
            log.warning("could not save permission mode to config.yaml")
        self._mark_mode()

    def _mark_mode(self) -> None:
        for key, item in self.mode_items.items():
            item.state = 1 if key == str(self.cfg.agents.permission_mode).lower() else 0

    def open_workspace(self, _sender) -> None:
        ws = Path(self.cfg.agents.workspace).expanduser()
        ws.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(ws)])

    def open_log(self, _sender) -> None:
        subprocess.run(["open", str(LOG_PATH)])

    def quit_app(self, _sender) -> None:
        rumps.quit_application()
        threading.Timer(1.5, lambda: os._exit(0)).start()  # don't wait for busy agent threads


def main() -> None:
    setup_logging()
    cfg = load_config()
    if not acquire_single_instance():
        rumps.alert("Jarvis", "Jarvis is already running. Look for its icon in the menu bar.")
        return
    if not cfg.api_key:
        rumps.alert("Jarvis", "No Gemini API key found. Put GEMINI_API_KEY=... in the .env file next to config.yaml.")
        return
    try:  # menu bar only: no Dock icon
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        log.warning("could not hide the Dock icon")
    app = JarvisMenu(cfg)
    threading.Thread(target=app.jarvis.run, daemon=True, name="jarvis-audio").start()
    app.run()


if __name__ == "__main__":
    main()
