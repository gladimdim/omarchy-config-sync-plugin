#!/usr/bin/env python3
"""Stamp this machine's Omarchy desktop onto another Omarchy install over SSH.

The source machine drives everything. The new machine needs only SSH, Python 3
and git: this machine's config repo travels as a git bundle, the plugin's own
scripts travel with it, and both are hash-checked before anything runs there.

Source-side commands (the panel runs these; one JSON document on stdout):
  probe      reachability, SSH login, and whether the target can take a clone
  preview    ship the bundle, dry-run the mirror there, scan for secrets and
             machine-specific settings, list missing programs and services
  clone      back up, install the plugin, link the bundled repo, mirror, restart
  health     after a clone: shell up, no QML errors, target in sync
  undo       put back every file the clone replaced and remove the ones it made
  adopt      give the target its own private repo (or share this one) through a
             repo-scoped deploy key; no GitHub login on the target
  machines   the family tree of machines cloned from here
  terminal   open a terminal for the steps a person must do (SSH key, host key,
             sudo package installs, services, plugin installs)

Target-side commands (run on the new machine from the shipped copy):
  target-plan, target-install-plugin, target-link, target-apply, target-undo,
  target-deploy-key, target-adopt
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_sync as cs  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
REMOTE_BASE = ".cache/omarchy-config-sync-clone"
SEED_REL = ".local/share/omarchy-config-sync/clone-seed.git"
UNDO_REL = ".local/share/omarchy-config-sync/clone-undo"
PLUGIN_REL = f".config/omarchy/plugins/{cs.PLUGIN_ID}"
STATE_REL = ".local/share/omarchy-config-sync"
GITHUB_ALIAS = "github-config-sync"
MIN_PYTHON = (3, 10)
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_BUNDLE_BYTES = 1024 * 1024 * 1024
MAX_SCAN_BYTES = 20 * 1024 * 1024
MAX_WALLPAPER_BYTES = 500 * 1024 * 1024
BACKGROUND_LINK_REL = ".local/state/omarchy/current/background"
RUNTIME_SKIP = {".git", "tests", "__pycache__", ".sync-session", "node_modules"}
STAGES = ["plugin", "link", "apply", "restart"]
INHIBIT = ["systemd-inhibit", "--what=sleep:idle", "--who=Config Sync",
           "--why=Cloning a desktop onto this machine", "--mode=block"]

DEST_RE = re.compile(r"^(?:[a-z_][a-z0-9_.-]{0,31}@)?[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
RUNID_RE = re.compile(r"^[0-9a-f]{12}$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

SSH_BASE = [
    "ssh", "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=8",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "LogLevel=ERROR",
]

# A plain SSH shell has no Wayland/Hyprland session; borrow the user session's.
SESSION_PREFIX = (
    "eval \"$(systemctl --user show-environment 2>/dev/null "
    "| grep -E '^(OMARCHY_PATH|WAYLAND_DISPLAY|HYPRLAND_INSTANCE_SIGNATURE|XDG_RUNTIME_DIR|PATH)=' "
    "| sed 's/^/export /')\"; "
    "export XDG_RUNTIME_DIR=\"${XDG_RUNTIME_DIR:-/run/user/$(id -u)}\"; "
    "[ -n \"$HYPRLAND_INSTANCE_SIGNATURE\" ] || export HYPRLAND_INSTANCE_SIGNATURE=\"$(ls -t \"$XDG_RUNTIME_DIR/hypr\" 2>/dev/null | head -1)\"; "
    "[ -n \"$WAYLAND_DISPLAY\" ] || export WAYLAND_DISPLAY=\"$(ls \"$XDG_RUNTIME_DIR\" 2>/dev/null | grep -m1 -E '^wayland-[0-9]+$')\"; "
    "if [ -z \"$OMARCHY_PATH\" ]; then "
    "if [ -d \"$HOME/.local/share/omarchy/.git\" ]; then export OMARCHY_PATH=\"$HOME/.local/share/omarchy\"; "
    "else export OMARCHY_PATH=/usr/share/omarchy; fi; fi; "
    "export PATH=\"$OMARCHY_PATH/bin:$HOME/.local/bin:$PATH\"; "
)

# Runs on the target through `python3 -`, before anything is copied there.
FACTS_PY = r'''
import json, os, platform, shutil, subprocess, sys
from pathlib import Path
home = Path.home()
def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""
def read(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return ""
def host_key():
    """sshd's ed25519 public key, wherever this machine's sshd keeps it."""
    paths = ["/etc/ssh/ssh_host_ed25519_key"]
    for cfg in ["/etc/ssh/sshd_config", *sorted(str(p) for p in Path("/etc/ssh/sshd_config.d").glob("*.conf"))]:
        for line in read(cfg).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].lower() == "hostkey":
                paths.insert(0, parts[1])
    for key in paths:
        pub = read(key + ".pub")
        if pub.startswith("ssh-ed25519 "):
            return " ".join(pub.split()[:2])
    return ""
uid = os.getuid()
runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
hypr = runtime / "hypr"
session = hypr.is_dir() and any(hypr.iterdir())
shell_up = bool(sh(["pgrep", "-u", str(uid), "-x", "quickshell"]) or sh(["pgrep", "-u", str(uid), "-x", "qs"]))
plugin = home / ".config/omarchy/plugins/gladimdim.config-sync"
manifest = {}
try:
    manifest = json.loads((plugin / "manifest.json").read_text())
except Exception:
    pass
state = {}
try:
    state = json.loads((home / ".local/share/omarchy-config-sync/state.json").read_text())
except Exception:
    pass
units = sh(["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend", "--plain"])
def desktop_owner():
    """Who owns the Omarchy desktop: a live graphical session, else autologin."""
    for line in sh(["loginctl", "list-sessions", "--no-legend"]).splitlines():
        sid = line.split()[0] if line.split() else ""
        props = dict(l.split("=", 1) for l in sh(["loginctl", "show-session", sid, "-p", "Type", "-p", "Name"]).splitlines() if "=" in l)
        if props.get("Type") in ("wayland", "x11"):
            return props.get("Name", "")
    import glob
    for conf in sorted(glob.glob("/etc/sddm.conf.d/*.conf")) + ["/etc/sddm.conf"]:
        for l in read(conf).splitlines():
            if l.strip().startswith("User=") and l.strip()[5:]:
                return l.strip()[5:]
    return ""
import pwd
accounts = sorted(p.pw_name for p in pwd.getpwall()
                  if 1000 <= p.pw_uid < 60000 and not p.pw_shell.endswith(("nologin", "false")) and os.path.isdir(p.pw_dir))
usage = shutil.disk_usage(home)
print(json.dumps({
    "hostname": platform.node(),
    "user": os.environ.get("USER") or "",
    "machine_id": os.environ.get("OMARCHY_CLONE_TEST_MACHINE_ID") or read("/etc/machine-id"),
    "host_key": os.environ.get("OMARCHY_CLONE_TEST_HOST_KEY") or host_key(),
    "arch": platform.machine(),
    "python": list(sys.version_info[:3]),
    "git": sh(["git", "--version"]),
    "omarchy": sh(["pacman", "-Q", "omarchy"]).split(" ")[-1] if sh(["pacman", "-Q", "omarchy"]) else "",
    "omarchy_dev": sh(["omarchy-version"]),
    "hyprland": (sh(["pacman", "-Q", "hyprland"]).split(" ") + [""])[1],
    "quickshell": (sh(["pacman", "-Q", "quickshell"]).split(" ") + [""])[1],
    "has_omarchy": bool(shutil.which("omarchy") or (home / ".local/share/omarchy").is_dir() or Path("/usr/share/omarchy").is_dir()),
    "session": session,
    "shell_up": shell_up,
    "free_bytes": usage.free,
    "plugin_version": manifest.get("version", ""),
    "plugin_git": (plugin / ".git").exists(),
    "linked_repo": state.get("repo_url", ""),
    "lineage": state.get("lineage") or {},
    "units": [u.split()[0] for u in units.splitlines() if u.strip()],
    "commands": {c: bool(shutil.which(c)) for c in sys.argv[1:]},
    "home": str(home),
    "login_user": pwd.getpwuid(uid).pw_name,
    "desktop_owner": desktop_owner(),
    "accounts": accounts,
}))
'''

NVIDIA_PATTERNS = ("LIBVA_DRIVER_NAME", "__GLX_VENDOR_LIBRARY_NAME", "NVD_BACKEND", "GBM_BACKEND", "nvidia")
SECRET_PATTERNS = [
    (re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}"), "GitHub token"),
    (re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}"), "GitHub token"),
    (re.compile(rb"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(rb"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"), "API secret key"),
    (re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
    # A quoted literal that mixes letters and digits, assigned to a secret-ish
    # name. Identifiers (token = normalizeToken) and word fixtures do not match.
    (re.compile(rb"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*['\"](?=[A-Za-z0-9_\-/+=]*[0-9])(?=[A-Za-z0-9_\-/+=]*[A-Za-z])[A-Za-z0-9_\-/+=]{20,}['\"]"), "credential assignment"),
]
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|fixtures?)/|\.(test|spec)\.[A-Za-z]+$")
SECRET_NAMES = re.compile(r"(^|/)(\.env(\..*)?|id_(rsa|ed25519|ecdsa|dsa)|.*\.pem|.*\.key|\.netrc|credentials(\.json)?|.*\.kdbx)$")
COMMAND_WRAPPERS = {"uwsm", "uwsm-app", "app", "--", "setsid", "env", "exec", "nohup", "sh", "bash", "-c", "systemd-run", "--user", "--scope"}


# --------------------------------------------------------------------------- helpers


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_dest(dest: str) -> str:
    dest = str(dest or "").strip()
    if not dest or dest.startswith("-") or not DEST_RE.match(dest):
        raise cs.SyncError("Enter the new machine as host or user@host (letters, digits, dots, dashes).")
    return dest


def safe_host_key(dest: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", dest)[:80]


def clones_dir(ctx: cs.Context) -> Path:
    return ctx.state_dir / "clones"


def load_run(ctx: cs.Context, dest: str) -> dict[str, Any]:
    path = clones_dir(ctx) / f"{safe_host_key(dest)}.json"
    data = cs.load_json(path, default={}, within=ctx.state_dir) if path.is_file() else {}
    return data if isinstance(data, dict) else {}


def save_run(ctx: cs.Context, dest: str, run: dict[str, Any]) -> None:
    clones_dir(ctx).mkdir(parents=True, exist_ok=True)
    cs.write_json(clones_dir(ctx) / f"{safe_host_key(dest)}.json", run, within=ctx.state_dir)


def check(cid: str, title: str, status: str, detail: str = "", fix: str = "") -> dict[str, str]:
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix}


def source_facts() -> dict[str, Any]:
    proc = subprocess.run([sys.executable, "-"], input=FACTS_PY, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise cs.SyncError("Could not read this machine's own facts.")


class Target:
    """The new machine. Every command is an argv list; nothing is interpolated.

    OMARCHY_CLONE_FAKE_HOME turns the target into a local directory so the whole
    flow can be tested without a second machine.
    """

    def __init__(self, dest: str):
        self.dest = validate_dest(dest)
        self.inhibit_note = ""
        self.fake_home = os.environ.get("OMARCHY_CLONE_FAKE_HOME") or ""

    def remote_string(self, argv: list[str], session: bool = False) -> str:
        cmd = "env LC_ALL=C " + shlex.join(argv)
        return (SESSION_PREFIX + cmd) if session else cmd

    def run(self, argv: list[str], *, input: bytes | None = None, timeout: int = 60,
            session: bool = False, what: str = "") -> subprocess.CompletedProcess:
        if self.fake_home:
            # Test target: a directory on this machine. Never touch this
            # machine's session, and use this interpreter (not a PATH shim).
            if argv and argv[0] in {"setsid", "systemd-inhibit", "timeout", "omarchy", "hyprctl"}:
                if argv[0] != "systemd-inhibit":
                    return subprocess.CompletedProcess(argv, 0, b"", b"")
                argv = argv[argv.index("python3"):] if "python3" in argv else argv
            argv = [sys.executable if a == "python3" else a for a in argv]
            session = False
        remote = self.remote_string(argv, session)
        if self.fake_home:
            env = dict(os.environ, HOME=self.fake_home, OMARCHY_CLONE_TEST_MACHINE_ID="fake-target-machine",
                       OMARCHY_CLONE_TEST_HOST_KEY="ssh-ed25519 FAKETARGETKEY")
            env.pop("XDG_DATA_HOME", None)
            env.pop("OMARCHY_CLONE_FAKE_HOME", None)
            full, cwd = ["bash", "-c", remote], self.fake_home
        else:
            env, cwd = None, None
            full = SSH_BASE + ["--", self.dest, remote]
        try:
            return subprocess.run(full, input=input, capture_output=True, timeout=timeout, env=env, cwd=cwd)
        except subprocess.TimeoutExpired:
            raise cs.SyncError(f"{what or 'The remote step'} timed out after {timeout}s on {self.dest}.")

    def json(self, argv: list[str], *, input: bytes | None = None, timeout: int = 120, what: str = "",
             session: bool = False) -> dict[str, Any]:
        proc = self.run(argv, input=input, timeout=timeout, what=what, session=session)
        out = proc.stdout.decode("utf-8", "replace").strip()
        # Banners and locale noise go to stderr; the last line is our JSON.
        line = out.splitlines()[-1] if out else ""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            err = proc.stderr.decode("utf-8", "replace").strip()[-600:]
            raise cs.SyncError(f"{what or 'Remote step'} failed on {self.dest}: {err or out[-300:] or 'no output'}")
        if not isinstance(data, dict):
            raise cs.SyncError(f"{what or 'Remote step'} returned unexpected output.")
        return data

    def facts(self, commands: list[str] | None = None) -> dict[str, Any]:
        return self.json(["python3", "-"] + list(commands or []), input=FACTS_PY.encode(),
                         timeout=40, what="Reading the new machine")

    def can_inhibit(self) -> bool:
        """Keeping the target awake needs polkit's OK, which an SSH login often lacks."""
        if self.fake_home:
            return False
        proc = self.run(INHIBIT + ["true"], timeout=20, what="Checking sleep inhibit")
        self.inhibit_note = "" if proc.returncode == 0 else "could not keep the new machine awake; keep it from sleeping"
        return proc.returncode == 0

    def py(self, remote_dir: str, command: list[str], *, timeout: int = 300, what: str = "",
           session: bool = False, inhibit: bool = False) -> dict[str, Any]:
        argv = ["python3", f"{remote_dir}/plugin/scripts/clone_machine.py"] + command
        if inhibit and self.can_inhibit():
            argv = INHIBIT + argv
        return self.json(argv, timeout=timeout, what=what, session=session)


def classify_ssh_error(stderr: str) -> tuple[str, str]:
    s = stderr.lower()
    if "host key verification failed" in s or "no matching host key" in s:
        return "hostkey", "This computer has never seen the new machine's SSH fingerprint. Accept it once in a terminal."
    if "remote host identification has changed" in s:
        return "hostkey-changed", "The new machine's fingerprint changed (a reinstall does this). Remove the old one with: ssh-keygen -R <host>"
    if "permission denied" in s:
        return "auth", "The new machine does not know this computer's SSH key yet. Copy it over once (you type its password one time)."
    if "could not resolve" in s or "name or service not known" in s:
        return "dns", "That name does not resolve. Use its IP address, its Tailscale name, or check the spelling."
    if "timed out" in s or "no route" in s or "connection refused" in s:
        return "network", "Cannot reach SSH on the new machine. Is it on, on the same network (or Tailscale), with SSH enabled?"
    return "other", stderr.strip()[-300:]


def ssh_resolve(dest: str) -> tuple[str, int, str]:
    """(hostname, port, user) as ssh will actually use them, ~/.ssh/config included."""
    try:
        out = subprocess.run(["ssh", "-G", "--", dest], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    vals: dict[str, str] = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        vals.setdefault(k, v)
    return vals.get("hostname", dest.split("@")[-1]), int(vals.get("port", "22") or 22), vals.get("user", "")


def proxied(dest: str) -> bool:
    try:
        out = subprocess.run(["ssh", "-G", "--", dest], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.startswith(("proxyjump ", "proxycommand ")) and not line.endswith(" none")
               for line in out.splitlines())


def source_ip_for(host: str) -> str:
    """This machine's address as the target will see it (for a tight firewall rule)."""
    try:
        addr = socket.getaddrinfo(host, 22, socket.AF_INET)[0][4][0]
        out = subprocess.run(["ip", "-4", "-o", "route", "get", addr], capture_output=True, text=True, timeout=5).stdout
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def same_machine(a: dict[str, Any], b: dict[str, Any]) -> bool | None:
    """True/False when it can tell, None when neither id is readable.

    machine-id alone is not enough: imaged laptops and cloned VMs share it.
    The SSH host key differs between machines even then.
    """
    ids = (a.get("machine_id") or "", b.get("machine_id") or "")
    keys = (a.get("host_key") or "", b.get("host_key") or "")
    if not all(ids) and not all(keys):
        return None
    if all(keys) and keys[0] != keys[1]:
        return False
    if all(ids) and ids[0] != ids[1]:
        return False
    return True


def version_tuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", (v or "").split("-")[0])
    return tuple(int(n) for n in nums)


# --------------------------------------------------------------------------- probe


def cmd_probe(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    target = Target(args.target)
    checks: list[dict[str, str]] = []
    source_ip = "" if target.fake_home or proxied(target.dest) else source_ip_for(ssh_resolve(target.dest)[0])

    # 1. Network
    if target.fake_home:
        checks.append(check("reach", "Reach the new machine", "pass", "local test target"))
    elif proxied(target.dest):
        checks.append(check("reach", "Reach the new machine", "pass", "through a jump host in ~/.ssh/config"))
    else:
        host, port, _ = ssh_resolve(target.dest)
        try:
            with socket.create_connection((host, port), timeout=5):
                pass
            checks.append(check("reach", "Reach the new machine", "pass", f"{host}:{port} answers"))
        except OSError as exc:
            checks.append(check("reach", "Reach the new machine", "fail", f"{host}:{port}: {exc}",
                                "network"))
            return cs.ok({"target": target.dest, "source_ip": source_ip, "checks": checks, "ready": False, "stage": "reach"})

    # 2. SSH login without a password
    proc = target.run(["true"], timeout=20, what="SSH login")
    if proc.returncode != 0:
        kind, hint = classify_ssh_error(proc.stderr.decode("utf-8", "replace"))
        checks.append(check("ssh", "Log in with an SSH key", "fail", hint, kind))
        return cs.ok({"target": target.dest, "source_ip": source_ip, "checks": checks, "ready": False, "stage": "ssh"})
    checks.append(check("ssh", "Log in with an SSH key", "pass", "no password needed"))

    # 3. Is it a machine we can stamp?
    mine = source_facts()
    theirs = target.facts()
    blocking = False

    def add(cid: str, title: str, good: bool, detail: str, fix: str = "", warn: bool = False) -> None:
        nonlocal blocking
        status = "pass" if good else ("warn" if warn else "fail")
        if status == "fail":
            blocking = True
        checks.append(check(cid, title, status, detail, fix))

    same = same_machine(mine, theirs)
    if same is None:
        add("self", "Not this machine", False, "Could not read a machine id or SSH host key, so it cannot be told apart from this machine.")
    else:
        add("self", "Not this machine", not same,
            f"{theirs.get('hostname')} is a different machine" if not same else "That address is THIS machine. A clone onto itself is refused.")
    me_user, owner = theirs.get("login_user") or "", theirs.get("desktop_owner") or ""
    others = [a for a in theirs.get("accounts") or [] if a != me_user]
    if owner and owner != me_user:
        add("account", "The account that owns the desktop", False,
            f"You reached {theirs.get('hostname')} as '{me_user}', but its Omarchy desktop belongs to '{owner}'. "
            f"Use {owner}@{target.dest.split('@')[-1]} instead (this computer's key must be on that account).",
            owner + "@" + target.dest.split("@")[-1])
    elif not owner and others:
        checks.append(check("account", "The account that owns the desktop", "warn",
                            f"Logged in as '{me_user}'. Nobody is logged in there, and it also has: " + ", ".join(others)
                            + ". Pick the account you use on it.", ",".join([me_user] + others)))
    else:
        checks.append(check("account", "The account that owns the desktop", "pass", f"'{me_user}'"))
    add("omarchy", "Omarchy is installed", bool(theirs.get("has_omarchy")),
        f"Omarchy {theirs.get('omarchy') or theirs.get('omarchy_dev') or '?'}" if theirs.get("has_omarchy") else "No Omarchy found. Install Omarchy first: https://omarchy.org",
        "install-omarchy")
    live = bool(theirs.get("session")) and bool(theirs.get("shell_up"))
    add("session", "Desktop is running", live,
        "Hyprland and the Omarchy bar are up" if live
        else "Nobody is logged in to its desktop. The clone can still copy everything; it takes effect at the next login.",
        "session", warn=True)
    py = tuple(theirs.get("python") or (0,))
    add("python", "Python 3.10+", py >= MIN_PYTHON, "Python " + ".".join(str(x) for x in py))
    add("git", "git", bool(theirs.get("git")), theirs.get("git") or "git is missing: sudo pacman -S git")
    add("arch", "Same CPU type", mine.get("arch") == theirs.get("arch"),
        f"{theirs.get('arch')} (this machine: {mine.get('arch')})",
        "Programs in ~/.local/bin are built for this CPU type.")
    free = int(theirs.get("free_bytes") or 0)
    add("disk", "Free disk space", free >= MIN_FREE_BYTES, f"{free // (1024 ** 3)} GiB free")
    mv, tv = mine.get("omarchy") or "", theirs.get("omarchy") or ""
    same_ver = mv == tv
    detail = f"this machine {mv or mine.get('omarchy_dev')}, new machine {tv or theirs.get('omarchy_dev')}"
    if not same_ver and version_tuple(tv) < version_tuple(mv):
        detail += (". The new machine is older: plugins written for the newer Omarchy may fill its logs with errors. "
                   "Update it first (omarchy update), or tick Continue anyway.")
    add("version", "Same Omarchy version", same_ver, detail, "version", warn=True)
    linked = theirs.get("linked_repo") or ""
    add("linked", "Not already linked", not linked,
        "No Config Sync link yet" if not linked else f"Already linked to {linked}. The clone replaces that link (it is backed up).",
        "linked", warn=True)
    return cs.ok({
        "target": target.dest,
        "source_ip": source_ip,
        "checks": checks,
        "ready": not blocking,
        "stage": "machine",
        "source": {k: mine.get(k) for k in ("hostname", "arch", "omarchy", "omarchy_dev", "hyprland", "quickshell")},
        "facts": {k: theirs.get(k) for k in ("hostname", "user", "arch", "omarchy", "omarchy_dev", "hyprland",
                                           "quickshell", "plugin_version", "linked_repo", "lineage",
                                           "login_user", "desktop_owner", "accounts")},
    })


# --------------------------------------------------------------------------- preview


def runtime_tar() -> bytes:
    """This plugin's own files, as they are here, for the target."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(PLUGIN_ROOT.rglob("*")):
            rel = path.relative_to(PLUGIN_ROOT)
            if any(part in RUNTIME_SKIP for part in rel.parts) or path.is_symlink() or not path.is_file():
                continue
            if path.suffix in {".pyc"}:
                continue
            tar.add(str(path), arcname=f"plugin/{rel.as_posix()}", recursive=False)
    return buf.getvalue()


def scan_repo(repo: Path, hostname: str, ts_ips: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Secrets (never shipped) and machine-specific settings (your call) in HEAD's tree."""
    secrets: list[dict[str, Any]] = []
    specific: list[dict[str, Any]] = []
    files = cs.git_out(repo, "ls-files", "-z", timeout=30).split("\0")
    host_re = re.compile(rb"\b" + re.escape(hostname.encode()) + rb"\b") if hostname else None
    for rel in files:
        if not rel:
            continue
        path = repo / rel
        if not path.is_file() or path.is_symlink():
            continue
        if SECRET_NAMES.search(rel):
            secrets.append({"path": rel, "reason": "file name looks like a secret"})
            continue
        try:
            size = path.stat().st_size
            if size > MAX_SCAN_BYTES:
                if not is_compiled(path):
                    secrets.append({"path": rel, "reason": "too large to scan for secrets"})
                continue
            data = path.read_bytes()
        except OSError:
            continue
        binary = b"\0" in data[:4096]
        hit = ""
        for pattern, label in SECRET_PATTERNS:
            if label == "credential assignment" and (binary or size > 2 * 1024 * 1024 or TEST_PATH.search(rel)):
                continue
            if pattern.search(data):
                hit = label
                break
        if hit:
            secrets.append({"path": rel, "reason": hit})
            continue
        if binary:
            continue
        reasons: list[str] = []
        if host_re and host_re.search(data):
            reasons.append(f"mentions '{hostname}'")
        for ip in ts_ips:
            if ip and ip.encode() in data:
                reasons.append(f"mentions this machine's address {ip}")
        if any(p.encode() in data for p in NVIDIA_PATTERNS) and rel.startswith("hypr/"):
            reasons.append("NVIDIA graphics settings")
        if b"solaar" in data.lower():
            reasons.append("mouse/keyboard (Solaar) settings")
        if reasons:
            specific.append({"path": rel, "reasons": reasons})
    return secrets, specific


def is_compiled(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def compiled_files(repo: Path) -> list[str]:
    """Built programs in the repo. They are never copied: build or install them there."""
    files = cs.git_out(repo, "ls-files", "-z", timeout=30).split("\0")
    return sorted(rel for rel in files if rel and (repo / rel).is_file() and is_compiled(repo / rel))


def snapshot_commit(repo: Path, drop: set[str], message: str) -> str:
    """One parentless commit of HEAD's tree minus `drop`. History never travels."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(cs.git_env(), GIT_INDEX_FILE=str(Path(tmp) / "index"),
                   GIT_AUTHOR_NAME="Config Sync", GIT_AUTHOR_EMAIL="config-sync@localhost",
                   GIT_COMMITTER_NAME="Config Sync", GIT_COMMITTER_EMAIL="config-sync@localhost")

        def git(*a: str, input: str | None = None) -> str:
            res = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=", *a], capture_output=True,
                                 text=True, env=env, timeout=120, input=input)
            if res.returncode != 0:
                raise cs.SyncError("Could not build the snapshot: " + res.stderr[-300:])
            return res.stdout.strip()

        git("read-tree", "HEAD")
        for rel in sorted(drop):
            git("rm", "--cached", "-q", "--ignore-unmatch", "--", rel)
        tree = git("write-tree")
        return git("commit-tree", tree, "-m", message)


LUA_STRING = re.compile(r'\[\[(.*?)\]\]|"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', re.S)


def bind_commands(bindings: str) -> list[str]:
    """First program of each string command in bindings.lua (o.bind(keys, label, "cmd"))."""
    out: list[str] = []
    for entry in cs.extract_bind_statements(bindings):
        raw = str(entry.get("raw") or "")
        strings = [next(g for g in m.groups() if g is not None) for m in LUA_STRING.finditer(raw)]
        command = strings[2] if len(strings) >= 3 else ""
        if not command or command.startswith(("hl.", "o.", "function")):
            continue
        if "sh -c" in command or command.startswith(("sh ", "bash ")):
            inner = LUA_STRING.search(command.split("-c", 1)[-1]) if "-c" in command else None
            command = next((g for g in inner.groups() if g is not None), command) if inner else command
        try:
            words = shlex.split(command)
        except ValueError:
            continue
        for w in words:
            if w in COMMAND_WRAPPERS or w.startswith("-") or "=" in w:
                continue
            name = os.path.basename(w)
            if re.match(r"^[A-Za-z0-9._+-]+$", name):
                out.append(name)
            break
    return out


LAUNCH_CALL = re.compile(r"\b(?:launch_on_start|exec_once|exec_cmd|exec)\s*\(\s*(?:\[\[(.*?)\]\]|\"((?:[^\"\\\\]|\\\\.)*)\"|'((?:[^'\\\\]|\\\\.)*)')", re.S)


def autostart_commands(repo: Path) -> list[str]:
    """First program of every launch_on_start/exec call in the repo's hypr/*.lua."""
    out: list[str] = []
    hypr = repo / "hypr"
    if not hypr.is_dir():
        return out
    for path in sorted(hypr.glob("*.lua")):
        if path.is_symlink():
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("--"):
                continue
            for m in LAUNCH_CALL.finditer(line):
                cmd = next((g for g in m.groups() if g is not None), "")
                try:
                    words = shlex.split(cmd)
                except ValueError:
                    continue
                for w in words:
                    if w in COMMAND_WRAPPERS or w.startswith("-") or "=" in w:
                        continue
                    name = os.path.basename(w)
                    if re.match(r"^[A-Za-z0-9._+-]+$", name):
                        out.append(name)
                    break
    return out


def hook_interpreters(repo: Path) -> list[str]:
    out = []
    hooks = repo / "omarchy" / "hooks"
    if hooks.is_dir():
        for path in hooks.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    first = path.open("rb").readline(200).decode("utf-8", "replace")
                except OSError:
                    continue
                if first.startswith("#!"):
                    parts = first[2:].split()
                    if parts:
                        prog = os.path.basename(parts[1] if parts[0].endswith("/env") and len(parts) > 1 else parts[0])
                        out.append(prog)
    return out


def owning_packages(commands: list[str]) -> dict[str, dict[str, str]]:
    """command -> {package, repo: 'repo'|'aur'} for commands this machine has."""
    foreign = set()
    try:
        foreign = set(subprocess.run(["pacman", "-Qqm"], capture_output=True, text=True, timeout=20).stdout.split())
    except (OSError, subprocess.TimeoutExpired):
        pass
    out: dict[str, dict[str, str]] = {}
    for c in sorted(set(commands)):
        path = shutil.which(c)
        if not path or path.startswith(str(Path.home())):
            continue
        try:
            pkg = subprocess.run(["pacman", "-Qqo", path], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pkg = ""
        if pkg:
            out[c] = {"package": pkg, "repo": "aur" if pkg in foreign else "repo"}
    return out


def user_units(repo_units: list[str]) -> dict[str, str]:
    """unit -> 'custom' if it lives in ~/.config/systemd/user, else 'package'."""
    home_units = Path.home() / ".config/systemd/user"
    out = {}
    for u in repo_units:
        out[u] = "custom" if (home_units / u).exists() else "package"
    return out


def cmd_preview(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    target = Target(args.target)
    old = load_run(ctx, target.dest)
    if str(old.get("stage", "")).startswith("failed:") or old.get("stage") in {"cloning", "deferred"}:
        raise cs.SyncError("A clone onto this machine is not finished. Resume it or Undo it before a new preview.",
                           extra={"unfinished": True, "runid": old.get("runid")})
    repo = cs.configured_repo(ctx)
    if cs.git_out(repo, "status", "--porcelain", timeout=30).strip():
        raise cs.SyncError("This machine's repo clone has uncommitted changes. Publish or clean it first, then Preview again.")
    head = cs.git_out(repo, "rev-parse", "HEAD")
    subject = cs.git_out(repo, "log", "-1", "--format=%s")
    committed = cs.git_out(repo, "log", "-1", "--format=%cI")
    if not head:
        raise cs.SyncError("This machine's repo has no commits yet. Seed it first (Overview), then come back.")

    snap = cs.build_snapshot(ctx, fetch=False)
    unpublished = int((snap.get("status") or {}).get("local_changes") or 0)

    mine = source_facts()
    ts_ips = []
    if shutil.which("tailscale"):
        try:
            ts_ips = subprocess.run(["tailscale", "ip"], capture_output=True, text=True, timeout=5).stdout.split()
        except (OSError, subprocess.TimeoutExpired):
            ts_ips = []
    secrets, specific = scan_repo(repo, mine.get("hostname", ""), ts_ips)
    secret_paths = {s["path"] for s in secrets}
    built = compiled_files(repo)

    # Ship a history-free snapshot with secret files removed: nothing that was
    # ever committed and later deleted, and nothing the scan flagged, leaves here.
    snapshot = snapshot_commit(repo, secret_paths, f"Snapshot of {mine.get('hostname')} @ {head[:10]}")
    runid = uuid.uuid4().hex[:12]
    remote_dir = f"{REMOTE_BASE}/{runid}"
    ref = f"refs/config-sync/snapshot-{runid}"
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / "repo.bundle"
        cs.run_git(repo, ["update-ref", ref, snapshot], check=True)
        try:
            res = cs.run_git(repo, ["bundle", "create", str(bundle), ref], timeout=120)
        finally:
            cs.run_git(repo, ["update-ref", "-d", ref])
        if res.returncode != 0:
            raise cs.SyncError("Could not bundle this machine's repo: " + (res.stderr or "")[-300:])
        if bundle.stat().st_size > MAX_BUNDLE_BYTES:
            raise cs.SyncError("The repo bundle is over 1 GiB. Trim large files from the repo first.")
        bundle_bytes = bundle.read_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("repo.bundle")
        info.size = len(bundle_bytes)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(bundle_bytes))
        with tarfile.open(fileobj=io.BytesIO(runtime_tar())) as rt:
            for m in rt.getmembers():
                tar.addfile(m, rt.extractfile(m))
    payload = buf.getvalue()
    expected = {"repo.bundle": sha256_bytes(bundle_bytes),
                "plugin/scripts/clone_machine.py": sha256_bytes(Path(__file__).read_bytes()),
                "plugin/scripts/config_sync.py": sha256_bytes((Path(__file__).parent / "config_sync.py").read_bytes())}

    proc = target.run(["bash", "-c", 'umask 077 && mkdir -p -- "$1" && tar -x -C "$1"', "_", remote_dir],
                      input=payload, timeout=300, what="Sending the bundle")
    if proc.returncode != 0:
        raise cs.SyncError("Could not copy the bundle to the new machine: " + proc.stderr.decode("utf-8", "replace")[-300:])
    sums = target.run(["sha256sum", "--"] + [f"{remote_dir}/{k}" for k in expected], timeout=60, what="Checking the copy")
    got = {}
    for line in sums.stdout.decode().splitlines():
        digest, _, name = line.partition("  ")
        got[name.removeprefix(remote_dir + "/")] = digest
    if got != expected:
        target.run(["rm", "-rf", "--", remote_dir], timeout=30)
        raise cs.SyncError("The copy on the new machine does not match what was sent (hash mismatch). Nothing was changed; try again.")

    plan = target.py(remote_dir, ["target-plan", "--dir", remote_dir, "--commit", snapshot, "--runid", runid],
                     timeout=300, what="Dry run on the new machine")
    if not plan.get("ok"):
        raise cs.SyncError(plan.get("error") or "The dry run failed on the new machine.")

    bindings_path = repo / "hypr" / "bindings.lua"
    bindings = cs.read_text(bindings_path, within=repo) if bindings_path.is_file() else ""
    wanted = sorted(set(bind_commands(bindings) + hook_interpreters(repo) + autostart_commands(repo)))
    owners = owning_packages(wanted)
    theirs = target.facts(sorted(owners))
    missing = [{"command": c, **owners[c]} for c in sorted(owners) if not theirs.get("commands", {}).get(c)]
    repo_pkgs = sorted({m["package"] for m in missing if m["repo"] == "repo"})
    aur_pkgs = sorted({m["package"] for m in missing if m["repo"] == "aur"})

    source_units = set(mine.get("units") or [])
    target_units = set(theirs.get("units") or [])
    services = [{"unit": u, "kind": k} for u, k in user_units(sorted(source_units - target_units)).items()]

    # Plugin install commands are built HERE from this machine's own list,
    # never taken from the target's output.
    have = {p.name for p in ctx.config_plugins.iterdir() if p.is_dir() and not p.is_symlink()} if ctx.config_plugins.is_dir() else set()
    exact = args.plugins != "fresh"
    plugin_cmds, copy_plugins = source_plugin_plan(repo, plan.get("plugins") or [], exact, have)
    copy_plugins = [pid for pid in copy_plugins if pid in have]
    git_ok = public_plugin_repos(ctx, copy_plugins)
    replace_plugins = [r.get("id") for r in plan.get("plugins") or []
                       if isinstance(r, dict) and r.get("action") in {"update", "replace"} and r.get("id") in copy_plugins]
    copy_bytes = sum(f.stat().st_size for pid in copy_plugins for f in (ctx.config_plugins / pid).rglob("*")
                     if f.is_file() and not f.is_symlink())
    wallpapers = wallpaper_payload(ctx)
    run = {
        "dest": target.dest, "runid": runid, "remote_dir": remote_dir, "commit": head, "snapshot": snapshot,
        "target_hostname": theirs.get("hostname"), "target_identity": theirs.get("host_key") or theirs.get("machine_id") or "",
        "source_hostname": mine.get("hostname"),
        "stage": "previewed", "done": [], "previewed_at": now_iso(),
        "repo_pkgs": repo_pkgs, "aur_pkgs": aur_pkgs, "plugin_cmds": plugin_cmds, "services": services,
        "secret_paths": sorted(secret_paths),
        "built_paths": built,
        "wallpapers": wallpapers,
        "copy_plugins": copy_plugins,
        "replace_plugins": replace_plugins,
        "git_ok": git_ok,
    }
    save_run(ctx, target.dest, run)
    return cs.ok({
        "target": target.dest, "runid": runid,
        "source": {"hostname": mine.get("hostname"), "commit": head[:10], "subject": subject,
                   "committed": committed, "unpublished": unpublished},
        "target_hostname": theirs.get("hostname"),
        "plan": plan, "plugin_cmds": plugin_cmds, "copy_plugins": copy_plugins,
        "plugins_mode": "exact" if exact else "fresh", "copy_bytes": copy_bytes,
        "wallpapers": {"count": len(wallpapers["files"]), "bytes": wallpapers["bytes"], "current": wallpapers["current"]},
        "secrets": secrets, "specific": specific, "built": built,
        "missing": missing, "repo_pkgs": repo_pkgs, "aur_pkgs": aur_pkgs,
        "services": services,
        "not_cloned": [
            "Files that look like secrets (listed above) and the repo's history: only today's files travel",
            "Installed packages and apps (missing ones are listed above)",
            "Compiled programs (build or install them on the new machine from their source or package)",
            "Passwords, SSH keys, tokens, 1Password, browser profiles and logins",
            "Tailscale login and other network accounts",
            "User accounts, sudo rules and system settings under /etc",
            "Files that only exist on the new machine (they are kept)",
        ],
    })


def wallpaper_payload(ctx: cs.Context) -> dict[str, Any]:
    """The current background and the active theme's own backgrounds, home-relative."""
    home = ctx.home
    files: list[str] = []
    current = ""
    link = home / BACKGROUND_LINK_REL
    try:
        real = link.resolve(strict=True) if link.is_symlink() else None
    except OSError:
        real = None
    if real and real.is_file():
        try:
            rel = real.relative_to(home).as_posix()
            if rel.startswith(".config/omarchy/"):
                current = rel
                files.append(rel)
        except ValueError:
            pass
    slug = cs.read_theme_slug(ctx.theme_name_path, within=home) if ctx.theme_name_path.is_file() else ""
    bgdir = home / ".config/omarchy/themes" / slug / "backgrounds" if slug else None
    if bgdir and bgdir.is_dir() and not bgdir.is_symlink():
        for f in sorted(bgdir.iterdir()):
            if f.is_file() and not f.is_symlink():
                rel = f.relative_to(home).as_posix()
                if rel not in files:
                    files.append(rel)
    total = 0
    kept = []
    for rel in files:
        size = (home / rel).stat().st_size
        if total + size > MAX_WALLPAPER_BYTES:
            continue  # the cap applies to every file, the current one included
        total += size
        kept.append(rel)
    return {"files": kept, "current": current if current in kept else "", "bytes": total}


SSH_GITHUB = re.compile(r"^git@github\.com:([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?$")


def anonymous_source(source: str) -> str:
    """A URL a machine with no GitHub key can clone, or "" when there is none.

    A new machine has no SSH key for GitHub, so git@github.com:owner/repo only
    works there if the same repo is public over HTTPS. Checked from here.
    """
    m = SSH_GITHUB.match(source or "")
    url = f"https://github.com/{m.group(1)}/{m.group(2)}.git" if m else source
    if not url.startswith("https://"):
        return ""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", SSH_ASKPASS="true")
    try:
        ok = subprocess.run(["git", "-c", "credential.helper=", "ls-remote", "--", url, "HEAD"], capture_output=True,
                            env=env, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    return url if ok else ""


def public_plugin_repos(ctx: cs.Context, ids: list[str]) -> list[str]:
    """Plugins whose git history may travel: their origin is publicly readable.

    A private plugin's history could hold a secret that was committed and later
    deleted, so those travel as their working files only.
    """
    from concurrent.futures import ThreadPoolExecutor

    def check(pid: str) -> str:
        root = ctx.config_plugins / pid
        url = cs.git_out(root, "remote", "get-url", "origin", timeout=10)
        if not url or not anonymous_source(cs.sanitize_url(url) if hasattr(cs, "sanitize_url") else url):
            return ""
        # Every commit must already be on the remote: no unpushed history travels.
        if cs.git_out(root, "rev-list", "--all", "--not", "--remotes", "-n", "1", timeout=20):
            return ""
        return pid

    with ThreadPoolExecutor(max_workers=16) as pool:
        return sorted(p for p in pool.map(check, ids) if p)


def source_plugin_plan(repo: Path, planned: list[Any], exact: bool, have: set[str]) -> tuple[list[str], list[str]]:
    """(install/update commands, plugin ids to copy straight from this machine).

    exact: copy this machine's installed plugins as they are (same commits, no
    GitHub access or prompts needed there). Otherwise install the latest from
    GitHub, over HTTPS when the source is an SSH URL, copying only the private ones.
    """
    listed = cs.repo_plugin_list(repo)
    rows = []
    copy_ids: list[str] = []
    for item in planned:
        if not isinstance(item, dict):
            continue
        pid, action = item.get("id"), item.get("action")
        if not isinstance(pid, str) or not cs.valid_plugin_id(pid) or pid not in listed:
            continue
        if action in {"install", "update", "replace"} and exact and pid in have:
            copy_ids.append(pid)  # missing there, or at another version: take this machine's
        elif action == "install" and listed[pid].get("source"):
            url = anonymous_source(listed[pid]["source"])
            if url:
                rows.append({"id": pid, "source": url, "action": "install"})
            else:
                copy_ids.append(pid)  # private: the new machine cannot clone it
        elif action == "update":
            rows.append({"id": pid, "source": listed[pid].get("source", ""), "action": "update"})
    return cs.mirror_plugin_commands(rows), copy_ids


# --------------------------------------------------------------------------- target side


def target_ctx() -> cs.Context:
    return cs.Context.from_env()


def parse(argv: list[str]) -> argparse.Namespace:
    return cs.build_parser().parse_args(argv)


def cmd_target_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Dry run in a throwaway state dir: what a mirror would change here."""
    if not RUNID_RE.match(args.runid or ""):
        return cs.fail("Bad run id.")
    remote_dir = Path.home() / args.dir
    work = remote_dir / "data" / "omarchy-config-sync"
    seed = remote_dir / "seed.git"
    if not seed.exists():
        res = subprocess.run(["git", "clone", "--quiet", "--mirror", str(remote_dir / "repo.bundle"), str(seed)],
                             capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            return cs.fail("Could not unpack the bundle: " + res.stderr[-300:])
        ref = f"refs/config-sync/snapshot-{args.runid}"
        for cmd in (["update-ref", "refs/heads/main", ref], ["symbolic-ref", "HEAD", "refs/heads/main"],
                    ["update-ref", "-d", ref]):
            res = subprocess.run(["git", "-C", str(seed)] + cmd, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                return cs.fail("Could not prepare the bundle: " + res.stderr[-300:])
    head = subprocess.run(["git", "-C", str(seed), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if not args.commit or head != args.commit:
        return cs.fail("The bundle on the new machine is not the snapshot that was sent.")
    home = Path.home()
    ctx = cs.Context(home=home, state_dir=work, default_clone=work / "repo", plugin_root=None)
    if not (work / "repo").exists():
        cs.cmd_connect(ctx, parse(["connect", seed.resolve().as_uri()]))  # a URL clones; a path would be used in place
    state = cs.load_state(ctx)
    repo = cs.configured_repo(ctx, state)
    diff = cs.annotate_diff(ctx, repo, state)
    paths = cs.mirror_paths(diff["files"], "apply")
    files = []
    for item in diff["files"]:
        if item["path"] not in paths:
            continue
        files.append({
            "path": item["path"], "status": item["status"], "removal": bool(item.get("removal")),
            "exists_here": bool(item.get("local_exists")), "portable": bool(item.get("portable", True)),
        })
    plugin_rows = [r for r in (diff.get("plugin_list") or []) if r.get("action") in {"install", "update"}]
    # Installed here at a different commit than the source's list (newer or
    # otherwise): an exact clone rolls it to the source's version too.
    for r in diff.get("plugin_list") or []:
        if (r.get("status") == "local" and not r.get("removal") and r.get("repo_version")
                and r.get("local_version") and r["local_version"] != r["repo_version"]):
            plugin_rows.append(dict(r, action="replace"))
    return cs.ok({
        "files": files,
        "overwrite": sum(1 for f in files if f["exists_here"] and not f["removal"]),
        "create": sum(1 for f in files if not f["exists_here"]),
        "remove": sum(1 for f in files if f["removal"]),
        "hooks": [f["path"] for f in files if f["path"].startswith("omarchy/hooks/")],
        "machine": [f["path"] for f in files if not f["portable"]],
        "shell_json": any(f["path"] == "omarchy/shell.json" for f in files),
        "plugins": [{"id": r["id"], "name": r.get("name"), "action": r.get("action")} for r in plugin_rows],
    })


def undo_dir(home: Path, runid: str) -> Path:
    if not RUNID_RE.match(runid or ""):
        raise cs.SyncError("Bad run id.")
    d = home / UNDO_REL / runid
    old = os.umask(0o077)
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    finally:
        os.umask(old)
    os.chmod(d, 0o700)
    return d


def read_pre(d: Path) -> dict[str, Any]:
    try:
        data = json.loads((d / "pre.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_pre(d: Path, pre: dict[str, Any]) -> None:
    tmp = d / "pre.json.tmp"
    tmp.write_text(json.dumps(pre))
    os.replace(tmp, d / "pre.json")


def cmd_target_install_plugin(args: argparse.Namespace) -> dict[str, Any]:
    home = Path.home()
    d = undo_dir(home, args.runid)
    pre = read_pre(d)
    src = home / args.dir / "plugin"
    dst = home / PLUGIN_REL
    if "plugin_previous" not in pre:
        # Record where the old plugin goes BEFORE moving it: a crash after the
        # move must still let Undo find it.
        if dst.exists() or dst.is_symlink():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            aside = home / ".config/omarchy/plugins-backup" / f"{cs.PLUGIN_ID}.{stamp}-{args.runid}"
            pre["plugin_previous"] = str(aside)
            write_pre(d, pre)
            aside.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(aside))
        else:
            pre["plugin_previous"] = ""
            write_pre(d, pre)
    elif dst.exists():
        shutil.rmtree(dst)  # our own partial copy from an interrupted attempt
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=False)
    return cs.ok({"installed": str(dst), "previous": pre.get("plugin_previous", "")})


def cmd_target_link(args: argparse.Namespace) -> dict[str, Any]:
    home = Path.home()
    d = undo_dir(home, args.runid)
    pre = read_pre(d)
    state_dir = home / STATE_REL
    seed = home / SEED_REL
    if "state_previous" not in pre:
        keep = [p for p in state_dir.iterdir() if p.name != "clone-undo"] if state_dir.is_dir() else []
        if keep:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            aside = home / ".local/share" / f"omarchy-config-sync.pre-clone.{stamp}-{args.runid}"
            pre["state_previous"] = str(aside)
            write_pre(d, pre)
            aside.mkdir(parents=True)
            for p in keep:
                shutil.move(str(p), str(aside / p.name))
        else:
            pre["state_previous"] = ""
            write_pre(d, pre)
    else:
        # Interrupted attempt: clear only what this run created.
        for p in list(state_dir.iterdir()) if state_dir.is_dir() else []:
            if p.name != "clone-undo":
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    state_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(home / args.dir / "seed.git", seed)
    ctx = target_ctx()
    cs.cmd_connect(ctx, parse(["connect", seed.resolve().as_uri()]))  # a URL clones; a path would be used in place
    return cs.ok({"linked": str(seed), "previous_state": pre.get("state_previous", "")})


def inside(home: Path, path: Path) -> bool:
    """True when path's parent resolves inside home (no symlinked escape)."""
    try:
        path.parent.resolve().relative_to(home.resolve())
        return True
    except ValueError:
        return False


def missing_dirs(home: Path, path: Path) -> list[str]:
    """Home-relative folders above `path` that do not exist yet (the clone will create them)."""
    out = []
    parent = path.parent
    while parent != home and home in parent.parents and not parent.exists():
        out.append(parent.relative_to(home).as_posix())
        parent = parent.parent
    return out


def cmd_target_apply(args: argparse.Namespace) -> dict[str, Any]:
    """Record exactly what the mirror will touch, then mirror."""
    ctx = target_ctx()
    home = ctx.home
    d = undo_dir(home, args.runid)
    state = cs.load_state(ctx)
    repo = cs.configured_repo(ctx, state)
    diff = cs.annotate_diff(ctx, repo, state)
    exclude = cs.parse_exclude_arg(argparse.Namespace(exclude=args.exclude or ""))
    paths = cs.mirror_paths(diff["files"], "apply", exclude)
    manifest_path = d / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())  # resuming: the first record is the true "before"
        if manifest.get("undone"):
            return cs.fail("This clone was already undone. Preview again to clone afresh.")
    else:
        entries = []
        created_dirs: set[str] = set()
        with tarfile.open(d / "files.tar", "w") as tar:
            for item in diff["files"]:
                if item["path"] not in paths:
                    continue
                local = Path(item["local_path"])
                try:
                    rel = local.relative_to(home)
                except ValueError:
                    continue
                if local.is_symlink():
                    entries.append({"rel": rel.as_posix(), "kind": "symlink", "target": os.readlink(local)})
                elif local.is_file():
                    tar.add(str(local), arcname=rel.as_posix(), recursive=False)
                    entries.append({"rel": rel.as_posix(), "kind": "file"})
                else:
                    entries.append({"rel": rel.as_posix(), "kind": "absent"})
                    created_dirs.update(missing_dirs(home, local))
        pre = read_pre(d)
        manifest = {"runid": args.runid, "created_at": now_iso(), "entries": entries,
                    "plugin_previous": pre.get("plugin_previous", ""), "state_previous": pre.get("state_previous", ""),
                    "created_dirs": sorted(created_dirs), "undone": False}
        (d / "manifest.json").write_text(json.dumps(manifest))
        os.chmod(d / "manifest.json", 0o600)
        os.chmod(d / "files.tar", 0o600)
    ns = parse(["resync", "--side", "repo", "--mirror", "--skip-plugin-launch", "--exclude", json.dumps(sorted(exclude))])
    result = cs.cmd_resync(ctx, ns)
    applied = set(result.get("applied") or [])
    leaked = sorted(applied & exclude)
    state = cs.load_state(ctx)
    state["lineage"] = {"cloned_from": args.source, "commit": args.commit, "at": now_iso(), "runid": args.runid}
    state["clone_undo"] = str(d)
    cs.save_state(ctx, state)
    if leaked:
        return cs.fail("Excluded files were written anyway: " + ", ".join(leaked[:5]) + ". Undo this clone.")
    return cs.ok({"applied": sorted(applied), "removed": result.get("removed") or [],
                  "backup_dir": result.get("backup_dir"), "undo_dir": str(d),
                  "message": result.get("message"), "sync_state": result.get("sync_state")})


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cmd_target_wallpapers(args: argparse.Namespace) -> dict[str, Any]:
    """Add wallpapers (never overwrite), then point the background link at the current one.

    Each file is recorded for Undo BEFORE it is written, so a crash never
    leaves a file Undo does not know about. A file this run already started
    (an interrupted attempt) is rewritten.
    """
    home = Path.home()
    d = undo_dir(home, args.runid)
    manifest_path = d / "manifest.json"
    if not manifest_path.is_file():
        return cs.fail("Copy the desktop before the wallpapers.")
    manifest = json.loads(manifest_path.read_text())
    ours = {e["rel"] for e in manifest.get("entries", []) if e.get("kind") == "absent" and e.get("wallpaper")}
    known = {e["rel"] for e in manifest.get("entries", [])}
    added = kept = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
        for m in tar:
            name = m.name
            if (not m.isfile() or name.startswith("/") or ".." in Path(name).parts
                    or not name.startswith(".config/omarchy/")):
                continue
            dst = home / name
            if not inside(home, dst) or dst.is_symlink():
                kept += 1
                continue
            if dst.exists():
                if name not in ours:
                    kept += 1  # the target's own file: never overwritten
                    continue
                dst.unlink()  # our own partial copy from an interrupted attempt
            if name not in known:
                manifest["entries"].append({"rel": name, "kind": "absent", "wallpaper": True})
                known.add(name)
            manifest["created_dirs"] = sorted(set(manifest.get("created_dirs") or []) | set(missing_dirs(home, dst)))
            save_manifest(manifest_path, manifest)
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(tar.extractfile(m), out)
            added += 1
    link = home / BACKGROUND_LINK_REL
    current = args.current or ""
    if current and current.startswith(".config/omarchy/") and ".." not in Path(current).parts and (home / current).is_file():
        if "background_previous" not in manifest:
            manifest["background_previous"] = os.readlink(link) if link.is_symlink() else ""
            save_manifest(manifest_path, manifest)
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp = link.with_name(".background.clone-tmp")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(str(home / current), tmp)
        os.replace(tmp, link)
    save_manifest(manifest_path, manifest)
    return cs.ok({"added": added, "kept": kept, "message": f"{added} wallpaper(s) added"})


def cmd_target_local_plugins(args: argparse.Namespace) -> dict[str, Any]:
    """Plugins copied from the source: missing here, or at another version.

    Each plugin folder is recorded for Undo BEFORE anything is moved or
    written. On a resumed attempt this run's own partial copy is deleted and
    extracted again; an original set aside earlier is never moved twice.
    """
    home = Path.home()
    d = undo_dir(home, args.runid)
    manifest_path = d / "manifest.json"
    if not manifest_path.is_file():
        return cs.fail("Copy the desktop before the plugins.")
    manifest = json.loads(manifest_path.read_text())
    trees = set(manifest.get("created_trees") or [])
    replaced = dict(manifest.get("replaced_trees") or {})
    plugins = home / ".config/omarchy/plugins"
    allow_replace = {p for p in (args.replace or "").split(",") if cs.valid_plugin_id(p)}
    ready: set[str] = set()
    skipped: set[str] = set()

    def prepare(pid: str) -> bool:
        root = plugins / pid
        rel = root.relative_to(home).as_posix()
        if rel in trees:
            aside_rec = replaced.get(rel)
            if aside_rec and not Path(aside_rec).exists() and root.is_dir():
                # Recorded but the move never happened: that is still the
                # original. Move it aside now instead of deleting it.
                Path(aside_rec).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(root), aside_rec)
            elif root.is_dir() and not root.is_symlink():
                shutil.rmtree(root)  # this run's own partial copy
        elif root.exists() or root.is_symlink():
            if pid not in allow_replace or root.is_symlink():
                return False  # the target's own plugin: not ours to replace
            aside = home / ".config/omarchy/plugins-backup" / f"{pid}.pre-clone-{args.runid}"
            if aside.exists():
                return False  # never move onto an existing backup
            replaced[rel] = str(aside)
            trees.add(rel)
            manifest["replaced_trees"] = replaced
            manifest["created_trees"] = sorted(trees)
            save_manifest(manifest_path, manifest)
            aside.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root), str(aside))
            return True
        trees.add(rel)
        manifest["created_trees"] = sorted(trees)
        save_manifest(manifest_path, manifest)
        return True

    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as tar:
        for m in tar:
            parts = Path(m.name).parts
            if not m.isfile() or m.name.startswith("/") or ".." in parts or len(parts) < 2 or not cs.valid_plugin_id(parts[0]):
                continue
            pid = parts[0]
            if pid in skipped:
                continue
            if pid not in ready:
                if not prepare(pid):
                    skipped.add(pid)
                    continue
                ready.add(pid)
            dst = plugins / m.name
            if not inside(home, dst):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), m.mode & 0o755 | 0o600)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(tar.extractfile(m), out)
    return cs.ok({"added": sorted(ready), "skipped": sorted(skipped), "message": f"{len(ready)} plugin(s) copied"})


def cmd_target_undo(args: argparse.Namespace) -> dict[str, Any]:
    """Put this machine back the way it was before the clone.

    Works from the full record, or (if the clone stopped before copying files)
    from the pre-move record. Each phase is recorded as it finishes, so an
    interrupted undo continues instead of moving things twice.
    """
    home = Path.home()
    d = home / UNDO_REL / (args.runid if RUNID_RE.match(args.runid or "") else "-")
    manifest_path = d / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
    elif (d / "pre.json").is_file():
        pre = read_pre(d)
        manifest = {"runid": args.runid, "entries": [], "plugin_previous": pre.get("plugin_previous", ""),
                    "state_previous": pre.get("state_previous", ""), "undone": False}
    else:
        return cs.fail("No record of that clone on this machine; nothing to undo.")
    if manifest.get("undone"):
        return cs.fail("That clone was already undone.")
    runid = manifest["runid"]
    phases = set(manifest.get("undo_phases") or [])

    def finish(name: str) -> None:
        phases.add(name)
        manifest["undo_phases"] = sorted(phases)
        save_manifest(manifest_path, manifest)

    removed = restored = skipped = 0
    if "files" not in phases:
        for e in manifest.get("entries", []):
            dst = home / e["rel"]
            if not inside(home, dst):
                skipped += 1
                continue
            if e.get("kind") in {"absent", "symlink"} and (dst.is_symlink() or dst.is_file()):
                dst.unlink()
                removed += e.get("kind") == "absent"
            if e.get("kind") == "symlink":
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(e["target"], dst)
                restored += 1
        tar_path = d / "files.tar"
        if tar_path.is_file():
            with tarfile.open(tar_path) as tar:
                for m in tar.getmembers():
                    if not m.isfile() or m.name.startswith("/") or ".." in Path(m.name).parts:
                        continue
                    dst = home / m.name
                    if not inside(home, dst):
                        skipped += 1
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if dst.is_symlink():
                        dst.unlink()
                    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    with os.fdopen(fd, "wb") as out, tar.extractfile(m) as src:
                        shutil.copyfileobj(src, out)
                    os.chmod(dst, m.mode & 0o777)
                    restored += 1
        finish("files")

    if "plugins" not in phases:
        # Plugin folders the clone copied in or replaced: remove ours, then
        # put any original back from its backup.
        replaced_map = manifest.get("replaced_trees") or {}
        for rel in manifest.get("created_trees") or []:
            path = home / rel
            if rel in replaced_map and not Path(replaced_map[rel]).is_dir():
                continue  # the move never happened: this is still the original
            if rel.startswith(".config/omarchy/plugins/") and path.is_dir() and not path.is_symlink() and inside(home, path):
                shutil.rmtree(path)
        backups_root = str(home / ".config/omarchy/plugins-backup") + "/"
        for rel, aside in (manifest.get("replaced_trees") or {}).items():
            path = home / rel
            if (rel.startswith(".config/omarchy/plugins/") and str(aside).startswith(backups_root)
                    and Path(aside).is_dir() and not path.exists()):
                shutil.move(aside, str(path))
        for rel in sorted(manifest.get("created_dirs") or [], key=lambda r: r.count("/"), reverse=True):
            path = home / rel
            if path.is_dir() and not path.is_symlink() and inside(home, path):
                try:
                    path.rmdir()
                except OSError:
                    pass
        if "background_previous" in manifest:
            link = home / BACKGROUND_LINK_REL
            if link.is_symlink():
                link.unlink()
            if manifest["background_previous"]:
                os.symlink(manifest["background_previous"], link)
        finish("plugins")

    backups = home / ".config/omarchy/plugins-backup"
    plugin = home / PLUGIN_REL
    def fresh_aside(base: Path) -> Path:
        """A backup path that does not exist yet (never move onto one)."""
        path, n = base, 1
        while path.exists():
            path = base.with_name(f"{base.name}.{n}")
            n += 1
        return path

    if "config-sync" not in phases:
        prev_plugin = manifest.get("plugin_previous") or ""
        if prev_plugin and not prev_plugin.startswith(str(backups / cs.PLUGIN_ID) + "."):
            prev_plugin = ""  # only a path this run created is trusted
        if "config-sync:aside" not in phases:
            if plugin.exists():
                backups.mkdir(parents=True, exist_ok=True)
                shutil.move(str(plugin), str(fresh_aside(backups / f"{cs.PLUGIN_ID}.from-clone-{runid}")))
            finish("config-sync:aside")
        if prev_plugin and Path(prev_plugin).is_dir() and not plugin.exists():
            shutil.move(prev_plugin, str(plugin))
        finish("config-sync")

    if "link" not in phases:
        state_dir = home / STATE_REL
        prev_state = manifest.get("state_previous") or ""
        if prev_state and not prev_state.startswith(str(home / ".local/share" / "omarchy-config-sync.pre-clone.")):
            prev_state = ""
        if "link:aside" not in phases:
            if state_dir.is_dir():
                aside = fresh_aside(home / ".local/share" / f"omarchy-config-sync.from-clone-{runid}")
                for p in list(state_dir.iterdir()):
                    if p.name == "clone-undo":
                        continue
                    aside.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(aside / p.name))
            finish("link:aside")
        if prev_state and Path(prev_state).is_dir():
            for p in Path(prev_state).iterdir():
                if not (state_dir / p.name).exists():
                    shutil.move(str(p), str(state_dir / p.name))
        finish("link")

    manifest["undone"] = True
    manifest["undone_at"] = now_iso()
    save_manifest(manifest_path, manifest)
    return cs.ok({"removed": removed, "restored": restored, "skipped": skipped, "undo_dir": str(d)})


def cmd_target_deploy_key(args: argparse.Namespace) -> dict[str, Any]:
    home = Path.home()
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    key = ssh_dir / "config_sync_deploy_ed25519"
    if not key.exists():
        res = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"config-sync@{os.uname().nodename}",
                              "-f", str(key)], capture_output=True, text=True, timeout=30)
        if res.returncode != 0:
            return cs.fail("ssh-keygen failed: " + res.stderr[-200:])
    known = sys.stdin.read() if args.known_hosts_stdin else ""
    kh = ssh_dir / "known_hosts_config_sync"
    if known.strip():
        kh.write_text(known if known.endswith("\n") else known + "\n")
        kh.chmod(0o600)
    cfg = ssh_dir / "config"
    block = (f"\n# Config Sync (added by the clone wizard)\nHost {GITHUB_ALIAS}\n  HostName github.com\n  User git\n"
             f"  IdentityFile {key}\n  IdentitiesOnly yes\n  HostKeyAlias github.com\n"
             f"  UserKnownHostsFile {kh}\n  StrictHostKeyChecking yes\n")
    text = cfg.read_text() if cfg.is_file() else ""
    if f"Host {GITHUB_ALIAS}" not in text:
        cfg.write_text(text + block)
        cfg.chmod(0o600)
    return cs.ok({"public_key": Path(str(key) + ".pub").read_text().strip(),
                  "host": os.uname().nodename})


def cmd_target_adopt(args: argparse.Namespace) -> dict[str, Any]:
    ctx = target_ctx()
    url = args.url
    cs.cmd_set_url(ctx, parse(["set-url", url]))
    result = {}
    if args.seed:
        result = cs.cmd_resync(ctx, parse(["resync", "--side", "local", "--mirror", "--skip-plugin-launch"]))
    state = cs.load_state(ctx)
    lineage = state.get("lineage") or {}
    lineage["repo"] = url
    state["lineage"] = lineage
    cs.save_state(ctx, state)
    snap = cs.build_snapshot(ctx, fetch=True)
    return cs.ok({"repo_url": url, "sync_state": snap.get("sync_state"),
                  "published": result.get("published") or [], "message": result.get("message", "")})


# --------------------------------------------------------------------------- clone / health / undo / adopt


def require_run(ctx: cs.Context, dest: str, runid: str) -> dict[str, Any]:
    run = load_run(ctx, dest)
    if not run or run.get("runid") != runid or not RUNID_RE.match(runid or ""):
        raise cs.SyncError("Run Preview again: the clone must use the preview you just reviewed.")
    return run


def cmd_clone(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    target = Target(args.target)
    if args.resume:
        # Continue a clone that was confirmed once and stopped part way.
        run = load_run(ctx, target.dest)
        stage_now = str(run.get("stage", ""))
        if not (stage_now.startswith("failed:") or stage_now in {"cloning", "deferred"}):
            raise cs.SyncError("There is no half-finished clone onto that machine to resume.")
        exclude = set(run.get("exclude") or [])
        args.no_wallpapers = bool(run.get("no_wallpapers"))
    else:
        run = require_run(ctx, target.dest, args.runid)
        if (args.confirm or "").strip() != (run.get("target_hostname") or ""):
            raise cs.SyncError(f"Type the new machine's name exactly ({run.get('target_hostname')}) to confirm.")
        exclude = cs.parse_exclude_arg(argparse.Namespace(exclude=args.exclude or ""))
    repo = cs.configured_repo(ctx)
    if cs.git_out(repo, "rev-parse", "HEAD") != run["commit"]:
        raise cs.SyncError("This machine's repo moved since the preview. Preview again so you clone what you reviewed.")
    theirs = target.facts()
    mine = source_facts()
    same = same_machine(mine, theirs)
    if same is None:
        raise cs.SyncError("Could not read a machine id or SSH host key, so this cannot prove it is not cloning onto itself.")
    if same:
        raise cs.SyncError("That address is this machine. Refusing to clone onto itself.")
    if ((theirs.get("host_key") or theirs.get("machine_id")) != run.get("target_identity")
            or theirs.get("hostname") != run.get("target_hostname")):
        raise cs.SyncError("The machine at that address is not the one you previewed.")
    # Compiled programs never travel between machines: build them there.
    exclude |= set(run.get("built_paths") or [])
    remote_dir = run["remote_dir"]
    done = list(run.get("done") or [])
    log: list[dict[str, Any]] = []
    run["stage"] = "cloning"
    run["exclude"] = sorted(exclude)
    run["no_wallpapers"] = bool(args.no_wallpapers)
    save_run(ctx, target.dest, run)

    def stage(name: str, fn) -> dict[str, Any]:
        if name in done:
            log.append({"stage": name, "status": "skipped", "detail": "already done"})
            return run.get(f"result_{name}") or {}
        try:
            res = fn()
        except cs.SyncError as exc:
            res = {"ok": False, "error": str(exc)}
        if not res.get("ok", True):
            log.append({"stage": name, "status": "fail", "detail": res.get("error", "")})
            run["stage"] = f"failed:{name}"
            save_run(ctx, target.dest, run)
            raise cs.SyncError(f"Stopped at '{name}': {res.get('error')}", extra={"log": log, "resumable": True})
        if res.get("defer"):
            run["deferred"] = name
            save_run(ctx, target.dest, run)
            log.append({"stage": name, "status": "deferred", "detail": res.get("message") or ""})
            return res
        run.pop("deferred", None)
        done.append(name)
        run["done"] = done
        run[f"result_{name}"] = res
        save_run(ctx, target.dest, run)
        log.append({"stage": name, "status": "done", "detail": res.get("message") or ""})
        return res

    stage("plugin", lambda: target.py(remote_dir, ["target-install-plugin", "--dir", remote_dir, "--runid", run["runid"]],
                                     what="Installing the plugin"))
    stage("link", lambda: target.py(remote_dir, ["target-link", "--dir", remote_dir, "--runid", run["runid"]],
                                   what="Linking the repo"))
    applied = stage("apply", lambda: target.py(
        remote_dir,
        ["target-apply", "--runid", run["runid"], "--commit", run["commit"], "--source", run.get("source_hostname") or "",
         "--exclude", json.dumps(sorted(exclude))],
        timeout=900, what="Copying the desktop", inhibit=True))

    def wallpapers() -> dict[str, Any]:
        wp = run.get("wallpapers") or {}
        if args.no_wallpapers or not wp.get("files"):
            return {"ok": True, "message": "skipped"}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for rel in wp["files"]:
                src = ctx.home / rel
                if src.is_file() and not src.is_symlink():
                    tar.add(str(src), arcname=rel, recursive=False)
        return target.json(["python3", f"{remote_dir}/plugin/scripts/clone_machine.py", "target-wallpapers",
                            "--runid", run["runid"], "--current", wp.get("current") or ""],
                           input=buf.getvalue(), timeout=900, what="Copying wallpapers")
    stage("wallpapers", wallpapers)

    def private_plugins() -> dict[str, Any]:
        ids = [pid for pid in run.get("copy_plugins") or [] if cs.valid_plugin_id(pid)]
        if not ids:
            return {"ok": True, "message": "none"}
        held: list[str] = []
        git_ok = set(run.get("git_ok") or [])
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for pid in ids:
                root = ctx.config_plugins / pid
                for path in sorted(root.rglob("*")):
                    if path.is_symlink() or not path.is_file():
                        continue
                    in_git = ".git" in path.relative_to(root).parts
                    if in_git and pid not in git_ok:
                        continue  # private or unpushed: working files only, never its history
                    if path.relative_to(root).as_posix() == ".git/config":
                        # Credentials never travel inside a remote URL.
                        text = re.sub(r"(https?://)[^/@\s]+@", r"\1", path.read_text(errors="replace"))
                        data = text.encode()
                        info = tarfile.TarInfo(f"{pid}/.git/config")
                        info.size, info.mode = len(data), 0o644
                        tar.addfile(info, io.BytesIO(data))
                        continue
                    if not in_git and is_compiled(path):
                        held.append(f"{pid}/{path.relative_to(root).as_posix()}")
                        continue
                    tar.add(str(path), arcname=f"{pid}/{path.relative_to(root).as_posix()}", recursive=False)
        res = target.json(["python3", f"{remote_dir}/plugin/scripts/clone_machine.py", "target-local-plugins",
                           "--runid", run["runid"], "--replace", ",".join(run.get("replace_plugins") or [])],
                          input=buf.getvalue(), timeout=1800, what="Copying plugins")
        if held:
            res["message"] = (res.get("message") or "") + f"; held back {len(held)} compiled file(s): " + ", ".join(held[:4])
            run["held_binaries"] = held
        return res
    stage("plugins", private_plugins)

    def restart() -> dict[str, Any]:
        if not (theirs.get("session") and theirs.get("shell_up")):
            if "omarchy/shell.json" in exclude:
                # Its own bar is kept, and Config Sync can only be added to a
                # running bar: finish this after someone logs in (Resume).
                return {"ok": True, "defer": True,
                        "message": "log in on it, then press Resume to add Config Sync to its bar"}
            return {"ok": True, "message": "no desktop session: takes effect at next login"}
        # Hyprland auto-reloads as each file lands, so it may have loaded a
        # half-written config mid-copy. Reload once now that every file is in.
        cmds = [["hyprctl", "reload"], ["timeout", "90", "omarchy", "restart", "shell"]]
        if "omarchy/shell.json" in exclude:
            # Its own bar was kept, so add this plugin to it.
            cmds.insert(0, ["omarchy", "plugin", "enable", cs.PLUGIN_ID, "--section", "right"])
        for argv in cmds:
            proc = target.run(argv, session=True, timeout=120, what="Restarting the desktop shell")
            if proc.returncode != 0:
                return {"ok": False, "error": (" ".join(argv[:4]) + ": " + proc.stderr.decode("utf-8", "replace")[-200:])}
        return {"ok": True, "message": "shell restarted"}
    stage("restart", restart)
    target.run(["rm", "-rf", "--", remote_dir], timeout=60)
    run["stage"] = "deferred" if run.get("deferred") else "cloned"
    run["cloned_at"] = now_iso()
    save_run(ctx, target.dest, run)
    return cs.ok({
        "target": target.dest, "log": log, "applied": len(applied.get("applied") or []),
        "removed": len(applied.get("removed") or []), "backup_dir": applied.get("backup_dir"),
        "undo_dir": applied.get("undo_dir"), "plugin_cmds": run.get("plugin_cmds") or [],
        "notes": [n for n in [target.inhibit_note] if n],
        "message": f"Cloned {run.get('source_hostname')} onto {run.get('target_hostname')}.",
    })


def cmd_health(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    target = Target(args.target)
    run = load_run(ctx, target.dest)
    since = run.get("cloned_at") or ""
    checks = []
    theirs = target.facts()
    live = bool(theirs.get("session"))
    if live:
        checks.append(check("shell", "Desktop shell is running", "pass" if theirs.get("shell_up") else "fail",
                            "the bar process is up" if theirs.get("shell_up") else "quickshell is not running"))
    else:
        checks.append(check("shell", "Desktop shell is running", "warn", "skipped: nobody is logged in to the desktop yet"))
    since_arg = []
    if since:
        try:
            since_arg = ["--since", "@" + str(int(datetime.fromisoformat(since).timestamp()))]
        except ValueError:
            since_arg = []
    proc = target.run(["journalctl", "--user", "--no-pager", "-o", "cat"] + since_arg, timeout=30, what="Reading logs")
    errors = [line for line in proc.stdout.decode("utf-8", "replace").splitlines()
              if re.search(r"TypeError|ReferenceError|is not a type|Cannot assign|Segmentation fault|failed to load", line)]
    by_source: dict[str, int] = {}
    for line in errors:
        m = re.search(r"/plugins/([A-Za-z0-9._-]+)/", line)
        if "/shell/plugins/" in line or "/shell/" in line and not m:
            src = "Omarchy shell"
        else:
            src = m.group(1) if m else "other"
        by_source[src] = by_source.get(src, 0) + 1
    top = sorted(by_source.items(), key=lambda kv: -kv[1])[:4]
    detail = "clean" if not errors else "errors from " + ", ".join(f"{k} ({v})" for k, v in top)
    fix = ("" if not errors else
           "Usually a plugin that needs a newer Omarchy than this machine has, or a service that only exists on "
           "the source machine. Update Omarchy here (omarchy update), or turn that plugin off.")
    checks.append(check("qml", "No QML errors since the clone", "pass" if not errors else "warn", detail, fix))
    if live:
        hyp = target.run(["hyprctl", "configerrors"], session=True, timeout=30, what="Reading Hyprland errors")
        herr = [l for l in hyp.stdout.decode("utf-8", "replace").splitlines() if l.strip()]
        checks.append(check("hypr", "Hyprland config loads cleanly", "pass" if hyp.returncode == 0 and not herr else "fail",
                            "no config errors" if not herr else f"{len(herr)} error line(s): " + herr[0][:160],
                            "\n".join(herr[:8])))
    else:
        checks.append(check("hypr", "Hyprland config loads cleanly", "warn", "skipped: checked at next login"))
    snap = target.json(["python3", f"{PLUGIN_REL}/scripts/config_sync.py", "snapshot"], timeout=120,
                       what="Checking sync state")
    st = snap.get("sync_state") or (snap.get("status") or {}).get("sync_state")
    status = snap.get("status") or {}
    counts = status.get("counts") or {}
    pending_plugins = int(status.get("plugin_list_changes") or 0)
    kept = set(run.get("exclude") or [])
    diff = snap.get("diff") or {}
    changed = [f for f in diff.get("files") or [] if isinstance(f, dict) and f.get("status") not in {"identical", "machine"}]
    own = [f for f in changed if f.get("status") == "added-local"]  # only on this machine: kept by design
    chosen = [f for f in changed if f.get("path") in kept]
    unexpected = [f for f in changed if f not in own and f not in chosen]
    # A plugin list row whose installed commit equals the listed one differs only
    # in how the source URL is written; not a real difference.
    plugin_diffs = [r for r in diff.get("plugin_list") or []
                    if isinstance(r, dict) and r.get("local_version") != r.get("repo_version")]
    notes = []
    if chosen:
        notes.append(f"{len(chosen)} you chose to keep")
    if own:
        notes.append(f"{len(own)} that only exist on this machine (kept)")
    if st == "in-sync":
        detail, level = "everything matches", "pass"
    elif not unexpected and not plugin_diffs:
        detail, level = "matches" + (", except " + " and ".join(notes) if notes else ""), "pass"
    elif st == "remote-ahead" and pending_plugins and int(status.get("repo_changes") or 0) <= pending_plugins:
        detail, level = f"{pending_plugins} plugin(s) still to install: press Install plugins", "warn"
    else:
        detail, level = f"{st or 'unknown'} ({counts.get('changed', 0)} differences)", "warn"
    checks.append(check("sync", "In sync with the cloned repo", level, detail))
    return cs.ok({"target": target.dest, "checks": checks,
                  "healthy": all(c["status"] == "pass" for c in checks)})


def cmd_undo(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    target = Target(args.target)
    run = load_run(ctx, target.dest)
    runid = run.get("runid") or ""
    started = run.get("done") or str(run.get("stage", "")).startswith("failed:") or run.get("stage") in {"cloning", "cloned", "deferred"}
    if not RUNID_RE.match(runid) or not started:
        raise cs.SyncError("There is no clone onto that machine to undo.")
    # Always run the shipped, hash-known copy of this script, not whatever is installed there.
    tmp = f"{REMOTE_BASE}/undo-{uuid.uuid4().hex[:12]}"
    target.run(["bash", "-c", 'umask 077 && mkdir -p -- "$1" && tar -x -C "$1"', "_", tmp], input=runtime_tar(), timeout=120)
    try:
        res = target.json(["python3", f"{tmp}/plugin/scripts/clone_machine.py", "target-undo", "--runid", runid],
                          timeout=300, what="Undoing the clone")
    finally:
        target.run(["rm", "-rf", "--", tmp], timeout=60)
    if not res.get("ok"):
        raise cs.SyncError(res.get("error") or "Undo failed.")
    target.run(["timeout", "90", "omarchy", "restart", "shell"], session=True, timeout=120)
    run["stage"] = "undone"
    run["done"] = []
    run["undone_at"] = now_iso()
    save_run(ctx, target.dest, run)
    return cs.ok({"target": target.dest, "removed": res.get("removed"), "restored": res.get("restored"),
                  "skipped": res.get("skipped"),
                  "message": f"Put back {res.get('restored')} file(s) and removed {res.get('removed')} the clone added."})


def gh_owner_repo(url: str) -> tuple[str, str]:
    m = re.search(r"github\.com[:/]([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$", url or "")
    if not m:
        raise cs.SyncError("This machine's repo is not on GitHub; adopt needs a GitHub repo.")
    return m.group(1), m.group(2)


def gh(args: list[str], timeout: int = 60, input: str | None = None) -> subprocess.CompletedProcess:
    if not shutil.which("gh"):
        raise cs.SyncError("The GitHub CLI (gh) is not installed on this machine: sudo pacman -S github-cli, then gh auth login.")
    return subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout, input=input)


def cmd_adopt(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    target = Target(args.target)
    run = load_run(ctx, target.dest)
    if run.get("stage") != "cloned":
        raise cs.SyncError("Clone the machine first; then it can get its own repo.")
    state = cs.load_state(ctx)
    owner, source_repo = gh_owner_repo(state.get("repo_url", ""))
    if args.mode == "share":
        name = source_repo
    else:
        name = args.repo_name or f"{run.get('target_hostname')}_omarchy_config_sync"
        if not REPO_NAME_RE.match(name):
            raise cs.SyncError("Repo names may use letters, digits, dots, dashes and underscores.")
        view = gh(["repo", "view", f"{owner}/{name}", "--json", "visibility,isEmpty"])
        if view.returncode == 0:
            info = json.loads(view.stdout or "{}")
            if info.get("visibility") != "PRIVATE":
                raise cs.SyncError(f"{owner}/{name} exists and is not private. Pick another name.")
            if not info.get("isEmpty"):
                raise cs.SyncError(f"{owner}/{name} already has commits. Pick another name.")
        else:
            made = gh(["repo", "create", f"{owner}/{name}", "--private",
                       "--description", f"Omarchy config for {run.get('target_hostname')} (Config Sync)"])
            if made.returncode != 0:
                raise cs.SyncError("gh repo create failed: " + made.stderr[-300:])
    # GitHub's host keys as this machine already trusts them, for the target.
    known = subprocess.run(["ssh-keygen", "-F", "github.com"], capture_output=True, text=True).stdout
    known = "\n".join(line for line in known.splitlines() if line and not line.startswith("#"))
    if not known:
        raise cs.SyncError("This machine has no GitHub host key on file. Run once: ssh -T git@github.com")
    remote = f"{REMOTE_BASE}/{uuid.uuid4().hex[:12]}"
    target.run(["bash", "-c", 'umask 077 && mkdir -p -- "$1" && tar -x -C "$1"', "_", remote], input=runtime_tar(), timeout=120)
    try:
        keyres = target.json(["python3", f"{remote}/plugin/scripts/clone_machine.py", "target-deploy-key",
                              "--known-hosts-stdin"], input=(known + "\n").encode(), what="Making a deploy key")
        if not keyres.get("ok"):
            raise cs.SyncError(keyres.get("error") or "Could not make a deploy key.")
        with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as f:
            f.write(keyres["public_key"] + "\n")
            pub = f.name
        try:
            added = gh(["repo", "deploy-key", "add", pub, "-R", f"{owner}/{name}", "--allow-write",
                        "--title", f"config-sync {keyres.get('host')}"])
        finally:
            os.unlink(pub)
        if added.returncode != 0 and "already in use" not in added.stderr:
            raise cs.SyncError("Adding the deploy key failed: " + added.stderr[-300:])
        url = f"git@{GITHUB_ALIAS}:{owner}/{name}.git"
        res = target.json(["python3", f"{remote}/plugin/scripts/clone_machine.py", "target-adopt", "--url", url]
                          + (["--seed"] if args.mode != "share" else []), timeout=600, what="Linking the new repo")
    finally:
        target.run(["rm", "-rf", "--", remote], timeout=60)
    if not res.get("ok"):
        raise cs.SyncError(res.get("error") or "Adopt failed.")
    run["adopted"] = {"mode": args.mode, "repo": f"{owner}/{name}", "at": now_iso()}
    save_run(ctx, target.dest, run)
    return cs.ok({"target": target.dest, "repo": f"https://github.com/{owner}/{name}", "mode": args.mode,
                  "sync_state": res.get("sync_state"),
                  "message": f"{run.get('target_hostname')} now syncs with {owner}/{name} (private)."})


def cmd_machines(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    out = []
    d = clones_dir(ctx)
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            r = cs.load_json(p, default={}, within=ctx.state_dir) or {}
            if not isinstance(r, dict):
                continue
            out.append({k: r.get(k) for k in ("dest", "target_hostname", "source_hostname", "stage", "cloned_at",
                                               "commit", "adopted", "undone_at")})
    state = cs.load_state(ctx) if ctx.state_path.is_file() else {}
    return cs.ok({"self": {"hostname": os.uname().nodename, "repo": state.get("repo_url", ""),
                           "lineage": state.get("lineage") or {}}, "machines": out})


# --------------------------------------------------------------------------- discover

OMARCHY_CHECK = ('if command -v omarchy >/dev/null 2>&1 || [ -d /usr/share/omarchy ] || [ -d "$HOME/.local/share/omarchy" ]; '
                 'then echo omarchy=yes; else echo omarchy=no; fi; '
                 'echo "host=$(cat /proc/sys/kernel/hostname 2>/dev/null)"; '
                 'echo "version=$(pacman -Q omarchy 2>/dev/null | cut -d" " -f2)"; '
                 'echo "user=$(id -un)"')
MAX_CANDIDATES = 128


def tailscale_peers(status: dict[str, Any]) -> list[dict[str, str]]:
    """Online Linux peers from `tailscale status --json`."""
    out = []
    for peer in (status.get("Peer") or {}).values():
        if not isinstance(peer, dict) or not peer.get("Online") or str(peer.get("OS", "")).lower() != "linux":
            continue
        dns = str(peer.get("DNSName") or "").rstrip(".")
        ips = peer.get("TailscaleIPs") or []
        if dns or ips:
            out.append({"name": str(peer.get("HostName") or dns.split(".")[0]), "dest": dns or ips[0],
                        "addr": ips[0] if ips else "", "via": "Tailscale"})
    return out


def ssh_config_hosts(text: str) -> list[str]:
    """Concrete Host aliases (no wildcards, no negations) in an ssh config."""
    hosts = []
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0].lower() == "host":
            for h in parts[1:]:
                if not any(c in h for c in "*?!") and DEST_RE.match(h):
                    hosts.append(h)
    return hosts


def lan_neighbors(text: str) -> list[str]:
    """IPv4 neighbors this machine has already talked to (`ip -4 neigh`)."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if parts and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]) and "FAILED" not in parts and "INCOMPLETE" not in parts:
            out.append(parts[0])
    return out


def mdns_hosts(text: str) -> list[tuple[str, str]]:
    """(hostname.local, ip) for SSH services from `avahi-browse -rtp _ssh._tcp`."""
    out = []
    for line in text.splitlines():
        f = line.split(";")
        if len(f) >= 9 and f[0] == "=" and f[2] == "IPv4" and re.match(r"^\d{1,3}(\.\d{1,3}){3}$", f[7]):
            out.append((f[6], f[7]))
    return out


def platform_node() -> str:
    import platform
    return platform.node()


def run_text(cmd: list[str], timeout: int = 6) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def cmd_discover(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    """Omarchy machines this computer can see, from places it already knows.

    Passive: Tailscale peers, ~/.ssh/config hosts, the LAN neighbor table and
    mDNS. It never sweeps address ranges. Each candidate gets a TCP check on
    port 22 and, only where SSH key login already works, one question: is
    Omarchy installed?
    """
    cands: dict[str, dict[str, Any]] = {}

    def add(dest: str, name: str, via: str, addr: str = "") -> None:
        if not dest or not DEST_RE.match(dest) or len(cands) >= MAX_CANDIDATES:
            return
        key = addr or dest
        if key in cands:
            cands[key]["via"] = sorted(set(cands[key]["via"]) | {via})
            return
        cands[key] = {"dest": dest, "name": name or dest, "addr": addr, "via": [via]}

    own = set(run_text(["hostname", "-I"]).split()) | {"127.0.0.1"}
    status: dict[str, Any] = {}
    if shutil.which("tailscale"):
        try:
            status = json.loads(run_text(["tailscale", "status", "--json"]) or "{}")
        except json.JSONDecodeError:
            status = {}
        own |= set((status.get("Self") or {}).get("TailscaleIPs") or [])
        for peer in tailscale_peers(status):
            add(peer["dest"], peer["name"], "Tailscale", peer["addr"])
    own_names = {platform_node().lower()}
    if shutil.which("tailscale"):
        own_names.add(str((status.get("Self") or {}).get("DNSName") or "").rstrip(".").lower())
    cfg = Path.home() / ".ssh/config"
    if cfg.is_file():
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

        def resolve(alias: str) -> tuple[str, str, str]:
            host, _, _ = ssh_resolve(alias)
            try:
                return alias, host, socket.gethostbyname(host)
            except OSError:
                return alias, host, ""

        aliases = [a for a in ssh_config_hosts(cfg.read_text(errors="replace")) if a.lower() not in own_names]
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(resolve, a) for a in aliases]
            for fut in futures:
                try:
                    alias, host, addr = fut.result(timeout=4)
                except FutureTimeout:
                    continue  # a dead name must not stall the search
                if host.lower() in own_names:
                    continue
                add(alias, alias, "SSH config", addr)
    if shutil.which("avahi-browse"):
        for name, ip in mdns_hosts(run_text(["avahi-browse", "-rtp", "_ssh._tcp"], timeout=5)):
            add(ip, name.removesuffix(".local"), "mDNS", ip)
    for ip in lan_neighbors(run_text(["ip", "-4", "neigh", "show"])):
        add(ip, ip, "Local network", ip)
    for addr in list(cands):
        if addr in own:
            del cands[addr]

    def check(c: dict[str, Any]) -> dict[str, Any]:
        host, port, _ = ssh_resolve(c["dest"])
        if not proxied(c["dest"]):
            try:
                with socket.create_connection((c["addr"] or host, port), timeout=1.5):
                    pass
            except OSError:
                return dict(c, status="unreachable")
        target = Target(c["dest"])
        try:
            proc = target.run(["sh", "-c", OMARCHY_CHECK], timeout=12, what="Checking")
        except cs.SyncError:
            return dict(c, status="unreachable")
        if proc.returncode != 0:
            kind, _hint = classify_ssh_error(proc.stderr.decode("utf-8", "replace"))
            if kind.startswith("hostkey"):
                return dict(c, status="ssh-open", detail="SSH answers; its fingerprint is not trusted here yet")
            if kind == "auth":
                return dict(c, status="ssh-open", detail="SSH answers; this computer's key is not on it yet")
            return dict(c, status="unreachable")  # off, asleep, or not answering
        facts = dict(line.split("=", 1) for line in proc.stdout.decode("utf-8", "replace").splitlines() if "=" in line)
        if facts.get("omarchy") != "yes" or not facts.get("version"):
            return dict(c, status="not-omarchy")  # confirmed only by the installed package
        user = facts.get("user", "")
        dest = c["dest"] if "@" in c["dest"] or not user else f"{user}@{c['dest']}"
        return dict(c, status="omarchy", name=facts.get("host") or c["name"], version=facts.get("version", ""),
                    user=user, dest=dest)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=32) as pool:
        found = list(pool.map(check, cands.values()))
    # Only confirmed Omarchy machines are listed; the rest are just counted.
    shown = sorted((f for f in found if f["status"] == "omarchy"), key=lambda f: f["name"])
    unconfirmed = sum(1 for f in found if f["status"] == "ssh-open")
    return cs.ok({"machines": shown, "checked": len(found), "unconfirmed": unconfirmed,
                  "sources": "Tailscale, ~/.ssh/config, local network neighbors, mDNS (no address sweeps)"})


# --------------------------------------------------------------------------- terminals


def cmd_terminal_status(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    """Has the terminal step finished, and did it succeed?"""
    dest = validate_dest(args.target)
    marker = clones_dir(ctx) / f"{safe_host_key(dest)}.{re.sub(r'[^a-z-]', '', args.kind)}.done"
    if not marker.is_file():
        return cs.ok({"finished": False, "message": "Still running in the terminal. Finish there, then press NEXT."})
    rc = marker.read_text().strip()
    return cs.ok({"finished": True, "rc": rc, "succeeded": rc == "0"})


def cmd_terminal(ctx: cs.Context, args: argparse.Namespace) -> dict[str, Any]:
    dest = validate_dest(args.target)
    q = shlex.quote
    kind = args.kind
    marker = clones_dir(ctx) / f"{safe_host_key(dest)}.{re.sub(r'[^a-z-]', '', kind)}.done"
    clones_dir(ctx).mkdir(parents=True, exist_ok=True)
    marker.unlink(missing_ok=True)
    # The wizard waits for this file: the step's exit code, written when it ends.
    done = (f"rc=$?; echo \"$rc\" > {q(str(marker))}; echo; "
            "echo 'Done. Close this window and press NEXT in Config Sync.'; read -r _")
    if kind == "copy-id":
        cmd = ("echo 'Config Sync: let this computer log in to the new machine with a key.'; "
               "echo 'You will type the NEW machine password once. It is sent only to that machine.'; echo; "
               "[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519; "
               f"ssh-copy-id -i ~/.ssh/id_ed25519.pub -- {q(dest)}; {done}")
    elif kind == "hostkey":
        check_cmd = "for k in $(sudo sshd -T | awk '/^hostkey /{print $2}'); do ssh-keygen -lf $k.pub; done"
        cmd = ("echo 'Config Sync: first-time fingerprint check.'; "
               "echo 'On the NEW machine, run this and note the SHA256 fingerprint:'; "
               f"echo {q('    ' + check_cmd)}; "
               "echo 'Type yes below only if the fingerprints match.'; echo; "
               f"ssh -o StrictHostKeyChecking=ask -- {q(dest)} true && echo 'Fingerprint saved.'; {done}")
    elif kind == "packages":
        run = load_run(ctx, dest)
        parts = []
        if run.get("repo_pkgs"):
            pk = " ".join(q(p) for p in run["repo_pkgs"])
            # Omarchy owns the system: its own installer, and 'omarchy update'
            # (never a raw pacman -Syu) when the package lists are out of date.
            # Update Omarchy first ONLY when the package lists are stale; any
            # other failure (a bad name, a conflict) is shown and stops.
            parts.append(f"out=$(omarchy-pkg-add {pk} 2>&1); rc=$?; echo \"$out\"; "
                         "if [ $rc -ne 0 ] && printf %s \"$out\" | grep -qiE 'failed retrieving file|failed to retrieve|404'; then "
                         "echo 'Package lists are out of date: updating Omarchy first (omarchy update).'; "
                         f"omarchy update && omarchy-pkg-add {pk}; fi")
        if run.get("aur_pkgs"):
            parts.append("omarchy-pkg-aur-add " + " ".join(q(p) for p in run["aur_pkgs"]))
        if not parts:
            raise cs.SyncError("Nothing to install: the new machine has every program the shortcuts use.")
        remote = SESSION_PREFIX + "; ".join(parts)
        cmd = (f"echo {q('Config Sync: installing missing programs on ' + dest)}; "
               "echo 'You type the NEW machine sudo password here. Config Sync never sees or stores it.'; echo; "
               f"echo {q('  ' + remote)}; echo; ssh -t -- {q(dest)} {q(remote)}; {done}")
    elif kind == "services":
        run = load_run(ctx, dest)
        units = [s["unit"] for s in run.get("services") or [] if s.get("kind") == "package"]
        if not units:
            raise cs.SyncError("No package services to enable. Custom services must be copied by hand.")
        remote = "systemctl --user enable --now " + " ".join(q(u) for u in units)
        cmd = (f"echo {q('Config Sync: enabling services on ' + dest)}; echo {q('  ' + remote)}; echo; "
               f"ssh -t -- {q(dest)} {q(remote)}; {done}")
    elif kind == "plugins":
        run = load_run(ctx, dest)
        cmds = run.get("plugin_cmds") or []
        if not cmds:
            raise cs.SyncError("No git plugins to install on the new machine.")
        remote = SESSION_PREFIX + "; ".join(cmds) + "; sleep 1; omarchy restart shell"
        cmd = (f"echo {q(f'Config Sync: installing {len(cmds)} plugin(s) on {dest}. Confirm each one.')}; echo; "
               f"ssh -t -- {q(dest)} {q(remote)}; {done}")
    else:
        raise cs.SyncError("Unknown terminal step.")
    if not cs.launch_omarchy_terminal(cmd):
        raise cs.SyncError("Could not open a terminal. Run this yourself:\n" + cmd)
    return cs.ok({"target": dest, "kind": kind, "message": "Opened a terminal. Finish there, then press Check again."})


# --------------------------------------------------------------------------- CLI


SOURCE_COMMANDS = {
    "probe": cmd_probe, "preview": cmd_preview, "clone": cmd_clone, "health": cmd_health,
    "undo": cmd_undo, "adopt": cmd_adopt, "machines": cmd_machines, "terminal": cmd_terminal,
    "discover": cmd_discover, "terminal-status": cmd_terminal_status,
}
TARGET_COMMANDS = {
    "target-plan": cmd_target_plan, "target-install-plugin": cmd_target_install_plugin,
    "target-link": cmd_target_link, "target-apply": cmd_target_apply, "target-undo": cmd_target_undo,
    "target-wallpapers": cmd_target_wallpapers, "target-local-plugins": cmd_target_local_plugins,
    "target-deploy-key": cmd_target_deploy_key, "target-adopt": cmd_target_adopt,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Clone this Omarchy desktop onto another machine")
    p.add_argument("command", choices=sorted(SOURCE_COMMANDS) + sorted(TARGET_COMMANDS))
    p.add_argument("--target", default="")
    p.add_argument("--runid", default="")
    p.add_argument("--confirm", default="")
    p.add_argument("--exclude", default="")
    p.add_argument("--kind", default="")
    p.add_argument("--mode", default="own", choices=["own", "share"])
    p.add_argument("--repo-name", default="")
    p.add_argument("--dir", default="")
    p.add_argument("--commit", default="")
    p.add_argument("--source", default="")
    p.add_argument("--plugin-previous", default="")
    p.add_argument("--state-previous", default="")
    p.add_argument("--url", default="")
    p.add_argument("--seed", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--no-wallpapers", action="store_true")
    p.add_argument("--current", default="")
    p.add_argument("--plugins", default="exact", choices=["exact", "fresh"])
    p.add_argument("--replace", default="")
    p.add_argument("--known-hosts-stdin", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in TARGET_COMMANDS:
            if args.dir and (args.dir.startswith("/") or ".." in Path(args.dir).parts
                             or not args.dir.startswith(REMOTE_BASE + "/")):
                raise cs.SyncError("Refusing an unexpected working directory.")
            result = TARGET_COMMANDS[args.command](args)
        else:
            if args.command not in {"machines", "discover"} and not args.target:
                raise cs.SyncError("Enter the new machine first.")
            result = SOURCE_COMMANDS[args.command](cs.Context.from_env(), args)
    except cs.SyncError as exc:
        result = cs.fail(str(exc), **exc.extra)
    except Exception as exc:  # noqa: BLE001 — the panel needs JSON, never a traceback
        result = cs.fail(f"{exc.__class__.__name__}: {exc}")
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
