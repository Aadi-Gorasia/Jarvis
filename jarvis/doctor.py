"""./jarvisctl.sh doctor : checks every part and tells you exactly what to fix."""
import pathlib
import subprocess
import sys
import time

import requests

from .util import DATA_DIR

OK, BAD, WARN = "  ✓", "  ✗", "  !"


def run_doctor(cfg) -> None:
    problems = 0

    def good(name, detail=""):
        print(f"{OK} {name}" + (f"   {detail}" if detail else ""))

    def warn(name, hint):
        print(f"{WARN} {name}\n       {hint}")

    def bad(name, hint):
        nonlocal problems
        problems += 1
        print(f"{BAD} {name}\n       → {hint}")

    print("\nJarvis doctor\n")
    py = sys.version_info
    if py >= (3, 14):
        warn(f"Python {py.major}.{py.minor}", "very new; if you see odd crashes, rebuild with 3.12: brew install python@3.12, delete venv, run ./install.sh")
    else:
        good(f"Python {py.major}.{py.minor}")

    # keys and Gemini speed
    if not cfg.api_key:
        bad("Gemini key", "put GEMINI_API_KEY=... in .env")
    else:
        try:
            from .assistant import make_client
            client = make_client(cfg, fast=True)
            t = time.time()
            client.models.generate_content(model=cfg.gemini.model, contents="Reply with the single word OK.")
            dt = time.time() - t
            if dt < 4:
                good(f"Gemini ({cfg.gemini.model}) answered in {dt:.1f}s")
            else:
                warn(f"Gemini ({cfg.gemini.model}) answered in {dt:.1f}s", "slow: Google may be busy; Jarvis fails over to the next model automatically")
        except Exception as exc:
            bad(f"Gemini ({cfg.gemini.model})", f"{str(exc)[:140]}  (quota, busy, or wrong model name in config.yaml)")
    if cfg.openai_key:
        try:
            r = requests.get(cfg.llm.openai_base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {cfg.openai_key}"}, timeout=10)
            good("OpenAI key works") if r.status_code == 200 else bad("OpenAI key", f"HTTP {r.status_code}: check the key and billing")
        except Exception as exc:
            bad("OpenAI", str(exc)[:100])
    else:
        warn("No OpenAI key", "optional backup model; add OPENAI_API_KEY to .env if you want it")

    # on-device model
    try:
        tags = requests.get(cfg.llm.ollama_url.rstrip("/") + "/api/tags", timeout=3).json().get("models", [])
        names = [m.get("name", "") for m in tags]
        if any(n.startswith(cfg.llm.ollama_model.split(":")[0]) for n in names):
            good(f"On-device model ready ({cfg.llm.ollama_model})")
        else:
            bad(f"On-device model {cfg.llm.ollama_model} not downloaded", f"ollama pull {cfg.llm.ollama_model}")
    except Exception:
        warn("Ollama is not running", "open the Ollama app (only needed for on-device fallback and local agents)")

    # history
    try:
        from . import history
        st = history.stats()
        if st == "nothing imported yet":
            warn("No ChatGPT or Gemini history imported yet", "export it (ChatGPT: Settings > Data controls > Export; Google: takeout.google.com > Gemini Apps), then use the menu bar's Import item or drop the zip in Downloads")
        else:
            good("History searchable: " + st)
    except Exception as exc:
        warn("History check failed", str(exc)[:100])

    # speed: on-device speech recognition
    try:
        import faster_whisper  # noqa: F401
        good(f"On-device speech recognition installed (Whisper {cfg.stt.whisper_model}): saves about a second per request")
    except Exception:
        warn("On-device speech recognition not installed", "optional, makes every answer about 1 second faster: ./venv/bin/pip install faster-whisper (Python 3.12 or 3.13 recommended)")

    # hands-free
    try:
        import faster_whisper  # noqa: F401
        mode = str(cfg.always_listen.enabled).lower()
        good(f"Hands-free listening available (always_listen.enabled: {mode}); sensitivity {cfg.always_listen.sensitivity}")
    except Exception:
        warn("Hands-free listening is off", "it needs on-device Whisper so nothing you say to other people is uploaded: ./venv/bin/pip install faster-whisper")
    from .util import perf_text
    good("Lightweight mode: low priority, 2 threads, halo " + str(cfg.performance.halo_fps) + " fps", "Jarvis now uses " + perf_text())

    # voice
    if cfg.elevenlabs_key and cfg.tts.engine == "elevenlabs":
        try:
            t = time.time()
            r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.tts.elevenlabs_voice_id}",
                              params={"output_format": "pcm_24000"},
                              headers={"xi-api-key": cfg.elevenlabs_key, "Content-Type": "application/json"},
                              json={"text": "Online.", "model_id": cfg.tts.elevenlabs_model}, timeout=15)
            good(f"ElevenLabs voice works ({time.time() - t:.1f}s)") if r.status_code == 200 else bad("ElevenLabs", f"HTTP {r.status_code}: {r.text[:100]}")
        except Exception as exc:
            bad("ElevenLabs", str(exc)[:100])
    else:
        warn("Using the Mac voice", "add ELEVENLABS_API_KEY to .env for the ElevenLabs voice")

    # wake word files
    needed = [DATA_DIR / "models" / f"{cfg.wake.model}_v0.1.onnx"]
    try:
        import openwakeword
        base = pathlib.Path(openwakeword.__file__).parent / "resources" / "models"
        needed += [base / "melspectrogram.onnx", base / "embedding_model.onnx"]
    except Exception:
        bad("openwakeword", "not installed: rerun ./install.sh")
    missing = [p.name for p in needed if not p.exists()]
    good("Wake-word model files present") if not missing else warn(f"Wake-word files missing: {', '.join(missing)}", "they download automatically on the next start (needs internet)")

    # microphone
    from .permissions import mic_status
    st = mic_status()
    if st == "authorized":
        good("macOS microphone permission: authorized")
    elif st in ("denied", "restricted"):
        bad(f"macOS microphone permission: {st}", "System Settings > Privacy & Security > Microphone, switch on Jarvis/Python/Terminal (or run: tccutil reset Microphone, then start Jarvis again to get the prompt)")
    elif st == "not_determined":
        warn("macOS has not asked for microphone permission yet", "start Jarvis and click Allow when the prompt appears")
    try:
        import numpy as np
        from .audio import Microphone
        mic = Microphone(cfg.audio.device)
        mic.open()
        peak = 0.0
        for _ in range(20):
            f = mic.read()
            peak = max(peak, float(np.sqrt(np.mean(f.astype(np.float32) ** 2))))
        ambient = mic.ambient
        mic.close()
        if peak == 0:
            bad("Microphone gives pure silence", "System Settings > Privacy & Security > Microphone: allow Terminal (and Jarvis.app if you use ./install.sh app)")
        else:
            good(f"Microphone works (room noise about {ambient:.0f})", "" if ambient < cfg.audio.min_rms else "")
            if ambient * cfg.audio.noise_multiplier > 1500:
                warn("Room is loud", "Jarvis may struggle to hear you; move closer to the mic")
    except Exception as exc:
        bad("Microphone", f"{str(exc)[:120]}")

    # macOS permissions and display
    try:
        from ApplicationServices import AXIsProcessTrusted
        good("Accessibility permission granted") if AXIsProcessTrusted() else warn("Accessibility permission missing", "needed only for agents that click and type: System Settings > Privacy & Security > Accessibility")
    except Exception:
        warn("Could not check Accessibility permission", "fine unless agents need to click and type")
    try:
        from AppKit import NSScreen
        good(f"Screen halo available ({len(NSScreen.screens())} display(s))")
    except Exception:
        warn("Screen halo unavailable", "pyobjc not installed: rerun ./install.sh")
    try:
        from .webinbox import WebInbox
        from .notifications import NotificationWatcher as _NW
        wi = WebInbox(cfg, None, _NW(cfg, print))
        wi.poll_once()
        if wi.status == "ok":
            good("Chat tabs readable: " + ", ".join(sorted(wi.seen)) )
        else:
            warn(f"Chat tabs: {wi.status}", wi.help_text())
    except Exception as exc:
        warn("Chat-tab check failed", str(exc)[:100])
    try:
        import os as _os
        from .notifications import NotificationWatcher
        w = NotificationWatcher(cfg, print)
        w.poll_once()
        if w.status == "disabled":
            pass
        elif w.status == "ok":
            good("Message access works (reading your notifications)")
        elif w.status == "needs_full_disk_access":
            warn("Message access needs Full Disk Access", f"System Settings > Privacy & Security > Full Disk Access > add: {_os.path.realpath(sys.executable)}")
        else:
            warn(f"Message access: {w.status}", "needs macOS 14 or later with notifications enabled for your messaging apps")
    except Exception as exc:
        warn("Message access check failed", str(exc)[:100])
    try:
        from .social import SocialWatcher
        snap = SocialWatcher(cfg, print).snapshot()
        if snap:
            good("Message updates can see: " + ", ".join(f"{k} ({v})" for k, v in snap.items()))
        else:
            warn("Message updates see no messaging apps or sites right now", "they only work for apps that are running and browser tabs that are open; edit social: in config.yaml")
    except Exception as exc:
        warn("Message updates check failed", str(exc)[:100])
    running = subprocess.run(["pgrep", "-fl", "jarvis[.](main|menubar)"], capture_output=True, text=True).stdout.strip()
    good("Jarvis is running", running.splitlines()[0][:60]) if running else warn("Jarvis is not running", "./jarvisctl.sh start   (or ./jarvisctl.sh menubar to watch it)")

    print(f"\n{'All good.' if not problems else str(problems) + ' thing(s) to fix above.'}\n")
