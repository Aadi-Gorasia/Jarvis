import argparse
import logging
import sys

from .config import load_config
from .util import setup_logging


def main() -> None:
    ap = argparse.ArgumentParser(description="Jarvis, an always-on Gemini voice assistant")
    ap.add_argument("--text", action="store_true", help="type instead of speaking (no mic needed)")
    ap.add_argument("--test-mic", action="store_true", help="show live mic levels to tune audio.min_rms")
    ap.add_argument("--test-local", action="store_true", help="check the on-device model (Ollama) and tool calling")
    ap.add_argument("--test-voice", action="store_true", help="speak a test line with the configured voice")
    ap.add_argument("--test-wake", action="store_true", help="live wake-word scores while you say Hey Jarvis")
    ap.add_argument("--doctor", action="store_true", help="check everything and say what to fix")
    ap.add_argument("--import-history", nargs="+", metavar="PATH", help="import a ChatGPT or Gemini data export")
    ap.add_argument("--distill-history", action="store_true", help="turn imported history into long-term facts (asks first)")
    ap.add_argument("--config", help="path to an alternative config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    from .util import lower_priority
    lower_priority(cfg.performance.nice)

    if args.test_mic:
        from .audio import mic_test
        mic_test(device=cfg.audio.device)
        return
    if args.import_history:
        from . import history
        counts = history.import_paths(args.import_history)
        print(f"Imported {counts['chatgpt']:,} ChatGPT and {counts['gemini']:,} Gemini messages "
              f"({counts['skipped_duplicates']:,} already imported). History now holds: {history.stats()}.")
        if not (counts["chatgpt"] or counts["gemini"] or counts["skipped_duplicates"]):
            print("Found nothing to import. Expected conversations.json (ChatGPT export) or MyActivity.json under 'Gemini Apps' (Google Takeout).")
        return
    if args.distill_history:
        from google import genai
        from . import history
        from .memory import Memory
        from .util import DATA_DIR
        n = history.distill(cfg, genai.Client(api_key=cfg.api_key), Memory(DATA_DIR / "jarvis.db"))
        print(f"Saved {n} facts to Jarvis's memory.")
        return
    if args.doctor:
        from .doctor import run_doctor
        run_doctor(cfg)
        return
    if args.test_wake:
        from .audio import wake_test
        wake_test(cfg)
        return
    if args.test_local:
        from .local_llm import self_test
        self_test(cfg)
        return
    if args.test_voice:
        from google import genai
        from .speech import Speaker
        client = genai.Client(api_key=cfg.api_key) if cfg.api_key else None
        Speaker(cfg, client).say("Good evening. All systems are online and at your service.")
        return
    if not cfg.api_key:
        sys.exit("No Gemini API key found. Put GEMINI_API_KEY=... in the .env file next to config.yaml.")

    if not args.text:
        from .util import acquire_single_instance
        if not acquire_single_instance():
            sys.exit("Jarvis is already running (the menu bar app?). Quit it first: ./jarvisctl.sh stop")
    from .assistant import Jarvis
    jarvis = Jarvis(cfg, text_mode=args.text)
    try:
        jarvis.run_text() if args.text else jarvis.run()
    except KeyboardInterrupt:
        logging.getLogger("jarvis").info("Stopped.")


if __name__ == "__main__":
    main()
