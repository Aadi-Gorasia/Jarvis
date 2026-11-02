"""Mac cleanup tools. Everything is reversible: files only ever move to the Trash (never deleted), every batch
is written to an undo manifest, nothing outside your home folder is touched, and one approval covers a whole batch."""
import hashlib
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import policy
from .base import tool

HOME = Path.home()
SAFE_LIB = [HOME / "Library/Caches", HOME / "Library/Logs", HOME / "Library/Developer/Xcode/DerivedData",
            HOME / "Library/Developer/CoreSimulator/Caches"]
SAFE_DOT = [HOME / ".cache", HOME / ".npm/_cacache", HOME / ".gradle/caches", HOME / ".cargo/registry/cache"]
FOLDERS = {HOME / n for n in ("Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music", "Library", "Applications", "Public")} | {HOME}
SKIP_WALK = {"node_modules", ".git", "Library", ".Trash", "Applications", "Jarvis Workspace", ".venv", "venv", "site-packages"}


def _size(path: Path, budget: float = 15.0) -> int:
    if path.is_file():
        return path.stat().st_size
    total, end = 0, time.time() + budget
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
        if time.time() > end:
            break
    return total


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _age_days(p: Path) -> int:
    return int((time.time() - p.stat().st_mtime) / 86400)


def _refuse_reason(p: Path):
    """Why this path must not be cleaned or moved (None = fine)."""
    try:
        p = policy.resolve(str(p))
    except PermissionError as exc:
        return str(exc)
    if HOME not in p.parents:
        return "outside your home folder"
    if p in FOLDERS:
        return "that is a main folder; only its contents may be cleaned"
    if HOME / "Library" in p.parents and not any(s == p or s in p.parents for s in SAFE_LIB):
        return "inside Library: only caches and logs may be cleaned"
    rel = p.relative_to(HOME).parts
    if rel and rel[0].startswith(".") and not any(s == p or s in p.parents for s in SAFE_DOT):
        return "a hidden settings folder"
    if ".Trash" in rel:
        return "already in the Trash"
    return None


def _walk(roots, deadline_s: float = 60.0, cap: int = 400_000):
    end, n = time.time() + deadline_s, 0
    for root in roots:
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_WALK and not d.endswith((".app", ".photoslibrary", ".xcodeproj"))]
            for f in files:
                fp = Path(dirpath) / f
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                yield fp, st.st_size, st.st_mtime
                n += 1
            if time.time() > end or n > cap:
                return
            if n % 3000 == 0:
                time.sleep(0.01)                              # stay polite to the rest of the Mac


def _lines(rows, limit=40):
    rows = sorted(rows, key=lambda r: -r[1])[:limit]
    return "\n".join(f"{_fmt(sz):>9}  {int((time.time() - mt) / 86400):>4}d  {p}" for p, sz, mt in rows)


@tool(scope="agent")
def disk_usage() -> str:
    """Free and used space on the Mac's main disk."""
    t, u, f = shutil.disk_usage("/")
    return f"Disk: {_fmt(t)} total, {_fmt(u)} used, {_fmt(f)} free."


_LAST_SAFE: list = []          # what the last mac_scan("all") judged safe; trash_many("@safe") uses it
ARCHIVES = (".zip", ".tar", ".gz", ".tgz", ".rar", ".7z", ".dmg", ".pkg", ".iso")


def _scan_all() -> str:
    """Every kind of scan at once, in parallel, with a ready-made list of what is clearly safe to trash."""
    global _LAST_SAFE
    dl, desk, week = HOME / "Downloads", HOME / "Desktop", time.time() - 7 * 86400

    def caches():
        rows = []
        for base in SAFE_LIB[:3] + SAFE_DOT:
            if base.is_dir():
                rows += [(c, _size(c, 6), c.stat().st_mtime) for c in base.iterdir()]
        return [r for r in rows if r[1] > 20_000_000]

    def installers():
        cut = time.time() - 14 * 86400
        return [r for r in _walk([dl, desk]) if r[0].suffix.lower() in (".dmg", ".pkg", ".iso") and r[2] < cut]

    def screenshots():
        cut = time.time() - 30 * 86400
        return [r for r in _walk([desk]) if r[0].name.startswith(("Screenshot", "Screen Shot")) and r[2] < cut]

    def old_archives():
        cut = time.time() - 60 * 86400
        return [r for r in _walk([dl]) if r[0].suffix.lower() in ARCHIVES and r[2] < cut]

    def dev():
        rows, end = [], time.time() + 45
        for dirpath, dirnames, _f in os.walk(HOME):
            hit = [d for d in dirnames if d in ("node_modules", ".venv", "venv", "__pycache__", ".next")]
            for d in hit:
                p = Path(dirpath) / d
                if _age_days(p) >= 60 and "Library" not in p.parts:
                    rows.append((p, _size(p, 4), p.stat().st_mtime))
            dirnames[:] = [d for d in dirnames if d not in hit and d not in (".git", "Library", ".Trash", "Applications") and not d.startswith(".")]
            if time.time() > end:
                break
        return rows

    def dup_extras():
        by_size = {}
        for p, sz, mt in _walk([HOME / x for x in ("Downloads", "Desktop", "Documents", "Pictures", "Movies", "Music") if (HOME / x).exists()], 45):
            if sz > 1_000_000:
                by_size.setdefault(sz, []).append((p, sz, mt))
        extras = []
        for sz, items in by_size.items():
            if len(items) < 2:
                continue
            by_hash = {}
            for p, _s, mt in items:
                try:
                    h = hashlib.sha1()
                    with open(p, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    by_hash.setdefault(h.hexdigest(), []).append((p, sz, mt))
                except OSError:
                    pass
            for group in by_hash.values():
                if len(group) > 1:
                    keep = next((g for g in group if HOME / "Documents" in g[0].parents), min(group, key=lambda g: g[2]))   # keep Documents' copy, else the oldest
                    extras += [g for g in group if g is not keep and HOME / "Documents" not in g[0].parents]
        return extras

    def big():
        return [r for r in _walk([HOME]) if r[1] > 500_000_000]

    jobs = {"caches": caches, "installers": installers, "screenshots": screenshots, "old archives in Downloads": old_archives,
            "stale dev folders": dev, "duplicate copies": dup_extras, "huge files": big}
    with ThreadPoolExecutor(max_workers=4) as ex:
        done = {name: ex.submit(fn) for name, fn in jobs.items()}
        results = {}
        for name, fut in done.items():
            try:
                results[name] = fut.result(timeout=90)
            except Exception:
                results[name] = []
    safe, seen, report = [], set(), [disk_usage()]
    for name, rows in results.items():
        if name == "huge files":
            continue
        keep = [r for r in rows if (name == "caches" or r[2] < week) and not _refuse_reason(r[0]) and str(r[0]) not in seen]
        seen.update(str(r[0]) for r in keep)
        safe += keep
        report.append(f"{name}: {len(keep)} items, {_fmt(sum(r[1] for r in keep))}")
    _LAST_SAFE = [str(r[0]) for r in safe]
    big_rows = sorted(results["huge files"], key=lambda r: -r[1])[:10]
    report.append(f"\nCLEARLY SAFE TO TRASH: {len(safe)} items, {_fmt(sum(r[1] for r in safe))}. Call trash_many with paths_json=\"@safe\" to move exactly these.")
    report.append("\nNEEDS THE USER'S DECISION (not included in @safe):\n" + (_lines(big_rows, 10) or "(nothing huge found)"))
    return "\n".join(report)


@tool(scope="agent")
def mac_scan(kind: str = "overview") -> str:
    """Scan the user's home folder for things that can be cleaned, without changing anything. kind: all (FAST: every scan at once in parallel, plus a ready list of what is safe to trash; use this first), overview (sizes of the usual suspects), large (biggest files), old_downloads (Downloads older than 60 days), installers (.dmg/.pkg/.iso), caches, dev (node_modules, virtualenvs, build folders), screenshots (old Desktop screenshots), duplicates (identical files). Returns size, age in days and the full path."""
    kind = kind.lower().strip()
    dl, desk = HOME / "Downloads", HOME / "Desktop"
    if kind == "all":
        return _scan_all()
    if kind == "overview":
        spots = [("Downloads", dl), ("Desktop", desk), ("Documents", HOME / "Documents"), ("Movies", HOME / "Movies"),
                 ("Library/Caches", HOME / "Library/Caches"), ("Xcode DerivedData", SAFE_LIB[2]), ("Trash", HOME / ".Trash"),
                 ("npm cache", HOME / ".npm"), ("other caches", HOME / ".cache")]
        rows = [f"{name:<20} {_fmt(_size(p, 8)):>10}" for name, p in spots if p.exists()]
        return disk_usage() + "\n" + "\n".join(rows) + "\nNext: scan caches, old_downloads, installers, large, dev, screenshots, duplicates."
    if kind == "large":
        return _lines([r for r in _walk([HOME]) if r[1] > 200_000_000], 30) or "No files over 200 MB."
    if kind == "old_downloads":
        cut = time.time() - 60 * 86400
        return _lines([r for r in _walk([dl]) if r[2] < cut], 60) or "Nothing in Downloads is older than 60 days."
    if kind == "installers":
        cut = time.time() - 14 * 86400
        return _lines([r for r in _walk([dl, desk]) if r[0].suffix.lower() in (".dmg", ".pkg", ".iso") and r[2] < cut], 40) or "No old installers."
    if kind == "screenshots":
        cut = time.time() - 30 * 86400
        return _lines([r for r in _walk([desk]) if r[0].name.startswith(("Screenshot", "Screen Shot")) and r[2] < cut], 60) or "No old screenshots."
    if kind == "caches":
        rows = []
        for base in SAFE_LIB[:3] + SAFE_DOT:
            if base.is_dir():
                rows += [(c, _size(c, 6), c.stat().st_mtime) for c in base.iterdir()]
        return _lines(rows, 30) or "No caches found."
    if kind == "dev":
        rows, end = [], time.time() + 60
        for dirpath, dirnames, _f in os.walk(HOME):
            hit = [d for d in dirnames if d in ("node_modules", ".venv", "venv", "__pycache__", ".next", "target", "build", "dist")]
            for d in hit:
                p = Path(dirpath) / d
                if _age_days(p) >= 60 and "Library" not in p.parts:
                    rows.append((p, _size(p, 5), p.stat().st_mtime))
            dirnames[:] = [d for d in dirnames if d not in hit and d not in (".git", "Library", ".Trash", "Applications") and not d.startswith(".")]
            if time.time() > end:
                break
        return _lines(rows, 40) or "No stale dev folders."
    if kind == "duplicates":
        by_size = {}
        for p, sz, mt in _walk([HOME / x for x in ("Documents", "Downloads", "Desktop", "Pictures", "Movies", "Music") if (HOME / x).exists()]):
            if sz > 1_000_000:
                by_size.setdefault(sz, []).append((p, mt))
        groups, hashed = [], 0
        for sz, items in by_size.items():
            if len(items) < 2 or hashed > 3_000_000_000:
                continue
            by_hash = {}
            for p, mt in items:
                try:
                    h = hashlib.sha1()
                    with open(p, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1 << 20), b""):
                            h.update(chunk)
                    hashed += sz
                    by_hash.setdefault(h.hexdigest(), []).append((p, mt))
                except OSError:
                    pass
            groups += [g for g in by_hash.values() if len(g) > 1]
        if not groups:
            return "No duplicate files over 1 MB."
        groups.sort(key=lambda g: -g[0][0].stat().st_size * (len(g) - 1))
        return "\n\n".join(f"{_fmt(g[0][0].stat().st_size)} each, {len(g)} copies (keep one):\n" + "\n".join(f"  {int((time.time() - mt) / 86400)}d  {p}" for p, mt in sorted(g, key=lambda x: x[1])) for g in groups[:25])
    return "Unknown kind. Use overview, large, old_downloads, installers, caches, dev, screenshots or duplicates."


def _manifest(entries, kind: str, reason: str) -> Path:
    d = policy.workspace() / "cleanup"
    d.mkdir(parents=True, exist_ok=True)
    m = d / f"undo-{time.strftime('%Y%m%d-%H%M%S')}.json"
    m.write_text(json.dumps({"kind": kind, "reason": reason, "entries": entries}, indent=1))
    return m


@tool(scope="agent")
def trash_many(paths_json: str, reason: str) -> str:
    """Move a batch of files or folders to the Trash (never deletes, fully undoable). paths_json is a JSON list of absolute paths, or the text @safe to use the safe list from the last mac_scan("all"). The user is asked ONCE for the whole batch. Only things inside the user's home folder are allowed, never main folders, settings, or Library (except caches and logs)."""
    paths = [Path(p).expanduser() for p in (_LAST_SAFE if paths_json.strip() == "@safe" else json.loads(paths_json))]
    ok, skipped = [], []
    for p in paths:
        why = _refuse_reason(p) if p.exists() else "does not exist"
        (skipped.append(f"{p} ({why})") if why else ok.append(p.resolve()))
    if not ok:
        return "Nothing moved. Skipped: " + "; ".join(skipped[:8])
    total = sum(_size(p, 4) for p in ok)
    if policy.mode() != "full":
        policy._need(f"move {len(ok)} items ({_fmt(total)}) to the Trash: {reason}", "Cleanup")
    trash, entries = HOME / ".Trash", []
    trash.mkdir(exist_ok=True)
    for p in ok:
        dest = trash / p.name
        if dest.exists():
            dest = trash / f"{p.stem}-{int(time.time())}{p.suffix}"
        try:
            shutil.move(str(p), str(dest))
            entries.append({"orig": str(p), "now": str(dest), "size": 0})
        except Exception as exc:
            skipped.append(f"{p} ({exc})")
    m = _manifest(entries, "trash", reason)
    return (f"Moved {len(entries)} items ({_fmt(total)}) to the Trash. Undo with undo_cleanup (manifest {m})."
            + (f" Skipped {len(skipped)}: " + "; ".join(skipped[:5]) if skipped else ""))


@tool(scope="agent")
def move_many(moves_json: str, reason: str) -> str:
    """Reorganise files: moves_json is a JSON list of {"from": path, "to": path}. Nothing is overwritten, the user is asked once for the whole batch, and it can be undone with undo_cleanup."""
    moves = json.loads(moves_json)
    plan, skipped = [], []
    for mv in moves:
        a, b = Path(mv["from"]).expanduser(), Path(mv["to"]).expanduser()
        why = _refuse_reason(a) if a.exists() else "does not exist"
        why = why or ("destination exists" if b.exists() else None) or _refuse_reason(b.parent / "x")
        (skipped.append(f"{a} ({why})") if why else plan.append((a.resolve(), b)))
    if not plan:
        return "Nothing moved. Skipped: " + "; ".join(skipped[:8])
    if policy.mode() != "full":
        policy._need(f"move {len(plan)} files into new places: {reason}", "Reorganise files")
    entries = []
    for a, b in plan:
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(b))
        entries.append({"orig": str(a), "now": str(b), "size": 0})
    m = _manifest(entries, "move", reason)
    return f"Moved {len(entries)} files. Undo with undo_cleanup (manifest {m})." + (f" Skipped {len(skipped)}." if skipped else "")


@tool(scope="agent")
def undo_cleanup(manifest_path: str = "") -> str:
    """Put back everything from a cleanup or reorganisation. Leave manifest_path empty to undo the most recent one."""
    d = policy.workspace() / "cleanup"
    m = Path(manifest_path).expanduser() if manifest_path else next(iter(sorted(d.glob("undo-*.json"), reverse=True)), None)
    if m is None or not m.exists():
        return "No cleanup to undo."
    data = json.loads(m.read_text())
    restored = 0
    for e in data["entries"]:
        src, dst = Path(e["now"]), Path(e["orig"])
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            restored += 1
    m.rename(m.with_suffix(".undone"))
    return f"Restored {restored} of {len(data['entries'])} items to where they were."
