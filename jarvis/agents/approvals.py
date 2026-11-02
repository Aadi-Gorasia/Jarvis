"""Lets a background agent pause for your OK. Jarvis announces the request; you answer by voice."""
import itertools
import threading

from ..util import notify


class ApprovalBroker:
    def __init__(self, cfg, announce):
        self.timeout = cfg.agents.approval_timeout_seconds
        self.announce = announce
        self.pending: dict = {}
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def request(self, who: str, action: str, reason: str) -> bool:
        with self.lock:
            aid = next(self.ids)
            entry = {"id": aid, "who": who, "action": action, "reason": reason,
                     "event": threading.Event(), "approved": False}
            self.pending[aid] = entry
        notify("Jarvis needs approval", f"{who}: {action}")
        self.announce(f"{who} needs your approval. {reason}: {action}. Say approve or deny.")
        entry["event"].wait(self.timeout)
        with self.lock:
            self.pending.pop(aid, None)
        return bool(entry["approved"])

    def resolve(self, approval_id: int, approve: bool) -> str:
        with self.lock:
            if not self.pending:
                return "Nothing is waiting for approval."
            if not approval_id:
                approval_id = max(self.pending)
            entry = self.pending.get(int(approval_id))
            if entry is None:
                return f"No pending approval numbered {approval_id}."
            entry["approved"] = bool(approve)
            entry["event"].set()
        return f"{'Approved' if approve else 'Denied'}: {entry['action']}"

    def describe(self) -> str:
        with self.lock:
            return "\n".join(f"#{e['id']} {e['who']}: {e['action']}" for e in self.pending.values()) or "Nothing pending."
