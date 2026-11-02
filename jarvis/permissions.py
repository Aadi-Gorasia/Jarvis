"""macOS microphone permission helpers (via AVFoundation when available)."""
import logging

log = logging.getLogger("jarvis.permissions")
FDA_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"


def mic_status() -> str:
    """authorized | denied | restricted | not_determined | unknown"""
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        st = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
        return {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}.get(st, "unknown")
    except Exception:
        return "unknown"


def request_mic_access() -> None:
    """Ask macOS to show the 'allow microphone?' prompt for this process (only works while status is not_determined)."""
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, lambda granted: log.info("Microphone permission %s", "granted" if granted else "refused"))
    except Exception as exc:
        log.warning("could not request microphone access (%s)", str(exc)[:100])
