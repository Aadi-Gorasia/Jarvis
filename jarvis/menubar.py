"""Menu bar app. This is the normal way to run Jarvis: the same assistant, plus a status icon, a pause
switch, a list of what the agents are doing, one-click approvals, and a box to type to Jarvis."""
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import rumps

from .config import load_config
from .halo import Halo
from .hud import TaskHud, fmt_elapsed
from .util import LOG_PATH, PROJECT_ROOT, acquire_single_instance, lower_priority, notify, on_battery, perf_text, setup_logging

log = logging.getLogger("jarvis.menubar")

ICONS = {"starting": "⏳", "listening": "🎙", "recording": "🔴", "thinking": "🧠",
         "speaking": "🔊", "paused": "⏸", "error": "⚠️", "blocked": "🔇"}
LABELS = {"starting": "Starting...", "listening": "Listening for “Hey Jarvis”", "recording": "Listening to you...",
          "thinking": "Thinking...", "speaking": "Speaking...", "paused": "Paused (microphone off)",
          "error": "Problem: choose Open log", "blocked": "Microphone blocked by macOS"}
BRAINS = {"gemini": "Gemini", "openai": "ChatGPT", "ollama": "On-device model"}
MODES = {"ask": "Ask me for everything", "trusted": "Trusted (ask only for risky things)", "full": "Full (never ask)"}


def title_for(state: str, running: int, pending: int, elapsed: float = None) -> str:
    """Menu bar text: the icon, plus a live timer for the longest-running job (and how many are running)."""
    icon = "🔔" if pending else ICONS.get(state, "🎙")
    if elapsed is None:
        return f"{icon} {running}" if running else icon
    return f"{icon} {fmt_elapsed(elapsed)}" + (f" ·{running}" if running > 1 else "")


class JarvisMenu(rumps.App):
    def __init__(self, cfg):
        super().__init__("Jarvis", title=ICONS["starting"], quit_button=None)
        from .assistant import Jarvis

        self.cfg = cfg
        self.jarvis = Jarvis(cfg)
        # items without a callback show greyed out: used as plain status lines
        self.status_item = rumps.MenuItem("Starting...")
        self.brain_item = rumps.MenuItem("Brain: -")
        self.msg_item = rumps.MenuItem("Messages: starting")
        self.perf_item = rumps.MenuItem("Jarvis: -")
        self._perf_t = 0.0
        self.pause_item = rumps.MenuItem("Pause listening", callback=self.toggle_pause)
        self.agents_menu = rumps.MenuItem("Agents")
        self.halo = Halo(cfg)
        self.hud = TaskHud(cfg)
        self._task_items = {}
        self.halo_item = rumps.MenuItem("Screen halo", callback=self.toggle_halo)
        self.hands_item = rumps.MenuItem("Hands-free (no wake word)", callback=self.toggle_hands_free)
        self.halo_item.state = 1 if self.halo.enabled else 0
        self.updates_item = rumps.MenuItem("Message updates", callback=self.toggle_updates)
        self.updates_item.state = 1 if cfg.social.enabled else 0
        self.mode_menu = rumps.MenuItem("Agent permissions")
        self.mode_items = {}
        for key, label in MODES.items():
            item = rumps.MenuItem(label, callback=lambda sender, k=key: self.set_mode(k))
            self.mode_items[key] = item
            self.mode_menu.add(item)
        self._mark_mode()
        self.menu = [
            self.status_item, self.brain_item, self.msg_item, self.perf_item, None,
            self.pause_item, self.hands_item, rumps.MenuItem("Type to Jarvis...", callback=self.type_to_jarvis),
            rumps.MenuItem("Stop speaking", callback=self.stop_speaking), None,
            self.agents_menu, self.mode_menu, self.halo_item, None,
            rumps.MenuItem("Check messages now", callback=self.check_messages), self.updates_item,
            rumps.MenuItem("Import ChatGPT / Gemini export...", callback=self.import_export),
            rumps.MenuItem("Set up message reading...", callback=self.allow_messages),
            rumps.MenuItem("Fix microphone access...", callback=self.fix_mic),
            rumps.MenuItem("Open workspace folder", callback=self.open_workspace),
            rumps.MenuItem("Open log", callback=self.open_log), None,
            rumps.MenuItem("Quit Jarvis", callback=self.quit_app),
        ]
        self._agents_sig = None
        self.hands_item.state = 1 if str(cfg.always_listen.enabled).lower() != "false" else 0
        self._timer = rumps.Timer(self.refresh, 0.7)  # UI updates happen on the main thread
        self._timer.start()
        self._halo_timer = rumps.Timer(self._halo_tick, 1 / float(cfg.performance.halo_fps))
        self._halo_timer.start()
        self._hud_timer = rumps.Timer(self._hud_tick, 0.25)
        self._hud_timer.start()

    # ── live status ──────────────────────────────────────────────
    def refresh(self, _sender=None) -> None:
        j = self.jarvis
        running = sum(1 for t in list(j.manager.tasks.values()) if t.status in ("queued", "running"))
        pending = len(j.broker.pending)
        jobs = j.jobs()
        self.title = title_for(j.state, running, pending, max((x["elapsed"] for x in jobs), default=None))
        label = LABELS.get(j.state, j.state)
        if j.state == "listening" and j.auto_active:
            label = "Quiet mode: say my name or \"wake up\"" if time.time() < j.asleep_until else "Listening: just talk to me"
        self.status_item.title = f"{label}: {j.error}" if j.state == "error" and j.error else label
        self.brain_item.title = "Brain: " + BRAINS.get(j.brain.last_provider, "ready")
        self.pause_item.title = "Resume listening" if j.paused else "Pause listening"
        w = j.webinbox
        if w.enabled:
            self.msg_item.title = {"ok": "Messages: reading your chat tabs", "needs_js_permission": "Messages: turn on JavaScript from Apple Events",
                                   "no_browser": "Messages: open your browser", "no_tabs": "Messages: open WhatsApp Web / Instagram in a tab",
                                   "starting": "Messages: starting"}.get(w.status, "Messages: " + w.status)
        else:
            self.msg_item.title = {"ok": "Messages: reading your notifications", "needs_full_disk_access": "Messages: needs Full Disk Access",
                                   "disabled": "Messages: off"}.get(j.notifier.status, "Messages")
        now = time.time()
        if now - self._perf_t > 5:                    # cheap: once every few seconds
            self._perf_t = now
            self.perf_item.title = "Jarvis uses " + perf_text()
            fps = float(self.cfg.performance.halo_fps_battery if (self.cfg.performance.battery_saver and on_battery()) else self.cfg.performance.halo_fps)
            self._halo_timer.interval = 1 / fps       # fewer frames on battery
        self._refresh_agents()
        for tid, agent, el, detail in j.manager.jobs():      # live timers inside the menu too
            item = self._task_items.get(tid)
            if item is not None:
                item.title = f"Stop #{tid} {agent}  {fmt_elapsed(el)}  {detail}"[:80]

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
                item = rumps.MenuItem(f"Stop {line}", callback=lambda s, i=t.id: j.manager.cancel(i))
                self._task_items[t.id] = item
                self.agents_menu.add(item)
            else:
                self.agents_menu.add(rumps.MenuItem(f"{line} ({t.status})"))
        if not tasks and not pending:
            self.agents_menu.add(rumps.MenuItem("No agent tasks yet"))

    def _hud_tick(self, _sender=None) -> None:
        self.hud.update(self.jarvis.jobs())

    def _halo_tick(self, _sender=None) -> None:
        self.halo.tick(self.jarvis.state, self.jarvis.voice_level())

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

    def toggle_hands_free(self, sender) -> None:
        j = self.jarvis
        if not j.auto_active and not j.stt.whisper_available():
            rumps.alert("Hands-free needs on-device speech recognition",
                        "So that nothing you say to other people is uploaded, hands-free uses Whisper on this Mac. "
                        "In Terminal, in the Jarvis folder, run:  ./venv/bin/pip install faster-whisper  then restart Jarvis. "
                        "(Python 3.12 or 3.13 works best.)")
            return
        j.auto_active = not j.auto_active
        sender.state = 1 if j.auto_active else 0

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

    def check_messages(self, _sender) -> None:
        def work():
            text = self.jarvis.social.summary()
            notify("Jarvis", text)
            self.jarvis._say(text)
        threading.Thread(target=work, daemon=True).start()

    def toggle_updates(self, sender) -> None:
        on = not self.jarvis.social.enabled
        self.jarvis.social.enabled = self.jarvis.notifier.announce_enabled = on
        sender.state = 1 if on else 0

    def import_export(self, _sender) -> None:
        def work():
            r = subprocess.run(["osascript", "-e", 'POSIX path of (choose file with prompt "Pick your ChatGPT export or Google Takeout (.zip)")'],
                               capture_output=True, text=True)
            path = r.stdout.strip()
            if not path:
                return
            from . import history
            c = history.import_paths([path])
            n = c["chatgpt"] + c["gemini"]
            msg = (f"Imported {n:,} messages. History now holds {history.stats()}." if n else
                   "I couldn't find ChatGPT or Gemini conversations in that file. Expected a ChatGPT export zip or a Google Takeout with Gemini Apps activity.")
            notify("Jarvis", msg)
            self.jarvis._say(msg)
        threading.Thread(target=work, daemon=True).start()

    def allow_messages(self, _sender) -> None:
        rumps.alert("Message reading",
                    "1. In your browser, pin WhatsApp Web, Instagram and any other chat sites as tabs and stay signed in.\n\n"
                    "2. Allow Jarvis to read them: Chrome/Brave/Edge: View > Developer > Allow JavaScript from Apple Events. "
                    "Safari: Settings > Advanced > Show features for web developers, then Develop > Developer Settings > "
                    "Allow JavaScript from Apple Events.\n\n"
                    "3. The first time, macOS asks whether Jarvis may control your browser: click OK.\n\n"
                    "Jarvis only reads the text of those tabs. It never types, clicks or sends anything.")

    def fix_mic(self, _sender) -> None:
        from .permissions import SETTINGS_URL, mic_status
        subprocess.run(["open", SETTINGS_URL])
        rumps.alert("Microphone access",
                    f"macOS says: {mic_status()}.\n\nIn the list that just opened, switch ON Jarvis (or Python / Terminal). "
                    "Then choose Quit Jarvis and open it again.\n\nIf Jarvis is not in the list, open Terminal in the "
                    "project folder and run:  ./jarvisctl.sh stop   then   ./install.sh app")

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
    lower_priority(cfg.performance.nice)
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
    from .permissions import mic_status, request_mic_access
    status = mic_status()
    log.info("Microphone permission: %s", status)
    if status == "not_determined":
        request_mic_access()               # show the allow/deny prompt right at launch
    app = JarvisMenu(cfg)
    threading.Thread(target=app.jarvis.run, daemon=True, name="jarvis-audio").start()
    app.run()


if __name__ == "__main__":
    main()
