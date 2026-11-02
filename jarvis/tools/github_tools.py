"""GitHub tools for the github agent: find your local repos, audit them, and talk to the GitHub API safely.
Needs either the GitHub CLI signed in once (brew install gh, then gh auth login) or GITHUB_TOKEN in .env."""
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from . import policy
from .base import tool

SKIP = {"node_modules", "venv", ".venv", "Library", "Applications", ".Trash", "site-packages", "__pycache__", "Pods",
        "DerivedData", "build", "dist", ".git", "Jarvis Workspace"}
SECRETS = {"AWS access key": r"AKIA[0-9A-Z]{16}", "GitHub token": r"gh[pousr]_[A-Za-z0-9]{36,}",
           "API key (sk-...)": r"sk-[A-Za-z0-9_\-]{20,}", "Google API key": r"AIza[0-9A-Za-z_\-]{35}",
           "private key": r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "Slack token": r"xox[baprs]-[A-Za-z0-9\-]{10,}"}
JUNK = (".DS_Store", "node_modules/", "__pycache__/", ".pyc", ".env", "venv/", ".venv/", ".idea/", "Thumbs.db")
BLOCKED_API = [r"^/user/(keys|gpg_keys|emails|ssh_signing_keys)", r"/actions/secrets", r"/collaborators", r"/invitations",
               r"/hooks", r"/keys\b", r"^/orgs/[^/]+/(members|memberships|invitations)"]


def _git(path, *args, timeout=25):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout)


def _token():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=8)   # internal use only, never shown
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


@tool(scope="agent")
def find_git_repos(root: str = "~", max_depth: int = 4) -> str:
    """Find git repositories on this Mac (your local clones), with their remote address and current branch."""
    found = _find_repos(policy.resolve(root), int(max_depth))
    rows = []
    for p in found:
        remote = _git(p, "config", "--get", "remote.origin.url").stdout.strip() or "(no remote)"
        branch = _git(p, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        rows.append(f"{p}  [{branch}]  {remote}")
    return "\n".join(rows) or "No git repositories found."


def _audit(p: Path) -> str:
    if _git(p, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return f"{p} is not a git repository."
    out, issues = [f"Repository: {p}"], []
    branch = _git(p, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    remote = _git(p, "config", "--get", "remote.origin.url").stdout.strip()
    dirty = [l for l in _git(p, "status", "--porcelain").stdout.splitlines() if l.strip()]
    ahead = _git(p, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
    last = _git(p, "log", "-1", "--format=%cr").stdout.strip()
    out.append(f"Branch {branch}, remote {remote or 'none'}, last commit {last or 'none'}, {len(dirty)} uncommitted change(s), {ahead or '0'} unpushed commit(s)")
    if not remote:
        issues.append("no remote: this repo is not on GitHub")
    if dirty:
        issues.append(f"{len(dirty)} uncommitted change(s) (leave them alone unless asked)")
    names = {f.name.lower(): f for f in p.iterdir() if f.is_file()}
    readme = next((f for n, f in names.items() if n.startswith("readme")), None)
    if not readme:
        issues.append("no README")
    else:
        text = readme.read_text(errors="replace")
        if len(text) < 400:
            issues.append(f"README is very short ({len(text)} characters)")
        missing = [h for h, pat in (("install/usage", r"install|usage|getting started|run"), ("a title", r"^#\s")) if not re.search(pat, text, re.I | re.M)]
        if missing:
            issues.append("README lacks " + " and ".join(missing))
    if not any(n.startswith(("license", "licence", "copying")) for n in names):
        issues.append("no LICENSE")
    if ".gitignore" not in names:
        issues.append("no .gitignore")
    tracked = _git(p, "ls-files", timeout=40).stdout.splitlines()
    junk = [f for f in tracked if any(j in f for j in JUNK)]
    if junk:
        issues.append(f"{len(junk)} junk/sensitive-looking file(s) are committed, e.g. {', '.join(junk[:4])}")
    big = [f for f in tracked if (p / f).is_file() and (p / f).stat().st_size > 5_000_000]
    if big:
        issues.append(f"{len(big)} file(s) over 5 MB committed, e.g. {', '.join(big[:3])}")
    hits = []
    for f in tracked[:2500]:
        fp = p / f
        try:
            if not fp.is_file() or fp.stat().st_size > 1_000_000:
                continue
            for n, line in enumerate(fp.read_text(errors="ignore").splitlines(), 1):
                for label, pat in SECRETS.items():
                    if re.search(pat, line):
                        hits.append(f"{f}:{n} looks like a {label}")
        except Exception:
            continue
    if hits:
        issues.insert(0, "POSSIBLE SECRETS COMMITTED (values hidden): " + "; ".join(hits[:6]) + ". Tell the user to rotate them; do not rewrite history.")
    stale = _git(p, "for-each-ref", "--sort=committerdate", "--format=%(refname:short) %(committerdate:relative)", "refs/heads").stdout.splitlines()
    old = [l for l in stale if re.search(r"(months|years) ago", l) and not l.startswith(branch + " ")]
    if old:
        issues.append(f"{len(old)} stale local branch(es): " + ", ".join(l.split()[0] for l in old[:5]))
    out.append("Issues:\n- " + "\n- ".join(issues) if issues else "No issues found.")
    return "\n".join(out)


@tool(scope="agent")
def repo_audit(path: str) -> str:
    """Audit a local git repository: README and LICENSE, .gitignore, junk or secrets committed, large files, uncommitted and unpushed work, stale state. Reports only; changes nothing. Secret values are never printed."""
    return _audit(policy.resolve(path))


def _find_repos(base: Path, max_depth: int = 4):
    found, deadline = [], time.time() + 40
    for dirpath, dirnames, _files in os.walk(base):
        depth = len(Path(dirpath).relative_to(base).parts)
        if ".git" in dirnames:
            found.append(dirpath)
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".") and depth < max_depth]
        if time.time() > deadline or len(found) >= 80:
            break
    return found


@tool(scope="agent")
def audit_all_repos(root: str = "~") -> str:
    """FAST: find every local git repository under a folder and audit them all in parallel, in one call. Returns a short report per repository. Use this instead of auditing one by one."""
    repos = _find_repos(policy.resolve(root))
    if not repos:
        return "No git repositories found."
    with ThreadPoolExecutor(max_workers=4) as ex:
        reports = list(ex.map(lambda r: _audit(Path(r)), repos))
    return f"{len(repos)} repositories audited.\n\n" + "\n\n".join(r[:900] for r in reports)


@tool(scope="agent")
def github_inventory(limit: int = 30) -> str:
    """FAST: one call that lists the user's GitHub repositories (most recently pushed first) with what needs fixing: missing description, topics, license or README, stale, plus whether the profile README repository exists. Replaces dozens of separate API calls."""
    token = _token()
    if not token:
        return "Error: no GitHub access yet. The user needs to run `gh auth login` once (brew install gh), or put GITHUB_TOKEN=... in Jarvis's .env."
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    me = requests.get("https://api.github.com/user", headers=hdr, timeout=20).json()
    login = me.get("login", "?")
    repos = []
    for page in (1, 2):
        batch = requests.get(f"https://api.github.com/user/repos?per_page=100&page={page}&sort=pushed&affiliation=owner", headers=hdr, timeout=25).json()
        repos += batch if isinstance(batch, list) else []
        if not isinstance(batch, list) or len(batch) < 100:
            break
    mine = [r for r in repos if not r.get("fork") and not r.get("archived")]
    top = mine[: int(limit)]

    def has_readme(r):
        return requests.get(f"https://api.github.com/repos/{r['full_name']}/readme", headers=hdr, timeout=15).status_code == 200

    with ThreadPoolExecutor(max_workers=8) as ex:
        readmes = list(ex.map(has_readme, top))
        profile = ex.submit(lambda: requests.get(f"https://api.github.com/repos/{login}/{login}", headers=hdr, timeout=15).status_code).result()
    lines = []
    for r, ok in zip(top, readmes):
        flags = [f for f, bad in (("no description", not r.get("description")), ("no topics", not r.get("topics")),
                                 ("no license", not r.get("license")), ("no README", not ok),
                                 ("stale", (r.get("pushed_at") or "")[:4] < str(time.gmtime().tm_year - 1))) if bad]
        lines.append(f"{r['full_name']}  [{r.get('language') or 'no language'}, {r.get('stargazers_count', 0)} stars, {'private' if r.get('private') else 'public'}, pushed {(r.get('pushed_at') or '')[:10]}]"
                     + (f"  NEEDS: {', '.join(flags)}" if flags else "  ok") + f"  {r.get('description') or ''}"[:80])
    head = (f"GitHub user {login}: {len(repos)} repositories ({len(mine)} not forks or archived, showing {len(top)}). "
            f"Profile README repository ({login}/{login}): {'exists' if profile == 200 else 'missing'}.")
    return head + "\n" + "\n".join(lines)


@tool(scope="agent")
def github_api(method: str, path: str, body_json: str = "") -> str:
    """Call the GitHub REST API as the user (path like /user/repos?per_page=100 or /repos/OWNER/REPO). GET is free; anything that changes GitHub asks the user first. Deleting repositories, changing visibility, keys, secrets, webhooks and collaborators are never allowed."""
    method = method.upper()
    if not path.startswith("/"):
        path = "/" + path
    token = _token()
    if not token:
        return "Error: no GitHub access yet. The user needs to run `gh auth login` once (brew install gh), or put GITHUB_TOKEN=... in Jarvis's .env."
    body = json.loads(body_json) if body_json.strip() else None
    if method == "DELETE" and re.fullmatch(r"/repos/[^/]+/[^/]+/?", path.split("?")[0]):
        raise PermissionError("deleting repositories is never allowed")
    if isinstance(body, dict) and "private" in body:
        raise PermissionError("changing repository visibility is never allowed")
    if any(re.search(pat, path) for pat in BLOCKED_API):
        raise PermissionError("that part of GitHub (keys, secrets, webhooks, collaborators, members) is off limits")
    if method != "GET":
        policy._need(f"GitHub {method} {path[:90]}", "Change on GitHub", key=f"github {method}")
    r = requests.request(method, "https://api.github.com" + path, json=body, timeout=25, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    text = r.text if r.status_code < 400 else f"HTTP {r.status_code}: {r.text[:300]}"
    return text[:8000]
