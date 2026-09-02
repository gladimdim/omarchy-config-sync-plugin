#!/usr/bin/env python3
"""Omarchy config-sync backend.

Every command prints a single JSON object to stdout. QML and tests both
speak this interface. Network git operations never prompt; they fail with
a message instead of hanging the panel.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ID = "gladimdim.config-sync"
STATE_VERSION = 1
SESSION_FILE = ".sync-session"
MARKER_NAME = ".omarchy-config.json"
MARKER_FORMAT = "omarchy-config"
MAX_DIFF_LINES = 48
MAX_DIFF_BYTES = 12_000
CLONE_TIMEOUT = 120
FETCH_TIMEOUT = 25
PUSH_TIMEOUT = 60
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_SUBPROCESS_BYTES = 2 * 1024 * 1024
MAX_INVENTORY_FILES = 20_000
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_SYNC_FILE_BYTES = 50 * 1024 * 1024
MAX_URL_INPUT_BYTES = 64 * 1024

BIND_RE = re.compile(
    r"""o\.bind\(\s*"([^"]+)"\s*,\s*(?:nil|"([^"]*)")""",
    re.MULTILINE,
)
UNBIND_RE = re.compile(r"""hl\.unbind\(\s*"([^"]+)"\s*\)""")

SKIP_DIR_NAMES = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules"}
SKIP_FILE_NAMES = {".DS_Store"}
SKIP_NAME_RE = re.compile(r"\.bak(\.|$)")
PROTECTED_PLUGINS = {PLUGIN_ID}  # this plugin is excluded from sync so it does not self-report or overwrite itself
PLUGIN_VERSION = "1.2.15"

FILE_SUMMARIES = {
    "hypr/autostart.lua": "Autostart programs",
    "hypr/bindings.lua": "Keyboard shortcuts",
    "hypr/hyprexpo.lua": "Workspace overview",
    "hypr/hyprland.lua": "Workspaces and window rules",
    "hypr/hyprsunset.conf": "Night light",
    "hypr/input.lua": "Keyboard, mouse, and touchpad",
    "hypr/looknfeel.lua": "Gaps, borders, animations, opacity",
    "hypr/monitors.lua": "Display layout (machine-specific)",
    "hypr/xdph.conf": "Screen share / XDG portal",
    "omarchy/shell.json": "Bar layout, widgets, and idle lock",
    "omarchy/theme.name": "Selected Omarchy theme",
}

MACHINE_LOCAL_PATHS = {"hypr/monitors.lua"}
THEME_REL = "omarchy/theme.name"
THEME_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


class SyncError(Exception):
    def __init__(self, message: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.extra = extra or {}


@dataclass
class Context:
    home: Path
    state_dir: Path
    default_clone: Path
    plugin_root: Path | None = None

    @classmethod
    def from_env(cls) -> "Context":
        home = Path(os.environ.get("HOME") or Path.home()).expanduser()
        data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local/share"))
        state_dir = data / "omarchy-config-sync"
        plugin_root = home / ".config" / "omarchy" / "plugins" / PLUGIN_ID
        if not plugin_root.is_dir():
            plugin_root = None
        return cls(home=home, state_dir=state_dir, default_clone=state_dir / "repo", plugin_root=plugin_root)

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def config_hypr(self) -> Path:
        return self.home / ".config" / "hypr"

    @property
    def config_omarchy(self) -> Path:
        return self.home / ".config" / "omarchy"

    @property
    def config_plugins(self) -> Path:
        return self.config_omarchy / "plugins"

    @property
    def local_bin(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def theme_name_path(self) -> Path:
        return self.home / ".local" / "state" / "omarchy" / "current" / "theme.name"

    @property
    def user_themes(self) -> Path:
        return self.config_omarchy / "themes"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ok(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {"ok": True}
    if payload:
        out.update(payload)
    return out


def fail(message: str, **extra: Any) -> dict[str, Any]:
    out = {"ok": False, "error": message}
    out.update(extra)
    return out


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def file_too_large(path: Path, max_bytes: int | None = None) -> bool:
    """True when a file exceeds the cap. Untrusted repo files feed the text
    parsers and previews; slurping them without a bound is a memory DoS."""
    if max_bytes is None:
        max_bytes = MAX_TEXT_FILE_BYTES
    try:
        return path.is_file() and path.stat().st_size > max_bytes
    except OSError:
        return False


def _open_bound(path: Path | str, max_bytes: int | None = None, within: Path | None = None) -> int | None:
    """Open a file with every trust property bound to the opened inode:
    O_NOFOLLOW refuses a symlink leaf at open time, S_ISREG and the size cap
    run via fstat on the descriptor, and (when `within` is given) the opened
    inode's canonical path is rechecked through /proc/self/fd. Nothing a
    concurrent writer swaps in between a stat and an open can redirect the
    read, inflate it past its cap, or escape containment. Returns None when
    the file is missing, a symlink, non-regular, oversized, or out of tree."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        if max_bytes is not None and st.st_size > max_bytes:
            raise OSError(errno.EFBIG, "exceeds size cap")
        if within is not None:
            proc_link = f"/proc/self/fd/{fd}"
            if not os.path.lexists(proc_link):
                # Containment was requested; without /proc we cannot prove it,
                # so refuse rather than silently skipping the check.
                raise OSError(errno.ENOSYS, "cannot verify containment without /proc")
            actual = Path(os.path.realpath(proc_link))
            if not actual.is_relative_to(within.resolve()):
                raise OSError(errno.EXDEV, "resolves outside the allowed root")
        return fd
    except OSError:
        os.close(fd)
        return None


def _read_fd_capped(fd: int, max_bytes: int) -> bytes | None:
    """Read at most max_bytes from fd; None when more data exists (covers a
    file that grows after the fstat size check)."""
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining > 0:
        try:
            chunk = os.read(fd, min(65536, remaining))
        except OSError:
            return None
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining <= 0:
        return None
    return b"".join(chunks)


def read_bytes_bound(path: Path, max_bytes: int | None = None, within: Path | None = None) -> bytes | None:
    cap = MAX_TEXT_FILE_BYTES if max_bytes is None else max_bytes
    fd = _open_bound(path, cap, within)
    if fd is None:
        return None
    try:
        return _read_fd_capped(fd, cap)
    finally:
        os.close(fd)


def read_text(path: Path, within: Path | None = None) -> str:
    data = read_bytes_bound(path, within=within)
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace")


def _open_dir_bound(root: Path, rel_parent: Path, create: bool = True) -> int:
    """Open root/rel_parent as a directory descriptor whose canonical location
    is verified to sit inside root. The walk is descriptor-relative end to
    end: each component is opened with openat() from the previous descriptor
    and missing ones are created with mkdirat(), so not even directory
    creation traverses a symlinked parent by pathname. A component that is a
    symlink is followed, but the directory it lands on is containment-checked
    on the opened descriptor via /proc/self/fd at every hop — symlinks that
    stay inside root (dotfiles setups) pass, escapes are refused."""
    if any(part in ("..", "") for part in rel_parent.parts):
        raise SyncError(f"Refusing to write through unsafe path: {rel_parent}")
    root_resolved = root.resolve()
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(root), flags)
    except OSError as exc:
        raise SyncError(f"Cannot open destination directory: {root}") from exc

    def verify(fd_: int) -> None:
        proc_link = f"/proc/self/fd/{fd_}"
        if not os.path.lexists(proc_link):
            raise SyncError("Cannot verify write containment without /proc; refusing to write.")
        actual = Path(os.path.realpath(proc_link))
        if not actual.is_relative_to(root_resolved):
            raise SyncError(f"Refusing to write into {root / rel_parent}: it resolves outside {root}")

    try:
        verify(fd)
        for part in rel_parent.parts:
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno != errno.ENOENT or not create:
                    raise SyncError(f"Cannot open destination directory: {root / rel_parent}") from exc
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                except OSError as mk_exc:
                    raise SyncError(f"Cannot create destination directory: {root / rel_parent}") from mk_exc
                try:
                    nxt = os.open(part, flags, dir_fd=fd)
                except OSError as exc2:
                    raise SyncError(f"Cannot open destination directory: {root / rel_parent}") from exc2
            os.close(fd)
            fd = nxt
            verify(fd)
        return fd
    except Exception:
        os.close(fd)
        raise


def _replace_at(dir_fd: int, name: str, mode: int, write_body) -> None:
    """Create an exclusive temp file relative to an already-verified directory
    descriptor, let write_body(fd) fill it, then atomically rename it over
    `name` inside that same directory. rename never follows a symlink target,
    so a planted link named `name` is replaced, not traversed."""
    tmp_name = f".{name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
    open_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    tmp_fd = os.open(tmp_name, open_flags, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(tmp_fd, mode)
        write_body(tmp_fd)
        os.fsync(tmp_fd)
    except Exception:
        os.close(tmp_fd)
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise
    os.close(tmp_fd)
    try:
        os.replace(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except Exception:
        try:
            os.unlink(tmp_name, dir_fd=dir_fd)
        except OSError:
            pass
        raise


def _write_within(root: Path, dst: Path, mode: int, write_body) -> None:
    """Write dst atomically with its parent directory bound to root: the
    directory is opened and containment-verified first, and both the temp
    file and the final rename are dir_fd-relative to that descriptor."""
    try:
        rel = dst.relative_to(root)
    except ValueError as exc:
        raise SyncError(f"Refusing to write {dst}: outside {root}") from exc
    dir_fd = _open_dir_bound(root, rel.parent)
    try:
        _replace_at(dir_fd, rel.name, mode, write_body)
    finally:
        os.close(dir_fd)


def atomic_write_text(path: Path, content: str, mode: int = 0o600, within: Path | None = None) -> None:
    data = content.encode("utf-8")
    if within is not None:
        def body(fd: int) -> None:
            os.write(fd, data)
        _write_within(within, path, mode, body)
        return
    ensure_parent(path)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        try:
            os.chmod(tmp.fileno(), mode)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        except Exception:
            tmp.close()
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
    os.replace(tmp_path, path)


def write_json(path: Path, data: Any, within: Path | None = None) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(path, content, mode=0o600, within=within)


def load_json(path: Path, default: Any = None, within: Path | None = None) -> Any:
    if not path.is_file() or path.is_symlink() or os.path.islink(path):
        return default
    data = read_bytes_bound(path, within=within)
    if data is None:
        return default
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, within: Path | None = None) -> str | None:
    fd = _open_bound(path, MAX_SYNC_FILE_BYTES, within)
    if fd is None:
        return None
    h = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(fd, 1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_SYNC_FILE_BYTES:
                return None
            h.update(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return h.hexdigest()


def canonical_shell_bytes(path: Path, within: Path | None = None) -> bytes | None:
    """Hash shell.json without this plugin's bar entry.

    Apply restores the tray widget after copying, so a naive hash would
    always look dirty. Stripping our own id keeps real bar edits visible.
    """
    if not path.is_file() or file_too_large(path):
        return None
    data = load_json(path, default=None, within=within)
    if not isinstance(data, dict):
        return read_bytes_bound(path, within=within)
    bar = data.get("bar")
    if isinstance(bar, dict):
        layout = bar.get("layout")
        if isinstance(layout, dict):
            for section, entries in list(layout.items()):
                if not isinstance(entries, list):
                    continue
                layout[section] = [
                    entry
                    for entry in entries
                    if (entry.get("id") if isinstance(entry, dict) else entry) != PLUGIN_ID
                ]
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def file_hash(path: Path, rel: str, within: Path | None = None) -> str | None:
    if rel == "omarchy/shell.json":
        data = canonical_shell_bytes(path, within=within)
        return sha256_bytes(data) if data is not None else None
    return sha256_file(path, within=within)


def is_skipped_file(name: str) -> bool:
    return name in SKIP_FILE_NAMES or bool(SKIP_NAME_RE.search(name))


def validate_safe_rel_path(rel: str) -> bool:
    if not rel or not isinstance(rel, str):
        return False
    rel = rel.strip()
    if not rel or rel.startswith("/") or rel.startswith("\\") or rel.startswith("-"):
        return False
    parts = Path(rel).parts
    if ".." in parts or "." in parts or "~" in parts:
        return False
    return not any(":" in p or "\0" in p or "\n" in p for p in parts)


def normalize_source(raw: str) -> tuple[str, str]:
    src = (raw or "").strip()
    if not src or src.startswith("-") or "\0" in src or "\n" in src:
        raise SyncError("Paste a git URL or a local path to your Omarchy config repo.")
    if src.startswith("git@") or src.startswith("ssh://") or src.startswith("file://"):
        return "url", src
    if src.startswith("http://") or src.startswith("https://"):
        return "url", src
    if src.startswith("github.com/") or src.startswith("gitlab.com/") or src.startswith("codeberg.org/"):
        return "url", "https://" + src
    if re.fullmatch(r"[\w.-]+/[\w.-]+", src) and not src.startswith("-"):
        return "url", f"https://github.com/{src}.git"
    path = Path(os.path.expanduser(src)).resolve()
    if path.exists() and not str(path).startswith("-"):
        return "path", str(path)
    raise SyncError(
        f"Not a local path, and not a git URL: {sanitize_url(src)}. "
        "Use https://github.com/you/omarchy-config.git or ~/Github/omarchy-config."
    )


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = env.get("GIT_ASKPASS") or "true"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return env


def _drain_capped(pipe: Any, sink: list[bytes], max_bytes: int, overflow: threading.Event) -> None:
    """Read a child pipe to EOF, keeping at most max_bytes and flagging overflow.

    Draining continues past the cap (discarding) so the child never blocks on a
    full pipe; the overflow flag lets the parent kill the process group instead."""
    kept = 0
    total = 0
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                return
            total += len(chunk)
            if kept < max_bytes:
                take = chunk[: max_bytes - kept]
                sink.append(take)
                kept += len(take)
            if total > max_bytes:
                overflow.set()
    except (OSError, ValueError):
        pass


def run_bounded(
    cmd: list[str],
    *,
    input: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    timeout: float = 30,
    max_bytes: int = MAX_SUBPROCESS_BYTES,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run(capture_output=True) with the byte cap enforced WHILE the
    child (git on an untrusted remote, hyprctl, omarchy-shell) is running, not
    after it exits: output streams through capped drain threads, nothing is
    spooled to disk, and a child that exceeds its output budget has its whole
    process group killed immediately instead of running until the timeout."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )

    def kill_group() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()

    overflow = threading.Event()
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    readers = [
        threading.Thread(target=_drain_capped, args=(proc.stdout, out_chunks, max_bytes, overflow), daemon=True),
        threading.Thread(target=_drain_capped, args=(proc.stderr, err_chunks, max_bytes, overflow), daemon=True),
    ]
    for t in readers:
        t.start()
    if input is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    deadline = time.monotonic() + timeout
    truncated = False
    while True:
        try:
            proc.wait(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            pass
        if overflow.is_set():
            truncated = True
            kill_group()
            proc.wait()
            break
        if time.monotonic() >= deadline:
            kill_group()
            proc.wait()
            for t in readers:
                t.join(timeout=2)
            for pipe in (proc.stdout, proc.stderr, proc.stdin):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass
            raise subprocess.TimeoutExpired(cmd, timeout)
    for t in readers:
        t.join(timeout=5)
    for pipe in (proc.stdout, proc.stderr, proc.stdin):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:
            pass
    out = b"".join(out_chunks).decode("utf-8", errors="replace")
    err = b"".join(err_chunks).decode("utf-8", errors="replace")
    if truncated:
        err = (err + "\n[output truncated: process exceeded its output limit and was stopped]").strip()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_git(
    repo: Path | None,
    args: list[str],
    timeout: int = 30,
    check: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    cmd += args
    try:
        result = run_bounded(
            cmd,
            timeout=timeout,
            env=git_env(),
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and result.returncode != 0:
        raise SyncError(git_error_message(args, result))
    return result


_URL_CREDENTIALS_RE = re.compile(r"^(https?://)([^/@]+)@(.+)$")
_CRLF_NUL_RE = re.compile(r"[\r\n\x00]")


def _write_credential_store(path: Path, protocol: str, host: str, username: str, secret: str) -> None:
    """Write the git-credential-store file ourselves through a verified
    descriptor: the secret never enters a subprocess, O_NOFOLLOW blocks a
    raced-in symlink, O_NONBLOCK keeps a raced-in FIFO from blocking the open,
    and the S_ISREG check runs on the opened descriptor itself (fstat), so no
    pathname race between a stat and the open can redirect the write."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(path), flags, 0o600)
    except OSError as exc:
        raise SyncError("Refusing to write git credentials: unsafe credential store path.") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SyncError("Refusing to write git credentials: credential store is not a regular file.")
        os.fchmod(fd, 0o600)
        line = (
            f"{protocol}://{urllib.parse.quote(username, safe='')}:"
            f"{urllib.parse.quote(secret, safe='')}@{host}\n"
        )
        os.ftruncate(fd, 0)
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare_git_credentials(ctx: Context, url: str) -> tuple[str, Path | None]:
    """Strip HTTP(S) credentials embedded in a git URL before it ever becomes a
    git child-process argument. The credential is stashed in a 0600 git
    credential-store file instead, so git authenticates via credential.helper
    rather than argv, .git/config, or our own state/UI carrying the secret."""
    match = _URL_CREDENTIALS_RE.match(url)
    if not match:
        return url, None
    scheme, userinfo, rest = match.groups()
    username, _, secret = userinfo.partition(":")
    username = urllib.parse.unquote(username)
    secret = urllib.parse.unquote(secret) if secret else "x-oauth-basic"
    host = rest.split("/", 1)[0]
    protocol = scheme.split("://", 1)[0]
    # Percent-decoding can smuggle CR/LF into the line-based store format and
    # git's credential protocol; reject it rather than let it inject fields.
    if any(_CRLF_NUL_RE.search(v) for v in (username, secret, host)):
        raise SyncError("Git URL contains invalid characters in its credentials.")
    clean_url = f"{scheme}{rest}"
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    cred_file = ctx.state_dir / ".git-credentials"
    _write_credential_store(cred_file, protocol, host, username, secret)
    return clean_url, cred_file


def _cred_helper_value(cred_file: Path) -> str:
    """credential.helper value with the store path single-quoted for the shell
    git uses to split helper commands, so a space or metacharacter in
    HOME/XDG_DATA_HOME cannot break or extend the helper command line."""
    p = str(cred_file)
    if _CRLF_NUL_RE.search(p):
        raise SyncError("Unsafe characters in the credential store path.")
    return "store --file=" + "'" + p.replace("'", "'\\''") + "'"


def git_cred_config_args(cred_file: Path | None) -> list[str]:
    if not cred_file:
        return []
    return ["-c", f"credential.helper={_cred_helper_value(cred_file)}"]


def persist_git_credential_helper(repo: Path, cred_file: Path | None) -> None:
    if not cred_file:
        return
    run_git(repo, ["config", "credential.helper", _cred_helper_value(cred_file)], check=False)


def sanitize_url(text: str) -> str:
    """Mask tokens or passwords embedded in git URLs (e.g. https://token@github.com)."""
    return re.sub(r"://([^/@:]+):([^/@]+)@", "://***:***@", re.sub(r"://([^/@:]+)@", "://***@", text))


def git_error_message(args: list[str], result: subprocess.CompletedProcess[str]) -> str:
    err = (result.stderr or result.stdout or "").strip()
    err = re.sub(r"\s+", " ", err)
    err = sanitize_url(err)
    if "Permission denied" in err or "Could not read from remote" in err:
        return "Git could not authenticate with the remote. Set up SSH keys or a credential helper, then try again."
    if "Repository not found" in err or "not found" in err.lower():
        return "Remote repository was not found. Check the URL and that this machine can access it."
    if "Authentication failed" in err or "could not read Username" in err:
        return "Git asked for a username/password and we refused so the panel would not hang. Use SSH or a stored credential."
    if not err:
        err = f"git {' '.join(args)} failed with exit {result.returncode}"
    if len(err) > 400:
        err = err[:397] + "..."
    return err


def git_out(repo: Path, *args: str, timeout: int = 20) -> str:
    result = run_git(repo, list(args), timeout=timeout)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def load_state(ctx: Context) -> dict[str, Any]:
    bind_install_session(ctx)
    data = load_json(ctx.state_path, default={}, within=ctx.state_dir)
    if not isinstance(data, dict):
        return {}
    return data


def save_state(ctx: Context, state: dict[str, Any]) -> None:
    state["version"] = STATE_VERSION
    session = read_or_create_session(ctx.plugin_root) if ctx.plugin_root is not None else None
    if session:
        state["plugin_instance"] = session
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    write_json(ctx.state_path, state, within=ctx.state_dir)


def read_or_create_session(plugin_root: Path | None) -> str | None:
    if plugin_root is None:
        return None
    path = plugin_root / SESSION_FILE
    value = read_text(path, within=plugin_root).strip()
    if value:
        return value
    value = uuid.uuid4().hex
    try:
        atomic_write_text(path, value + "\n", mode=0o600, within=plugin_root)
    except (OSError, SyncError):
        return None
    return value


def purge_saved_settings(ctx: Context) -> bool:
    """Forget linked-repo settings. Never deletes a user clone outside state_dir."""
    deleted_clone = False
    state: dict[str, Any] = {}
    if ctx.state_path.is_file():
        loaded = load_json(ctx.state_path, default={}, within=ctx.state_dir)
        if isinstance(loaded, dict):
            state = loaded
    clone = Path(state.get("clone_path") or "")
    using_existing = bool(state.get("using_existing_clone"))
    if clone.is_dir() and not using_existing:
        try:
            rel = clone.resolve().relative_to(ctx.state_dir.resolve())
        except (ValueError, OSError):
            rel = None
        if rel is not None and rel.parts:
            # Delete relative to a verified descriptor for state_dir so the
            # containment-checked path cannot be swapped for a symlink between
            # the check and the rmtree; the rmtree descends dir_fd-relative
            # and refuses a symlink at the top.
            try:
                state_fd = _open_dir_bound(ctx.state_dir, Path(), create=False)
            except SyncError:
                state_fd = None
            if state_fd is not None:
                try:
                    shutil.rmtree(str(rel), ignore_errors=True, dir_fd=state_fd)
                finally:
                    os.close(state_fd)
                deleted_clone = not clone.is_dir()
    if ctx.state_dir.is_dir():
        # rmtree refuses a symlinked root itself, and state_dir is a fixed
        # plugin-owned path, so a plain pathname delete is safe here.
        shutil.rmtree(ctx.state_dir, ignore_errors=True)
    return deleted_clone


def bind_install_session(ctx: Context) -> None:
    """Drop leftover XDG settings when this plugin folder is a new install.

    Omarchy's plugin remove only deletes ~/.config/omarchy/plugins/<id>/.
    Linked-repo state lives in XDG, so a reinstall would otherwise reconnect
    automatically. A session file in the plugin folder dies with the plugin;
    a new folder gets a new id and leftover settings are purged.
    """
    if ctx.plugin_root is None or not ctx.plugin_root.is_dir():
        return
    if not ctx.state_path.is_file():
        return
    session = read_or_create_session(ctx.plugin_root)
    if not session:
        return
    data = load_json(ctx.state_path, default={}, within=ctx.state_dir)
    if not isinstance(data, dict) or not data:
        return
    stored = str(data.get("plugin_instance") or "")
    if stored == session:
        return
    if stored:
        purge_saved_settings(ctx)
        return
    data["plugin_instance"] = session
    try:
        write_json(ctx.state_path, data, within=ctx.state_dir)
    except (OSError, SyncError):
        return


def configured_repo(ctx: Context, state: dict[str, Any] | None = None) -> Path:
    state = state if state is not None else load_state(ctx)
    raw = state.get("clone_path") or ""
    if not raw:
        raise SyncError("No config repo is linked yet. Paste a git URL to get started.")
    path = Path(raw)
    if not path.is_dir():
        raise SyncError(f"Linked repo is missing on disk: {path}")
    return path


def validate_repo(path: Path) -> dict[str, Any]:
    reasons: list[str] = []
    score = 0
    if not path.is_dir():
        return {"valid": False, "score": 0, "reasons": [], "error": f"Not a directory: {path}"}

    marker = path / MARKER_NAME
    if marker.is_file():
        marker_data = load_json(marker, default={}, within=path)
        if isinstance(marker_data, dict) and marker_data.get("format") == MARKER_FORMAT:
            score += 5
            reasons.append("Omarchy config marker")
        else:
            score += 1
            reasons.append("config marker file")

    hypr = path / "hypr"
    hypr_files = []
    if hypr.is_dir():
        hypr_files = [p.name for p in hypr.iterdir() if p.is_file() and p.suffix in {".lua", ".conf"}]
        if hypr_files:
            score += 2
            reasons.append(f"{len(hypr_files)} Hyprland config files")

    shell = path / "omarchy" / "shell.json"
    if shell.is_file():
        score += 2
        reasons.append("Omarchy shell.json")

    plugins_dir = path / "plugins"
    plugin_ids = []
    if plugins_dir.is_dir():
        for child in sorted(plugins_dir.iterdir()):
            if child.is_dir() and (child / "manifest.json").is_file():
                plugin_ids.append(child.name)
        if plugin_ids:
            score += 2
            reasons.append(f"{len(plugin_ids)} shell plugins")

    if (path / "apply.sh").is_file() or (path / "sync.sh").is_file():
        score += 1
        reasons.append("apply/sync scripts")

    if (path / "terminals").is_dir() and any((path / "terminals").iterdir()):
        score += 1
        reasons.append("terminal configs")

    has_core = bool(hypr_files) and (shell.is_file() or bool(plugin_ids) or (path / "apply.sh").is_file())
    valid = score >= 3 and (has_core or any(r == "Omarchy config marker" for r in reasons))
    if not hypr_files and not shell.is_file() and not plugin_ids:
        valid = any(r == "Omarchy config marker" for r in reasons) and score >= 5

    return {
        "valid": valid,
        "score": score,
        "reasons": reasons,
        "hypr_files": hypr_files,
        "plugin_ids": plugin_ids,
        "has_shell": shell.is_file(),
        "empty": False,
    }


STARTER_FILE_NAMES = {
    "readme",
    "readme.md",
    "readme.txt",
    "license",
    "license.md",
    "licence",
    "licence.md",
    "copying",
    "copying.md",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    "code_of_conduct.md",
    "security.md",
    "contributing.md",
    "authors",
    "changelog.md",
}

STARTER_TOP_DIRS = {".github", ".git", "docs"}
PROJECT_MARKERS = {
    "hypr",
    "omarchy",
    "plugins",
    "apply.sh",
    "sync.sh",
    "src",
    "lib",
    "app",
    "package.json",
    "cargo.toml",
    "pyproject.toml",
    "go.mod",
    "makefile",
    "cmakelists.txt",
}


def is_seedable_empty(path: Path) -> bool:
    """True for a brand-new GitHub repo: empty, or only README/LICENSE/.gitignore."""
    if not path.is_dir():
        return False
    if validate_repo(path).get("valid"):
        return False
    for child in path.iterdir():
        name = child.name
        if name in {".git", ".github"}:
            continue
        if name.lower() in PROJECT_MARKERS:
            return False
        if child.is_dir() and name.lower() not in STARTER_TOP_DIRS:
            return False
        if child.is_file() and name.lower() not in STARTER_FILE_NAMES:
            return False
    for file_path in iter_files(path):
        rel = rel_posix(file_path, path).lower()
        if rel.startswith(".github/"):
            continue
        if Path(rel).name.lower() in STARTER_FILE_NAMES:
            continue
        return False
    return True


def write_marker(repo: Path) -> None:
    marker_path = repo / MARKER_NAME
    existing = load_json(marker_path, default={}, within=repo)
    data = existing if isinstance(existing, dict) else {}
    data.update({"format": MARKER_FORMAT, "version": 1, "synced_by": PLUGIN_ID})
    write_json(marker_path, data, within=repo)


def summary_for(rel: str) -> str:
    if rel in FILE_SUMMARIES:
        return FILE_SUMMARIES[rel]
    if rel.startswith("plugins/"):
        name = rel.split("/")[1] if "/" in rel else rel
        return f"Plugin {name}"
    if rel == THEME_REL:
        return "Selected Omarchy theme"
    if rel.startswith("omarchy/themes/"):
        parts = rel.split("/")
        slug = parts[2] if len(parts) > 2 else ""
        return f"Custom theme files ({theme_display_name(slug)})" if slug else "Custom theme files"
    if rel.startswith("omarchy/hooks/"):
        return "Automation hook"
    if rel.startswith("omarchy/agents/"):
        return "Agent helper"
    if rel.startswith("omarchy/branding/"):
        return "Branding text"
    if rel.startswith("omarchy/extensions/"):
        return "Menu extension"
    if rel.startswith("terminals/"):
        return "Terminal config"
    if rel.startswith("bin/"):
        return "Helper script"
    if rel.startswith("hypr/"):
        return "Hyprland config"
    if rel.startswith("omarchy/"):
        return "Omarchy setting"
    return rel


def is_machine_local(rel: str) -> bool:
    return rel in MACHINE_LOCAL_PATHS


def is_bundled_path(rel: str) -> bool:
    """True for plugin/hook/agent/helper trees that carry executable code or
    agent instructions and must never be auto-selected for Apply."""
    return (
        rel.startswith("plugins/")
        or rel.startswith("omarchy/hooks/")
        or rel.startswith("omarchy/agents/")
        or rel.startswith("omarchy/branding/")
        or rel.startswith("omarchy/extensions/")
        or rel.startswith("bin/")
    )


def is_executable_payload(rel: str) -> bool:
    """Files that would land as (or be treated as) runnable code."""
    return rel.endswith((".sh", ".py", ".hook"))


def is_hidden_item(kind: str, item_id: str, hidden_keys: set[str]) -> bool:
    if not hidden_keys:
        return False
    if f"{kind}:{item_id}" in hidden_keys or item_id in hidden_keys:
        return True
    if kind == "f":
        parts = item_id.split("/")
        if item_id.startswith("plugins/") and len(parts) >= 2:
            pid = parts[1]
            if f"g:plugin:{pid}" in hidden_keys or f"p:{pid}" in hidden_keys or f"plugin:{pid}" in hidden_keys:
                return True
        elif item_id.startswith("omarchy/hooks/") and len(parts) >= 3:
            event = parts[2].replace(".d", "")
            if f"g:hooks:{event}" in hidden_keys or f"hooks:{event}" in hidden_keys:
                return True
        elif item_id.startswith("omarchy/agents/"):
            if "g:agents" in hidden_keys or "agents" in hidden_keys:
                return True
        elif item_id.startswith("omarchy/branding/"):
            if "g:branding" in hidden_keys or "branding" in hidden_keys:
                return True
        elif item_id.startswith("omarchy/extensions/"):
            if "g:extensions" in hidden_keys or "extensions" in hidden_keys:
                return True
        elif item_id.startswith("bin/"):
            if "g:bin" in hidden_keys or "bin" in hidden_keys:
                return True
        elif item_id == THEME_REL or item_id.startswith("omarchy/themes/"):
            if "t:selected" in hidden_keys or "t:theme" in hidden_keys or "theme" in hidden_keys:
                return True
    elif kind == "g":
        if item_id.startswith("plugin:"):
            pid = item_id[7:]
            if f"p:{pid}" in hidden_keys:
                return True
    elif kind == "p":
        if f"g:plugin:{item_id}" in hidden_keys or f"plugin:{item_id}" in hidden_keys:
            return True
    return False


def iter_files(root: Path) -> list[Path]:
    if root.is_symlink() or os.path.islink(root):
        # os.walk(top) descends into a symlinked top directory even with
        # followlinks=False; refuse it so a repo cannot alias a config dir
        # to somewhere else (e.g. omarchy/hooks -> ~/.ssh).
        return []
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIR_NAMES
            and not d.startswith(".")
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        for name in filenames:
            if name.startswith(".") and name not in {MARKER_NAME}:
                continue
            if is_skipped_file(name):
                continue
            fpath = Path(dirpath) / name
            if fpath.is_symlink() or os.path.islink(fpath):
                continue
            out.append(fpath)
    return out


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def read_theme_slug(path: Path, within: Path | None = None) -> str:
    if not path.is_file():
        return ""
    for line in read_text(path, within=within).splitlines():
        slug = line.strip().lower().replace(" ", "-")
        if slug and not slug.startswith("#"):
            return slug
    return ""


def theme_display_name(slug: str) -> str:
    if not slug:
        return ""
    return " ".join(part[:1].upper() + part[1:] for part in slug.replace("_", "-").split("-") if part)


def is_theme_asset(path: Path) -> bool:
    return path.suffix.lower() in THEME_SKIP_SUFFIXES


def iter_theme_files(root: Path) -> list[Path]:
    out = []
    for path in iter_files(root):
        if is_theme_asset(path):
            continue
        out.append(path)
    return out


def terminal_map(ctx: Context) -> dict[str, Path]:
    return {
        "terminals/alacritty.toml": ctx.home / ".config" / "alacritty" / "alacritty.toml",
        "terminals/ghostty.config": ctx.home / ".config" / "ghostty" / "config",
        "terminals/kitty.conf": ctx.home / ".config" / "kitty" / "kitty.conf",
        "terminals/foot.ini": ctx.home / ".config" / "foot" / "foot.ini",
    }


def collect_inventory(ctx: Context, repo: Path) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    repo_resolved = repo.resolve()
    home_resolved = ctx.home.resolve()

    def add(rel: str, local: Path, repo_file: Path, group: str, extra: dict[str, Any] | None = None) -> None:
        if not validate_safe_rel_path(rel):
            return
        if rel in items:
            return
        if len(items) >= MAX_INVENTORY_FILES:
            # Bound memory during collection itself, not just the final JSON
            # size: a malicious/huge linked repo could otherwise balloon the
            # in-memory inventory long before any response-size check runs.
            raise SyncError(
                f"Linked repo has more than {MAX_INVENTORY_FILES} tracked files; "
                "refusing to build a diff to avoid unbounded memory use."
            )
        try:
            # An intermediate directory symlink could alias a tracked path to
            # somewhere outside the clone or the home tree; refuse the item.
            if not repo_file.resolve().is_relative_to(repo_resolved):
                return
            if not local.resolve().is_relative_to(home_resolved):
                return
        except OSError:
            return
        if file_too_large(local, MAX_SYNC_FILE_BYTES) or file_too_large(repo_file, MAX_SYNC_FILE_BYTES):
            # Not config material; skip rather than hash/copy something huge.
            return
        local_regular = local.is_file() and not local.is_symlink() and not os.path.islink(local)
        repo_regular = repo_file.is_file() and not repo_file.is_symlink() and not os.path.islink(repo_file)
        items[rel] = {
            "path": rel,
            "group": group,
            "summary": summary_for(rel),
            "portable": not is_machine_local(rel),
            "local_path": str(local),
            "repo_path": str(repo_file),
            "local_exists": local_regular,
            "repo_exists": repo_regular,
            "local_hash": file_hash(local, rel, within=ctx.home) if local_regular else None,
            "repo_hash": file_hash(repo_file, rel, within=repo) if repo_regular else None,
            "git_managed": False,
        }
        if extra:
            items[rel].update(extra)

    hypr_names: set[str] = set()
    repo_hypr = repo / "hypr"
    if repo_hypr.is_dir():
        for p in repo_hypr.iterdir():
            if p.is_file() and p.suffix in {".lua", ".conf"} and not is_skipped_file(p.name):
                hypr_names.add(p.name)
    local_hypr = ctx.config_hypr
    if local_hypr.is_dir():
        for p in local_hypr.iterdir():
            if p.is_file() and p.suffix in {".lua", ".conf"} and not is_skipped_file(p.name):
                hypr_names.add(p.name)
    for name in sorted(hypr_names):
        add(f"hypr/{name}", local_hypr / name, repo_hypr / name, "hypr")

    omarchy_roots = ["branding", "extensions", "hooks", "agents"]
    for sub in omarchy_roots:
        repo_sub = repo / "omarchy" / sub
        local_sub = ctx.config_omarchy / sub
        rels: set[str] = set()
        for p in iter_files(repo_sub):
            rels.add(rel_posix(p, repo))
        for p in iter_files(local_sub):
            rels.add(f"omarchy/{sub}/" + rel_posix(p, local_sub))
        for rel in sorted(rels):
            add(rel, ctx.home / ".config" / rel, repo / rel, "omarchy")

    repo_shell = repo / "omarchy" / "shell.json"
    local_shell = ctx.config_omarchy / "shell.json"
    if repo_shell.is_file() or local_shell.is_file():
        add("omarchy/shell.json", local_shell, repo_shell, "omarchy")

    plugin_ids: set[str] = set()
    repo_plugins = repo / "plugins"
    if repo_plugins.is_dir():
        plugin_ids.update(
            p.name
            for p in repo_plugins.iterdir()
            if p.is_dir() and not p.is_symlink() and not p.name.startswith(".") and p.name != PLUGIN_ID
        )
    if ctx.config_plugins.is_dir():
        plugin_ids.update(
            p.name
            for p in ctx.config_plugins.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != PLUGIN_ID
        )
    for plugin_id in sorted(plugin_ids):
        if plugin_id == PLUGIN_ID:
            continue
        repo_plugin = repo_plugins / plugin_id
        local_plugin = ctx.config_plugins / plugin_id
        rels = set()
        for p in iter_files(repo_plugin):
            rels.add(rel_posix(p, repo))
        for p in iter_files(local_plugin):
            rels.add(f"plugins/{plugin_id}/" + rel_posix(p, local_plugin))
        git_managed = (local_plugin / ".git").exists()
        for rel in sorted(rels):
            add(
                rel,
                ctx.home / ".config" / "omarchy" / rel,
                repo / rel,
                "plugin",
                extra={"git_managed": git_managed},
            )

    for rel, local in terminal_map(ctx).items():
        repo_file = repo / rel
        if local.is_file() or repo_file.is_file():
            add(rel, local, repo_file, "terminal")

    bin_names: set[str] = set()
    repo_bin = repo / "bin"
    if repo_bin.is_dir():
        bin_names.update(p.name for p in repo_bin.iterdir() if p.is_file() and not is_skipped_file(p.name))
    for name in sorted(bin_names):
        add(f"bin/{name}", ctx.local_bin / name, repo_bin / name, "bin")

    local_theme = ctx.theme_name_path
    repo_theme = repo / THEME_REL
    if local_theme.is_file() or repo_theme.is_file():
        add(THEME_REL, local_theme, repo_theme, "theme")

    slugs: set[str] = set()
    for slug in (read_theme_slug(local_theme, within=ctx.home), read_theme_slug(repo_theme, within=repo)):
        if slug:
            slugs.add(slug)
    repo_themes = repo / "omarchy" / "themes"
    if repo_themes.is_dir():
        slugs.update(p.name for p in repo_themes.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    for slug in sorted(slugs):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug or ""):
            continue
        repo_overlay = repo_themes / slug
        local_overlay = ctx.user_themes / slug
        rels: set[str] = set()
        for p in iter_theme_files(repo_overlay):
            rels.add(rel_posix(p, repo))
        for p in iter_theme_files(local_overlay):
            rels.add(f"omarchy/themes/{slug}/" + rel_posix(p, local_overlay))
        for rel in sorted(rels):
            add(rel, ctx.home / ".config" / rel, repo / rel, "theme")

    return [items[k] for k in sorted(items)]


def classify_file(item: dict[str, Any], stored_hash: str | None) -> str:
    local_hash = item.get("local_hash")
    repo_hash = item.get("repo_hash")
    local_exists = bool(item.get("local_exists"))
    repo_exists = bool(item.get("repo_exists"))
    if local_exists and repo_exists and local_hash == repo_hash:
        return "identical"
    if not item.get("portable", True):
        return "machine"
    if stored_hash:
        local_changed = local_exists and local_hash != stored_hash
        repo_changed = repo_exists and repo_hash != stored_hash
        local_deleted = (not local_exists) and stored_hash
        repo_deleted = (not repo_exists) and stored_hash
        if local_deleted and repo_changed:
            return "repo"
        if repo_deleted and local_changed:
            return "local"
        if local_deleted and repo_deleted:
            return "identical"
        if local_changed and repo_changed:
            return "both" if item.get("portable", True) else "machine"
        if local_changed or local_deleted:
            return "local" if item.get("portable", True) else "machine"
        if repo_changed or repo_deleted:
            return "repo" if item.get("portable", True) else "machine"
        if not local_exists and repo_exists:
            return "added-repo"
        if local_exists and not repo_exists:
            return "added-local"
    if not local_exists and not repo_exists:
        return "identical"
    if local_exists and not repo_exists:
        return "added-local"
    if repo_exists and not local_exists:
        return "added-repo"
    if not item.get("portable", True):
        # Display layout stays on this machine unless the user opts in.
        return "machine"
    return "differs"


def unified_preview(local_path: Path, repo_path: Path, local_within: Path | None = None, repo_within: Path | None = None) -> str:
    try:
        import difflib
    except Exception:
        return ""
    if file_too_large(local_path) or file_too_large(repo_path):
        return "Binary or very large file — open the paths to compare."
    local_text = read_text(local_path, within=local_within) if local_path.is_file() else ""
    repo_text = read_text(repo_path, within=repo_within) if repo_path.is_file() else ""
    if len(local_text) + len(repo_text) > MAX_DIFF_BYTES * 4:
        return "Binary or very large file — open the paths to compare."
    diff = list(
        difflib.unified_diff(
            local_text.splitlines(),
            repo_text.splitlines(),
            fromfile="local",
            tofile="repo",
            lineterm="",
        )
    )
    if not diff:
        return ""
    clipped = diff[:MAX_DIFF_LINES]
    text = "\n".join(clipped)
    if len(diff) > MAX_DIFF_LINES:
        text += f"\n… {len(diff) - MAX_DIFF_LINES} more lines"
    if len(text) > MAX_DIFF_BYTES:
        text = text[: MAX_DIFF_BYTES - 20] + "\n… truncated"
    return text


def strip_lua_comments(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


def parse_shortcuts(text: str) -> list[dict[str, str]]:
    return [
        {"keys": e["keys"], "label": e["label"], "kind": e["kind"]}
        for e in extract_bind_statements(text)
    ]


def extract_bind_statements(text: str) -> list[dict[str, Any]]:
    """One-line o.bind / hl.unbind entries. Last occurrence of each key wins.

    Omarchy rebinds a default with `hl.unbind` then `o.bind` for the same key.
    Hyprland executes in order, so the later statement is the effective shortcut.
    """
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        bind = BIND_RE.search(line)
        unbind = UNBIND_RE.search(line) if not bind else None
        if bind:
            keys = bind.group(1).strip()
            label = (bind.group(2) or "").strip() or "Custom binding"
            kind = "bind"
        elif unbind:
            keys = unbind.group(1).strip()
            label = "Unbound default"
            kind = "unbind"
        else:
            continue
        if keys not in by_key:
            order.append(keys)
        by_key[keys] = {
            "keys": keys,
            "label": label,
            "kind": kind,
            "line": index,
            "raw": line.rstrip("\n"),
        }
    return [by_key[key] for key in order]


def shortcut_diff(
    local_path: Path,
    repo_path: Path,
    stored_hash: str | None = None,
    local_within: Path | None = None,
    repo_within: Path | None = None,
) -> list[dict[str, Any]]:
    local_text = read_text(local_path, within=local_within) if local_path.is_file() else ""
    repo_text = read_text(repo_path, within=repo_within) if repo_path.is_file() else ""
    local_map = {e["keys"]: e for e in extract_bind_statements(local_text)}
    repo_map = {e["keys"]: e for e in extract_bind_statements(repo_text)}
    local_file_hash = file_hash(local_path, "hypr/bindings.lua", within=local_within) if local_path.is_file() else None
    repo_file_hash = file_hash(repo_path, "hypr/bindings.lua", within=repo_within) if repo_path.is_file() else None
    local_at_baseline = bool(stored_hash) and local_file_hash == stored_hash
    repo_at_baseline = bool(stored_hash) and repo_file_hash == stored_hash
    rows = []
    for keys in sorted(set(local_map) | set(repo_map)):
        local_e = local_map.get(keys)
        repo_e = repo_map.get(keys)
        local_raw = local_e["raw"] if local_e else ""
        repo_raw = repo_e["raw"] if repo_e else ""
        if local_e and repo_e and local_raw.strip() == repo_raw.strip():
            continue
        if local_e and not repo_e:
            status = "added-local"
            change = "added"
        elif repo_e and not local_e:
            status = "added-repo"
            change = "added"
        elif local_at_baseline and not repo_at_baseline:
            status = "repo"
            change = "changed"
        elif repo_at_baseline and not local_at_baseline:
            status = "local"
            change = "changed"
        elif stored_hash:
            status = "both"
            change = "changed"
        else:
            # No sync baseline yet: treat as incoming so Apply can cherry-pick
            # which repo binds land locally. Uncheck to keep the local bind.
            status = "repo"
            change = "changed"
        local_label = (local_e or {}).get("label") or ""
        repo_label = (repo_e or {}).get("label") or ""
        if status in {"added-repo", "repo"}:
            label = repo_label or local_label or keys
            detail = f"was: {local_label}" if change == "changed" and local_label else "new in repo"
        elif status in {"added-local", "local"}:
            label = local_label or repo_label or keys
            detail = f"repo has: {repo_label}" if change == "changed" and repo_label else "new on this machine"
        else:
            label = local_label or repo_label or keys
            detail = f"this machine: {local_label} · repo: {repo_label}"
        rows.append(
            {
                "keys": keys,
                "label": label,
                "detail": detail,
                "change": change,
                "kind": (local_e or repo_e or {}).get("kind") or "bind",
                "status": status,
                "local_label": local_label,
                "repo_label": repo_label,
                "local_raw": local_raw,
                "repo_raw": repo_raw,
                "default_apply": status in {"added-repo", "repo"},
                "default_publish": status in {"added-local", "local"},
            }
        )
    return rows


def upsert_shortcut_lines(dest_text: str, source_entries: dict[str, dict[str, Any]], selected_keys: list[str]) -> str:
    dest_entries = {e["keys"]: e for e in extract_bind_statements(dest_text)}
    lines = dest_text.splitlines(keepends=True)
    replacements: dict[int, str] = {}
    append: list[str] = []
    for key in selected_keys:
        src = source_entries.get(key)
        if not src:
            continue
        raw = src["raw"].rstrip("\n") + "\n"
        dest = dest_entries.get(key)
        if dest is not None and 0 <= dest["line"] < len(lines):
            replacements[dest["line"]] = raw
        else:
            append.append(raw)
    out: list[str] = []
    for i, line in enumerate(lines):
        out.append(replacements[i] if i in replacements else line)
    if append:
        body = "".join(out)
        if body and not body.endswith("\n"):
            out.append("\n")
        if "-- config-sync cherry-pick" not in body:
            out.append("\n-- config-sync cherry-pick\n")
        out.extend(append)
    return "".join(out)


def merge_shortcuts_file(
    dest: Path,
    source: Path,
    selected_keys: list[str],
    dest_within: Path | None = None,
    source_within: Path | None = None,
) -> bool:
    if not selected_keys:
        return False
    if file_too_large(source) or file_too_large(dest):
        # read_text() degrades oversized files to ""; merging on top of that
        # would silently replace the user's bindings. Refuse instead.
        raise SyncError("bindings.lua is too large to merge safely.")
    source_text = read_text(source, within=source_within) if (source.is_file() and not source.is_symlink() and not os.path.islink(source)) else ""
    source_entries = {e["keys"]: e for e in extract_bind_statements(source_text)}
    dest_text = read_text(dest, within=dest_within) if (dest.is_file() and not dest.is_symlink() and not os.path.islink(dest)) else ""
    merged = upsert_shortcut_lines(dest_text, source_entries, selected_keys)
    if merged == dest_text:
        return False
    atomic_write_text(dest, merged, mode=0o600, within=dest_within)
    return True


def _rollup_statuses(statuses: list[str]) -> str | None:
    unique = set(s for s in statuses if s not in {"identical", "machine"})
    if not unique:
        return None
    if "both" in unique or ("local" in unique and "repo" in unique) or ("added-local" in unique and "added-repo" in unique):
        return "both"
    if unique <= {"local", "added-local"}:
        return "added-local" if "added-local" in unique else "local"
    if unique <= {"repo", "added-repo"}:
        return "added-repo" if "added-repo" in unique else "repo"
    return "differs"


def plugin_groups(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in files:
        path = str(item.get("path") or "")
        if item.get("group") != "plugin" and not path.startswith("plugins/"):
            continue
        pid = plugin_id_from_path(item["path"])
        if not pid:
            continue
        g = groups.setdefault(
            pid,
            {
                "id": pid,
                "name": pid,
                "files": [],
                "statuses": [],
                "git_managed": bool(item.get("git_managed")),
            },
        )
        g["files"].append(item["path"])
        g["statuses"].append(item.get("status") or "identical")
        if item.get("git_managed"):
            g["git_managed"] = True
    out = []
    for pid, g in sorted(groups.items()):
        if pid == PLUGIN_ID:
            continue
        statuses = [s for s in g["statuses"] if s not in {"identical", "machine"}]
        status = _rollup_statuses(g["statuses"])
        if not status:
            continue
        out.append(
            {
                "id": pid,
                "name": pid,
                "status": status,
                "files": g["files"],
                "file_count": len(g["files"]),
                "changed_count": len(statuses),
                "git_managed": g["git_managed"],
                # Plugins carry executable code; incoming ones require explicit opt-in, never a default-checked Apply.
                "default_apply": False,
                "default_publish": status in {"local", "added-local", "differs", "both"},
            }
        )
    return out


def file_bundles(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group plugin trees, hook dirs, and similar folders into one checkbox each."""
    buckets: dict[str, dict[str, Any]] = {}

    def bucket_for(path: str) -> tuple[str, str, str] | None:
        parts = path.split("/")
        if path.startswith("plugins/") and len(parts) >= 2 and parts[1]:
            pid = parts[1]
            if pid == PLUGIN_ID:
                return None
            return "plugin:" + pid, "plugin", pid
        if path.startswith("omarchy/hooks/") and len(parts) >= 3:
            event = parts[2]
            return "hooks:" + event, "hooks", event.replace(".d", "")
        if path.startswith("omarchy/agents/"):
            return "agents", "agents", "Agent helpers"
        if path.startswith("omarchy/branding/"):
            return "branding", "branding", "Branding"
        if path.startswith("omarchy/extensions/"):
            return "extensions", "extensions", "Menu extensions"
        if path.startswith("bin/") and len(parts) >= 2:
            return "bin", "bin", "Helper scripts"
        return None

    for item in files:
        spec = bucket_for(item.get("path") or "")
        if not spec:
            continue
        bid, kind, title = spec
        b = buckets.setdefault(
            bid,
            {
                "id": bid,
                "kind": kind,
                "plugin_id": title if kind == "plugin" else "",
                "name": title,
                "files": [],
                "statuses": [],
            },
        )
        b["files"].append(item["path"])
        b["statuses"].append(item.get("status") or "identical")

    out = []
    for bid, b in sorted(buckets.items()):
        status = _rollup_statuses(b["statuses"])
        if not status:
            continue
        changed = [s for s in b["statuses"] if s not in {"identical", "machine"}]
        n = len(changed)
        if b["kind"] == "plugin":
            if status == "added-repo":
                summary = f"New plugin · {n} file{'s' if n != 1 else ''}"
            elif status == "added-local":
                summary = f"New on this machine · {n} file{'s' if n != 1 else ''}"
            else:
                summary = f"Plugin updates · {n} file{'s' if n != 1 else ''}"
        elif b["kind"] == "hooks":
            summary = f"{n} hook file{'s' if n != 1 else ''}"
        else:
            summary = f"{n} file{'s' if n != 1 else ''}"
        out.append(
            {
                "id": b["id"],
                "kind": b["kind"],
                "plugin_id": b["plugin_id"],
                "name": b["name"],
                "summary": summary,
                "status": status,
                "files": [p for p, s in zip(b["files"], b["statuses"]) if s not in {"identical", "machine"}],
                "changed_count": n,
                # Hooks/agents/branding/extensions/bin run code or steer an agent;
                # incoming bundles require explicit opt-in, never a default-checked Apply.
                "default_apply": False,
                "default_publish": status in {"local", "added-local", "differs"},
            }
        )
    return out


def plugin_id_from_path(rel: str) -> str:
    if not rel.startswith("plugins/"):
        return ""
    parts = rel.split("/")
    return parts[1] if len(parts) > 1 else ""


def expand_plugin_paths(files: list[dict[str, Any]], plugin_ids: list[str], direction: str) -> set[str]:
    wanted = set(plugin_ids)
    out: set[str] = set()
    for item in files:
        pid = plugin_id_from_path(item["path"])
        if pid not in wanted:
            continue
        if direction == "apply" and item.get("repo_exists"):
            out.add(item["path"])
        elif direction == "publish" and item.get("local_exists"):
            out.add(item["path"])
    return out


def expand_theme_paths(files: list[dict[str, Any]], direction: str) -> set[str]:
    out: set[str] = set()
    for item in files:
        if item.get("group") != "theme":
            continue
        if direction == "apply" and item.get("repo_exists"):
            # The theme checkbox must not smuggle runnable code onto the
            # machine; scripts inside a theme overlay need their own opt-in.
            if is_executable_payload(item["path"]):
                continue
            out.add(item["path"])
        elif direction == "publish" and item.get("local_exists"):
            out.add(item["path"])
    return out


def apply_omarchy_theme(slug: str, dry_run: bool) -> str:
    slug = (slug or "").strip()
    if dry_run or not slug:
        return ""
    if not re.match(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]*$", slug) or slug.startswith("-"):
        return "Invalid theme slug"
    binary = shutil.which("omarchy")
    if not binary:
        return "omarchy CLI not found; theme name was copied but not applied"
    try:
        result = run_bounded([binary, "theme", "set", "--", slug], timeout=90)
    except subprocess.TimeoutExpired:
        return f"omarchy theme set {slug} timed out"
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "failed").strip()
        return err or f"omarchy theme set {slug} failed"
    return "ok"


def inspect_repo(ctx: Context, repo: Path, prefer_local: bool = False) -> dict[str, Any]:
    validation = validate_repo(repo)
    tree_root = ctx.home if prefer_local else repo
    hypr_root = ctx.config_hypr if prefer_local else repo / "hypr"
    omarchy_root = ctx.config_omarchy if prefer_local else repo / "omarchy"
    plugins_dir = ctx.config_plugins if prefer_local else repo / "plugins"
    shortcuts: list[dict[str, str]] = []
    bindings = hypr_root / "bindings.lua"
    if bindings.is_file():
        shortcuts = parse_shortcuts(read_text(bindings, within=tree_root))

    plugins = []
    if plugins_dir.is_dir():
        for child in sorted(plugins_dir.iterdir()):
            manifest_path = child / "manifest.json"
            if not child.is_dir() or not manifest_path.is_file():
                continue
            manifest = load_json(manifest_path, default={}, within=tree_root) or {}
            plugins.append(
                {
                    "id": manifest.get("id") or child.name,
                    "name": manifest.get("name") or child.name,
                    "version": manifest.get("version") or "",
                    "description": manifest.get("description") or "",
                    "kinds": manifest.get("kinds") or [],
                }
            )

    bar = {"position": "", "widgets": {"left": [], "center": [], "right": []}}
    idle = {}
    shell = load_json(omarchy_root / "shell.json", default={}, within=tree_root) or {}
    if isinstance(shell, dict):
        idle = shell.get("idle") or {}
        bar_cfg = shell.get("bar") or {}
        bar["position"] = bar_cfg.get("position") or ""
        layout = bar_cfg.get("layout") or {}
        for section in ("left", "center", "right"):
            ids = []
            for entry in layout.get(section) or []:
                if isinstance(entry, dict) and entry.get("id"):
                    ids.append(entry["id"])
                elif isinstance(entry, str):
                    ids.append(entry)
            bar["widgets"][section] = ids

    hooks = []
    hooks_root = omarchy_root / "hooks"
    if hooks_root.is_dir():
        for event_dir in sorted(hooks_root.iterdir()):
            if not event_dir.is_dir():
                continue
            event = event_dir.name[:-2] if event_dir.name.endswith(".d") else event_dir.name
            for hook in sorted(event_dir.iterdir()):
                if hook.is_file() and not hook.name.startswith("."):
                    hooks.append(
                        {
                            "event": event,
                            "name": hook.name,
                            "sample": hook.name.endswith(".sample"),
                        }
                    )

    bins = []
    repo_bin = repo / "bin"
    if repo_bin.is_dir():
        bins = [p.name for p in sorted(repo_bin.iterdir()) if p.is_file()]

    terminals = []
    for rel, local in terminal_map(ctx).items():
        present = local.is_file() if prefer_local else (repo / rel).is_file()
        if present:
            terminals.append(Path(rel).stem.replace("ghostty.config", "ghostty"))

    configs = []
    for item in collect_inventory(ctx, repo):
        wanted = item["local_exists"] if prefer_local else item["repo_exists"]
        if item["group"] in {"hypr", "omarchy", "terminal"} and wanted:
            configs.append(
                {
                    "path": item["path"],
                    "summary": item["summary"],
                    "portable": item["portable"],
                    "group": item["group"],
                }
            )

    return {
        "valid": validation["valid"],
        "score": validation["score"],
        "reasons": validation["reasons"],
        "empty": is_seedable_empty(repo),
        "source": "local" if prefer_local else "repo",
        "shortcuts": shortcuts,
        "plugins": plugins,
        "bar": bar,
        "idle": idle,
        "hooks": hooks,
        "bins": bins,
        "terminals": terminals,
        "configs": configs,
        "theme": inspect_theme(ctx, repo, prefer_local=prefer_local),
    }


def inspect_theme(ctx: Context, repo: Path, prefer_local: bool = False) -> dict[str, Any]:
    local_slug = read_theme_slug(ctx.theme_name_path, within=ctx.home)
    repo_slug = read_theme_slug(repo / THEME_REL, within=repo)
    slug = local_slug if prefer_local else (repo_slug or local_slug)
    custom = bool(slug) and (
        (ctx.user_themes / slug).is_dir() or (repo / "omarchy" / "themes" / slug).is_dir()
    )
    return {
        "slug": slug,
        "display": theme_display_name(slug) or "—",
        "local_slug": local_slug,
        "repo_slug": repo_slug,
        "custom": custom,
    }


def git_status_fields(repo: Path, fetch_error: str | None = None) -> dict[str, Any]:
    branch = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    head = git_out(repo, "rev-parse", "--short", "HEAD")
    head_full = git_out(repo, "rev-parse", "HEAD")
    subject = git_out(repo, "log", "-1", "--pretty=%s")
    dirty = bool(git_out(repo, "status", "--porcelain"))
    ahead = 0
    behind = 0
    remote_head = ""
    upstream = git_out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream:
        counts = git_out(repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                ahead = int(parts[0])
                behind = int(parts[1])
        remote_head = git_out(repo, "rev-parse", "--short", upstream)
    conflicts = []
    merge_head = repo / ".git" / "MERGE_HEAD"
    if merge_head.exists() or (repo / ".git" / "rebase-merge").exists() or (repo / ".git" / "rebase-apply").exists():
        unmerged = git_out(repo, "diff", "--name-only", "--diff-filter=U")
        conflicts = [line for line in unmerged.splitlines() if line.strip()]
    remotes = git_out(repo, "remote", "get-url", "origin")
    return {
        "branch": branch,
        "head": head,
        "head_full": head_full,
        "head_subject": subject,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
        "remote_head": remote_head,
        "upstream": upstream,
        "conflicts": conflicts,
        "origin_url": remotes,
        "fetch_error": fetch_error,
    }


def maybe_fast_forward(repo: Path, git_fields: dict[str, Any]) -> dict[str, Any]:
    if git_fields["behind"] and not git_fields["ahead"] and not git_fields["dirty"] and not git_fields["conflicts"]:
        result = run_git(repo, ["merge", "--ff-only", git_fields["upstream"] or "FETCH_HEAD"], timeout=40)
        if result.returncode == 0:
            return git_status_fields(repo, git_fields.get("fetch_error"))
        git_fields["fetch_error"] = git_fields.get("fetch_error") or git_error_message(["merge", "--ff-only"], result)
    return git_fields


def fetch_repo(repo: Path) -> str | None:
    result = run_git(repo, ["fetch", "--prune", "origin"], timeout=FETCH_TIMEOUT)
    if result.returncode != 0:
        return git_error_message(["fetch"], result)
    return None


def default_apply_status(status: str) -> bool:
    return status in {"repo", "added-repo", "differs"}


def default_publish_status(status: str) -> bool:
    return status in {"local", "added-local", "differs"}


def annotate_diff(ctx: Context, repo: Path, state: dict[str, Any]) -> dict[str, Any]:
    stored = state.get("file_hashes") or {}
    hidden_keys = set(state.get("hidden") or [])
    files = []
    counts = {
        "identical": 0,
        "local": 0,
        "repo": 0,
        "both": 0,
        "added-local": 0,
        "added-repo": 0,
        "differs": 0,
        "machine": 0,
        "changed": 0,
        "hidden": 0,
    }
    for item in collect_inventory(ctx, repo):
        status = classify_file(item, stored.get(item["path"]))
        item["status"] = status
        item["hidden"] = is_hidden_item("f", item["path"], hidden_keys)
        plugin_id = plugin_id_from_path(item["path"])
        item["default_apply"] = (
            default_apply_status(status)
            and item["portable"]
            and item["repo_exists"]
            and not item.get("git_managed")
            and not item["hidden"]
            and not is_bundled_path(item["path"])
            and not is_executable_payload(item["path"])
        )
        item["default_publish"] = default_publish_status(status) and item["local_exists"] and not item["hidden"]
        if status not in {"identical", "machine"}:
            item["preview"] = unified_preview(
                Path(item["local_path"]),
                Path(item["repo_path"]),
                local_within=ctx.home,
                repo_within=repo,
            )
            if not item["hidden"]:
                counts["changed"] += 1
        else:
            item["preview"] = ""
        if not item["hidden"]:
            counts[status] = counts.get(status, 0) + 1
        elif status not in {"identical", "machine"}:
            counts["hidden"] = counts.get("hidden", 0) + 1
        files.append(item)
    shortcuts = shortcut_diff(
        ctx.config_hypr / "bindings.lua",
        repo / "hypr" / "bindings.lua",
        stored.get("hypr/bindings.lua"),
        local_within=ctx.home,
        repo_within=repo,
    )
    for s in shortcuts:
        s["hidden"] = is_hidden_item("s", s["keys"], hidden_keys)
    drop_bindings_file_without_shortcut_diffs(files, counts, shortcuts)
    plugins = plugin_groups(files)
    for plugin in plugins:
        manifest = load_json(repo / "plugins" / plugin["id"] / "manifest.json", default=None, within=repo) or load_json(
            ctx.config_plugins / plugin["id"] / "manifest.json", default=None, within=ctx.home
        )
        if isinstance(manifest, dict) and manifest.get("name"):
            plugin["name"] = str(manifest.get("name"))
        plugin["hidden"] = is_hidden_item("p", plugin["id"], hidden_keys)
    bundles = file_bundles(files)
    names = {p["id"]: p["name"] for p in plugins}
    for bundle in bundles:
        if bundle["kind"] == "plugin" and bundle["plugin_id"] in names:
            bundle["name"] = names[bundle["plugin_id"]]
        bundle["hidden"] = is_hidden_item("g", bundle["id"], hidden_keys)
    theme_files = [f for f in files if f.get("group") == "theme"]
    theme_diff = None
    if any(f.get("status") not in {"identical", "machine"} for f in theme_files):
        name_item = next((f for f in theme_files if f["path"] == THEME_REL), None)
        statuses = [f.get("status") for f in theme_files if f.get("status") not in {"identical", "machine"}]
        unique = set(statuses)
        if "both" in unique or ("local" in unique and "repo" in unique) or ("added-local" in unique and "added-repo" in unique):
            status = "both"
        elif unique <= {"local", "added-local"}:
            status = "added-local" if "added-local" in unique else "local"
        elif unique <= {"repo", "added-repo"}:
            status = "added-repo" if "added-repo" in unique else "repo"
        else:
            status = "differs"
        local_slug = read_theme_slug(ctx.theme_name_path, within=ctx.home)
        repo_slug = read_theme_slug(repo / THEME_REL, within=repo)
        slug = repo_slug or local_slug
        theme_diff = {
            "id": "selected",
            "slug": slug,
            "display": theme_display_name(slug) or slug,
            "local_slug": local_slug,
            "repo_slug": repo_slug,
            "status": status,
            "files": [f["path"] for f in theme_files],
            "custom": any(f["path"].startswith("omarchy/themes/") for f in theme_files),
            "default_apply": (status in {"repo", "added-repo", "differs"} or (name_item or {}).get("default_apply")) and not is_hidden_item("t", "selected", hidden_keys),
            "default_publish": (status in {"local", "added-local", "differs"} or (name_item or {}).get("default_publish")) and not is_hidden_item("t", "selected", hidden_keys),
            "hidden": is_hidden_item("t", "selected", hidden_keys),
        }
    return {
        "files": files,
        "counts": counts,
        "shortcuts": shortcuts,
        "plugins": plugins,
        "bundles": bundles,
        "theme": theme_diff,
        "hidden": list(hidden_keys),
    }


def drop_bindings_file_without_shortcut_diffs(
    files: list[dict[str, Any]], counts: dict[str, int], shortcuts: list[dict[str, Any]]
) -> None:
    """Ignore hypr/bindings.lua when every effective keybind already matches.

    Comment, order, and cherry-pick-section drift still change the file hash, but
    the Changes list is per-shortcut. Counting that file as incoming left the
    header on "Incoming updates" with an empty review list.
    """
    if any(not s.get("hidden") for s in shortcuts):
        return
    for item in files:
        if item.get("path") != "hypr/bindings.lua":
            continue
        status = str(item.get("status") or "identical")
        if status in {"identical", "machine"}:
            return
        hidden = bool(item.get("hidden"))
        item["status"] = "identical"
        item["preview"] = ""
        item["default_apply"] = False
        item["default_publish"] = False
        if hidden:
            counts["hidden"] = max(0, int(counts.get("hidden") or 0) - 1)
        else:
            counts["changed"] = max(0, int(counts.get("changed") or 0) - 1)
            counts[status] = max(0, int(counts.get(status) or 0) - 1)
            counts["identical"] = int(counts.get("identical") or 0) + 1
        return


def rollup_sync_state(git_fields: dict[str, Any], counts: dict[str, int], has_baseline: bool) -> str:
    if git_fields.get("conflicts"):
        return "conflicts"
    if git_fields.get("ahead") and git_fields.get("behind"):
        return "conflicts"
    both = counts.get("both", 0)
    local_n = counts.get("local", 0) + counts.get("added-local", 0)
    repo_n = counts.get("repo", 0) + counts.get("added-repo", 0)
    differs = counts.get("differs", 0)
    if not has_baseline:
        if differs or local_n or repo_n or both:
            return "ready"
        if git_fields.get("behind"):
            return "remote-ahead"
        if git_fields.get("ahead") or git_fields.get("dirty"):
            return "local-ahead"
        return "in-sync"
    if both or (local_n and repo_n):
        return "diverged"
    if git_fields.get("behind") or repo_n:
        return "remote-ahead" if not local_n else "diverged"
    if local_n or git_fields.get("ahead") or git_fields.get("dirty"):
        return "local-ahead"
    if differs:
        return "ready"
    return "in-sync"


def build_snapshot(ctx: Context, fetch: bool = False) -> dict[str, Any]:
    state = load_state(ctx)
    if not state.get("clone_path"):
        return ok(
            {
                "configured": False,
                "sync_state": "not-configured",
                "status": {
                    "configured": False,
                    "sync_state": "not-configured",
                    "repo_url": "",
                    "clone_path": "",
                },
                "inspect": None,
                "diff": {"files": [], "counts": {}, "hidden": []},
                "hidden": [],
            }
        )
    repo = configured_repo(ctx, state)
    fetch_error = None
    if fetch and state.get("repo_url") and not Path(state.get("repo_url", "")).exists():
        fetch_error = fetch_repo(repo)
    git_fields = git_status_fields(repo, fetch_error)
    git_fields = maybe_fast_forward(repo, git_fields)
    validation = validate_repo(repo)
    empty = is_seedable_empty(repo)
    inspect = inspect_repo(ctx, repo, prefer_local=empty)
    diff = annotate_diff(ctx, repo, state)
    sync_state = rollup_sync_state(git_fields, diff["counts"], has_baseline=bool(state.get("file_hashes")))
    if empty:
        sync_state = "empty"
    elif not validation["valid"]:
        sync_state = "invalid"
    status = {
        "configured": True,
        "sync_state": sync_state,
        "empty": empty,
        "repo_url": state.get("repo_url") or git_fields.get("origin_url") or "",
        "clone_path": str(repo),
        "using_existing_clone": bool(state.get("using_existing_clone")),
        "connected_at": state.get("connected_at"),
        "last_apply_at": state.get("last_apply_at"),
        "last_publish_at": state.get("last_publish_at"),
        "last_applied_commit": (state.get("last_applied_commit") or "")[:7],
        "hostname": socket.gethostname(),
        **git_fields,
        "valid": validation["valid"],
        "reasons": validation["reasons"],
        "counts": diff["counts"],
        "local_changes": diff["counts"].get("local", 0) + diff["counts"].get("added-local", 0),
        "repo_changes": diff["counts"].get("repo", 0) + diff["counts"].get("added-repo", 0),
        "both_changed": diff["counts"].get("both", 0),
        "unknown_differs": diff["counts"].get("differs", 0),
        "shortcut_changes": len([s for s in (diff.get("shortcuts") or []) if not s.get("hidden")]),
        "plugin_changes": len([p for p in (diff.get("plugins") or []) if not p.get("hidden")]),
        "hidden": state.get("hidden") or [],
        "hidden_count": len(state.get("hidden") or []),
        "plugin_version": PLUGIN_VERSION,
    }
    return ok(
        {
            "configured": True,
            "sync_state": sync_state,
            "status": status,
            "inspect": inspect,
            "diff": diff,
            "hidden": state.get("hidden") or [],
        }
    )


def cmd_snapshot(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    return build_snapshot(ctx, fetch=bool(args.fetch))


def cmd_connect(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    source_raw = ""
    if getattr(args, "stdin", False):
        source_raw = sys.stdin.read(MAX_URL_INPUT_BYTES).strip()
    if not source_raw:
        source_raw = " ".join(args.args).strip() or (args.url or "")
    kind, value = normalize_source(source_raw)
    ctx.state_dir.mkdir(parents=True, exist_ok=True)

    if kind == "path":
        repo = Path(value)
        if not (repo / ".git").exists() and not (repo / ".git").is_file():
            # allow worktrees / git files
            probe = run_git(repo, ["rev-parse", "--is-inside-work-tree"])
            if probe.returncode != 0:
                raise SyncError(f"{repo} is not a git repository.")
        return finish_connect(ctx, repo, git_out(repo, "remote", "get-url", "origin") or str(repo), using_existing=True, fetch=True)

    clean_url, cred_file = prepare_git_credentials(ctx, value)
    clone_path = ctx.default_clone
    existing_state = load_state(ctx)
    previous_clone: Path | None = None
    if clone_path.exists() and (clone_path / ".git").exists():
        current_origin = git_out(clone_path, "remote", "get-url", "origin")
        same = (
            current_origin.rstrip("/") == clean_url.rstrip("/")
            or current_origin.rstrip("/").removesuffix(".git") == clean_url.rstrip("/").removesuffix(".git")
        )
        if same:
            persist_git_credential_helper(clone_path, cred_file)
            fetch_error = fetch_repo(clone_path)
            if fetch_error:
                raise SyncError(fetch_error)
            run_git(clone_path, ["pull", "--ff-only"], timeout=40)
        else:
            # Different remote: move the old clone aside rather than deleting blindly.
            previous_clone = ctx.state_dir / f"repo.bak.{int(time.time())}"
            clone_path.rename(previous_clone)
            clone_path = ctx.default_clone
            result = run_git(
                None,
                [*git_cred_config_args(cred_file), "clone", "--", clean_url, str(clone_path)],
                timeout=CLONE_TIMEOUT,
                cwd=ctx.state_dir,
            )
            if result.returncode != 0:
                if clone_path.exists():
                    shutil.rmtree(clone_path, ignore_errors=True)
                if previous_clone.exists():
                    previous_clone.rename(ctx.default_clone)
                raise SyncError(git_error_message(["clone", clean_url], result))
            persist_git_credential_helper(clone_path, cred_file)
    else:
        if clone_path.exists():
            shutil.rmtree(clone_path)
        result = run_git(
            None,
            [*git_cred_config_args(cred_file), "clone", "--", clean_url, str(clone_path)],
            timeout=CLONE_TIMEOUT,
            cwd=ctx.state_dir,
        )
        if result.returncode != 0:
            if clone_path.exists():
                shutil.rmtree(clone_path, ignore_errors=True)
            raise SyncError(git_error_message(["clone", clean_url], result))
        persist_git_credential_helper(clone_path, cred_file)

    try:
        snap = finish_connect(ctx, clone_path, clean_url, using_existing=False, fetch=False)
        snap["message"] = snap.get("message") or f"Linked {clean_url}"
        return snap
    except SyncError:
        if clone_path.exists() and not existing_state.get("using_existing_clone"):
            shutil.rmtree(clone_path, ignore_errors=True)
        if previous_clone is not None and previous_clone.exists() and not ctx.default_clone.exists():
            previous_clone.rename(ctx.default_clone)
        raise


def finish_connect(ctx: Context, repo: Path, repo_url: str, using_existing: bool, fetch: bool) -> dict[str, Any]:
    validation = validate_repo(repo)
    empty = is_seedable_empty(repo)
    if not validation["valid"] and not empty:
        raise SyncError(
            "That git repo is not an Omarchy config repo, and it is not empty either. "
            "Use a private repo that is empty (to seed from this machine) or one that already "
            "has hypr/ configs plus shell.json, plugins/, or apply.sh.",
            extra={"validation": validation},
        )
    state = {
        "repo_url": repo_url,
        "clone_path": str(repo),
        "using_existing_clone": using_existing,
        "connected_at": now_iso(),
        "file_hashes": {},
        "hostname": socket.gethostname(),
        "empty_seed": empty,
    }
    save_state(ctx, state)
    snap = build_snapshot(ctx, fetch=fetch)
    snap["connected"] = True
    snap["validation"] = validation
    snap["empty"] = empty
    if empty:
        snap["message"] = (
            "Linked an empty private repo. Review the Configs tab "
            "(this machine), then Publish to seed the repo. Keep it private."
        )
    return snap


def cmd_disconnect(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    deleted = purge_saved_settings(ctx)
    return ok({"disconnected": True, "deleted_clone": deleted})


def copy_mapped_file(
    item: dict[str, Any],
    direction: str,
    src_root: Path | None = None,
    dst_root: Path | None = None,
) -> None:
    src = Path(item["repo_path"] if direction == "apply" else item["local_path"])
    dst = Path(item["local_path"] if direction == "apply" else item["repo_path"])
    # Every check is bound to the inode actually touched: the source is opened
    # O_NOFOLLOW, S_ISREG/size run via fstat on that descriptor, containment
    # is rechecked through /proc/self/fd, and the bytes are read from the same
    # descriptor with a running budget. The destination side mirrors it: the
    # parent directory is reached by a descriptor-relative walk with
    # containment verified at every hop, and the temp file plus the final
    # rename are dir_fd-relative to that descriptor, so a symlinked parent
    # swapped in concurrently cannot redirect the write.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        src_fd = os.open(str(src), flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SyncError(f"Refusing to copy symlink: {src}") from exc
        raise SyncError(f"Missing source file: {src}") from exc
    dir_fd: int | None = None
    try:
        st = os.fstat(src_fd)
        if not stat.S_ISREG(st.st_mode):
            raise SyncError(f"Refusing to copy non-regular file: {src}")
        if st.st_size > MAX_SYNC_FILE_BYTES:
            raise SyncError(f"Refusing to copy oversized file: {src}")
        if src_root is not None:
            proc_link = f"/proc/self/fd/{src_fd}"
            if not os.path.lexists(proc_link):
                raise SyncError("Cannot verify copy containment without /proc; refusing to copy.")
            actual = Path(os.path.realpath(proc_link))
            if not actual.is_relative_to(src_root.resolve()):
                raise SyncError(f"Refusing to copy {src}: it resolves outside {src_root}")
        # Only helper-script and hook trees — both explicit opt-in — ever get the
        # executable bit; a script smuggled anywhere else lands non-executable.
        target_mode = (
            0o755
            if (
                direction == "apply"
                and (item["path"].startswith("bin/") or item["path"].startswith("omarchy/hooks/"))
            )
            else 0o600
        )
        if dst_root is not None:
            try:
                rel = dst.relative_to(dst_root)
            except ValueError as exc:
                raise SyncError(f"Refusing to write {dst}: outside {dst_root}") from exc
            dir_fd = _open_dir_bound(dst_root, rel.parent)
            dst_name = rel.name
        else:
            ensure_parent(dst)
            dir_fd = os.open(str(dst.parent), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
            dst_name = dst.name

        def body(tmp_fd: int) -> None:
            remaining = MAX_SYNC_FILE_BYTES + 1
            while True:
                chunk = os.read(src_fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                os.write(tmp_fd, chunk)
                remaining -= len(chunk)
                if remaining <= 0:
                    raise SyncError(f"Refusing to copy {src}: it grew past the size limit during the copy.")

        _replace_at(dir_fd, dst_name, target_mode, body)
    finally:
        os.close(src_fd)
        if dir_fd is not None:
            os.close(dir_fd)


def backup_local(ctx: Context, files: list[dict[str, Any]]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ctx.home / ".config" / f"omarchy-backup.{stamp}"
    copied = 0
    for item in files:
        src = Path(item["local_path"])
        # The open is bound to $HOME: O_NOFOLLOW covers the leaf, and the
        # /proc/self/fd containment recheck covers symlinked parents, so a
        # path that resolves outside the home tree is skipped instead of
        # copying unrelated local data into the backup.
        src_fd = _open_bound(src, MAX_SYNC_FILE_BYTES, within=ctx.home)
        if src_fd is None:
            continue
        try:
            rel = Path(item["path"])
            # Map back into a backup tree that mirrors ~/.config and ~/.local/bin
            if item["path"].startswith("bin/"):
                dest = backup_dir / "local-bin" / rel.name
            elif item["path"].startswith("terminals/"):
                dest = backup_dir / "terminals" / rel.name
            elif item["path"].startswith("plugins/"):
                dest = backup_dir / "omarchy" / rel
            else:
                dest = backup_dir / rel

            def body(tmp_fd: int) -> None:
                # Hard byte budget during the copy itself: the fstat size
                # check bounds what the file was at open time, this bounds
                # what it becomes if it grows while being copied.
                remaining = MAX_SYNC_FILE_BYTES + 1
                while True:
                    chunk = os.read(src_fd, min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    os.write(tmp_fd, chunk)
                    remaining -= len(chunk)
                    if remaining <= 0:
                        raise SyncError(
                            f"Backup aborted: {src} grew past the size limit while it was being copied."
                        )

            _write_within(ctx.home, dest, 0o600, body)
        finally:
            os.close(src_fd)
        copied += 1
    backup_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        backup_dir / "README.txt",
        f"Omarchy config-sync backup of {copied} files at {now_iso()}\n",
        mode=0o600,
        within=ctx.home,
    )
    return backup_dir


def strip_plugin_git_dirs(repo: Path) -> None:
    """Drop accidental .git dirs copied along with plugins before committing.

    Deletion is anchored to a verified descriptor for the clone's plugins/
    tree and every plugin dir is opened O_NOFOLLOW, so a repo that commits
    plugins/<x> as a symlink cannot steer the rmtree at a .git directory
    elsewhere on the machine; the rmtree itself descends dir_fd-relative."""
    plugins_dir = repo / "plugins"
    if not plugins_dir.is_dir() or plugins_dir.is_symlink():
        return
    try:
        plugins_fd = _open_dir_bound(repo, Path("plugins"), create=False)
    except SyncError:
        return
    try:
        for name in os.listdir(plugins_fd):
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=plugins_fd,
                )
            except OSError:
                continue  # symlinked or non-directory entry: leave it alone
            try:
                st = os.stat(".git", dir_fd=child_fd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(".git", ignore_errors=True, dir_fd=child_fd)
            except OSError:
                pass
            finally:
                os.close(child_fd)
    finally:
        os.close(plugins_fd)


def selected_items(diff_files: list[dict[str, Any]], wanted: set[str] | None, include_machine: bool, direction: str) -> list[dict[str, Any]]:
    chosen = []
    for item in diff_files:
        rel = item["path"]
        if wanted is not None:
            if rel not in wanted:
                continue
        else:
            if item.get("hidden"):
                continue
            if direction == "apply" and not item.get("default_apply"):
                continue
            if direction == "publish" and not item.get("default_publish"):
                continue
        if not include_machine and not item["portable"]:
            continue
        if direction == "apply" and not item["repo_exists"] and wanted is None:
            continue
        if direction == "publish" and not item["local_exists"] and wanted is None:
            continue
        chosen.append(item)
    return chosen


def parse_files_arg(raw: str | None, explicit: bool = False) -> set[str] | None:
    if raw is None or raw == "":
        return set() if explicit else None
    parts = [p.strip() for p in raw.split(",") if p.strip() and validate_safe_rel_path(p.strip())]
    return set(parts)


def extract_widget_entry(shell_data: Any) -> tuple[str | None, dict[str, Any] | None, int]:
    if not isinstance(shell_data, dict):
        return None, None, -1
    bar = shell_data.get("bar") or {}
    layout = bar.get("layout") or {}
    for section in ("left", "center", "right"):
        entries = layout.get(section) or []
        for idx, entry in enumerate(entries):
            entry_id = entry.get("id") if isinstance(entry, dict) else entry
            if entry_id == PLUGIN_ID:
                saved = dict(entry) if isinstance(entry, dict) else {"id": PLUGIN_ID}
                return section, saved, idx
    return None, None, -1


def restore_widget_entry(
    shell_path: Path,
    section: str | None,
    entry: dict[str, Any] | None,
    index: int,
    within: Path | None = None,
) -> None:
    if not entry or not shell_path.is_file():
        return
    data = load_json(shell_path, default=None, within=within)
    if not isinstance(data, dict):
        return
    current_section, _, _ = extract_widget_entry(data)
    if current_section:
        return
    bar = data.setdefault("bar", {})
    layout = bar.setdefault("layout", {})
    target = section or "right"
    entries = list(layout.get(target) or [])
    insert_at = index if 0 <= index <= len(entries) else len(entries)
    # Prefer sitting next to the tray if we lost the original index.
    if index < 0:
        for i, existing in enumerate(entries):
            eid = existing.get("id") if isinstance(existing, dict) else existing
            if eid in {"gladimdim.tray", "omarchy.tray"}:
                insert_at = i + 1
                break
    entries.insert(insert_at, entry)
    layout[target] = entries
    write_json(shell_path, data, within=within)


def reload_desktop() -> dict[str, str]:
    notes = {}
    hypr = shutil.which("hyprctl")
    if hypr:
        result = run_bounded([hypr, "reload"], timeout=20)
        notes["hyprctl"] = "ok" if result.returncode == 0 else (result.stderr or result.stdout or "failed").strip()
    shell = shutil.which("omarchy-shell")
    if shell:
        for cmd in (["shell", "reloadConfig"], ["shell", "rescanPlugins"]):
            result = run_bounded([shell, *cmd], timeout=20)
            notes[" ".join(cmd)] = "ok" if result.returncode == 0 else (result.stderr or result.stdout or "failed").strip()
    return notes


def cmd_apply(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(ctx)
    repo = configured_repo(ctx, state)
    fetch_error = fetch_repo(repo)
    git_fields = git_status_fields(repo, fetch_error)
    git_fields = maybe_fast_forward(repo, git_fields)
    if git_fields["conflicts"]:
        raise SyncError(
            "The git clone has merge conflicts. Resolve them before applying.",
            extra={"conflicts": git_fields["conflicts"]},
        )
    if git_fields["behind"]:
        raise SyncError(
            "Remote is ahead and could not fast-forward. Pull/merge first, then Apply.",
            extra={"ahead": git_fields["ahead"], "behind": git_fields["behind"]},
        )
    diff = annotate_diff(ctx, repo, state)
    explicit = bool(getattr(args, "explicit", False))
    shortcut_keys = [s for s in (getattr(args, "shortcut", None) or []) if s]
    plugin_ids = [p for p in (getattr(args, "plugin", None) or []) if p]
    wanted = parse_files_arg(args.files, explicit=explicit)
    if plugin_ids:
        extra = expand_plugin_paths(diff["files"], plugin_ids, "apply")
        wanted = set() if wanted is None else set(wanted)
        wanted |= extra
    if getattr(args, "theme", False) or (wanted is not None and THEME_REL in wanted):
        extra_theme = expand_theme_paths(diff["files"], "apply")
        wanted = set() if wanted is None else set(wanted)
        wanted |= extra_theme
    if shortcut_keys and wanted is not None:
        wanted.discard("hypr/bindings.lua")
    unresolved_both = [
        i
        for i in diff["files"]
        if not i.get("hidden") and i["status"] == "both" and (i["portable"] or args.include_machine) and (wanted is None or i["path"] in wanted)
    ]
    if unresolved_both and wanted is None:
        raise SyncError(
            "Some files changed on both this machine and the repo. Pick Keep local or Take repo for each, then Apply.",
            extra={"both": [i["path"] for i in unresolved_both]},
        )
    chosen = selected_items(diff["files"], wanted, bool(args.include_machine), "apply")
    if shortcut_keys:
        chosen = [i for i in chosen if i["path"] != "hypr/bindings.lua"]
    if not chosen and not shortcut_keys:
        snap = build_snapshot(ctx, fetch=False)
        snap["applied"] = []
        snap["message"] = "Nothing to apply."
        return snap

    shell_path = ctx.config_omarchy / "shell.json"
    section, widget_entry, widget_index = extract_widget_entry(load_json(shell_path, default={}, within=ctx.home))
    backup_targets = [i for i in chosen if i["local_exists"]]
    bindings_local = ctx.config_hypr / "bindings.lua"
    if shortcut_keys and bindings_local.is_file():
        backup_targets.append(
            {
                "path": "hypr/bindings.lua",
                "local_path": str(bindings_local),
                "local_exists": True,
            }
        )
    backup_dir = backup_local(ctx, backup_targets)
    applied = []
    for item in chosen:
        if not Path(item["repo_path"]).is_file():
            continue
        copy_mapped_file(item, "apply", src_root=repo, dst_root=ctx.home)
        applied.append(item["path"])
    if shortcut_keys:
        if merge_shortcuts_file(
            bindings_local,
            repo / "hypr" / "bindings.lua",
            shortcut_keys,
            dest_within=ctx.home,
            source_within=repo,
        ):
            applied.append("hypr/bindings.lua")
    restore_widget_entry(shell_path, section, widget_entry, widget_index, within=ctx.home)

    # Refresh hashes for every tracked file after apply.
    post = collect_inventory(ctx, repo)
    hashes = dict(state.get("file_hashes") or {})
    applied_set = set(applied)
    for item in post:
        live = file_hash(Path(item["local_path"]), item["path"], within=ctx.home) if Path(item["local_path"]).is_file() else None
        if item["path"] in applied_set and live:
            hashes[item["path"]] = live
        elif not item["portable"]:
            # Machine-specific files were left alone; freeze the local copy
            # as the baseline so they stop looking like incoming diffs.
            if item["local_hash"]:
                hashes[item["path"]] = item["local_hash"]
        elif item["local_exists"] and item["repo_exists"] and item["local_hash"] == item["repo_hash"] and item["local_hash"]:
            hashes[item["path"]] = item["local_hash"]
    state["file_hashes"] = hashes
    state["last_apply_at"] = now_iso()
    state["last_applied_commit"] = git_fields.get("head_full") or git_out(repo, "rev-parse", "HEAD")
    save_state(ctx, state)
    notes = {}
    if not args.dry_run:
        notes = reload_desktop()
        if THEME_REL in applied:
            slug = read_theme_slug(ctx.theme_name_path, within=ctx.home)
            theme_note = apply_omarchy_theme(slug, dry_run=False)
            if theme_note:
                notes["theme"] = theme_note
    snap = build_snapshot(ctx, fetch=False)
    snap["applied"] = applied
    snap["backup_dir"] = str(backup_dir)
    snap["reload"] = notes
    theme_msg = ""
    if THEME_REL in applied:
        slug = read_theme_slug(ctx.theme_name_path, within=ctx.home)
        if slug:
            theme_msg = f" Theme set to {theme_display_name(slug)}."
    snap["message"] = (
        f"Applied {len(applied)} file{'s' if len(applied) != 1 else ''} from the repo.{theme_msg}"
    )
    return snap


def ensure_git_identity(repo: Path) -> None:
    name = git_out(repo, "config", "user.name") or git_out(None, "config", "--global", "user.name")
    email = git_out(repo, "config", "user.email") or git_out(None, "config", "--global", "user.email")
    if not name:
        run_git(repo, ["config", "user.name", os.environ.get("USER") or "omarchy"], check=True)
    if not email:
        host = socket.gethostname()
        user = os.environ.get("USER") or "omarchy"
        run_git(repo, ["config", "user.email", f"{user}@{host}"], check=True)


def cmd_publish(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(ctx)
    repo = configured_repo(ctx, state)
    fetch_error = fetch_repo(repo)
    git_fields = git_status_fields(repo, fetch_error)
    git_fields = maybe_fast_forward(repo, git_fields)
    if git_fields["conflicts"]:
        raise SyncError(
            "The git clone has merge conflicts. Resolve them before publishing.",
            extra={"conflicts": git_fields["conflicts"]},
        )
    if git_fields["behind"]:
        raise SyncError(
            "Remote has commits this clone does not. Pull/merge first so you do not overwrite another machine.",
            extra={"ahead": git_fields["ahead"], "behind": git_fields["behind"]},
        )
    diff = annotate_diff(ctx, repo, state)
    explicit = bool(getattr(args, "explicit", False))
    shortcut_keys = [s for s in (getattr(args, "shortcut", None) or []) if s]
    plugin_ids = [p for p in (getattr(args, "plugin", None) or []) if p]
    wanted = parse_files_arg(args.files, explicit=explicit)
    if plugin_ids:
        extra = expand_plugin_paths(diff["files"], plugin_ids, "publish")
        wanted = set() if wanted is None else set(wanted)
        wanted |= extra
    if getattr(args, "theme", False) or (wanted is not None and THEME_REL in wanted):
        extra_theme = expand_theme_paths(diff["files"], "publish")
        wanted = set() if wanted is None else set(wanted)
        wanted |= extra_theme
    if shortcut_keys and wanted is not None:
        wanted.discard("hypr/bindings.lua")
    unresolved_both = [
        i
        for i in diff["files"]
        if not i.get("hidden") and i["status"] == "both" and (i["portable"] or args.include_machine) and (wanted is None or i["path"] in wanted)
    ]
    if unresolved_both and wanted is None:
        raise SyncError(
            "Some files changed on both this machine and the repo. Pick Keep local or Take repo for each, then Publish.",
            extra={"both": [i["path"] for i in unresolved_both]},
        )
    chosen = selected_items(diff["files"], wanted, bool(args.include_machine), "publish")
    if shortcut_keys:
        chosen = [i for i in chosen if i["path"] != "hypr/bindings.lua"]
    if not chosen and not shortcut_keys:
        if args.push and git_fields["ahead"] and not git_fields["behind"]:
            result = run_git(repo, ["push", "-u", "origin", "HEAD"], timeout=PUSH_TIMEOUT)
            snap = build_snapshot(ctx, fetch=False)
            if result.returncode != 0:
                snap["push_error"] = git_error_message(["push"], result)
                snap["message"] = "Nothing new to commit, and push failed: " + snap["push_error"]
            else:
                snap["pushed"] = True
                snap["published"] = []
                snap["message"] = "Pushed existing local commits to origin."
            return snap
        snap = build_snapshot(ctx, fetch=False)
        snap["published"] = []
        snap["message"] = "Nothing to publish."
        return snap

    write_marker(repo)
    published = []
    for item in chosen:
        if not Path(item["local_path"]).is_file():
            continue
        copy_mapped_file(item, "publish", src_root=ctx.home, dst_root=repo)
        published.append(item["path"])
    if shortcut_keys:
        if merge_shortcuts_file(
            repo / "hypr" / "bindings.lua",
            ctx.config_hypr / "bindings.lua",
            shortcut_keys,
            dest_within=repo,
            source_within=ctx.home,
        ):
            published.append("hypr/bindings.lua")

    strip_plugin_git_dirs(repo)

    ensure_git_identity(repo)
    run_git(repo, ["add", "-A"], check=True)
    porcelain = git_out(repo, "status", "--porcelain")
    committed = False
    if porcelain:
        host = socket.gethostname()
        listed = "\n".join(f"- {p}" for p in published[:30])
        if len(published) > 30:
            listed += f"\n- … {len(published) - 30} more"
        message = args.message or f"Sync config from {host}\n\n{listed}\n"
        result = run_git(repo, ["commit", "-m", message], timeout=30)
        if result.returncode != 0:
            raise SyncError(git_error_message(["commit"], result))
        committed = True

    pushed = False
    push_error = None
    if args.push:
        result = run_git(repo, ["push", "-u", "origin", "HEAD"], timeout=PUSH_TIMEOUT)
        if result.returncode != 0:
            push_error = git_error_message(["push"], result)
        else:
            pushed = True

    post = collect_inventory(ctx, repo)
    hashes = dict(state.get("file_hashes") or {})
    for item in post:
        if item["local_exists"] and item["repo_exists"] and item["local_hash"] == item["repo_hash"] and item["local_hash"]:
            hashes[item["path"]] = item["local_hash"]
        elif item["path"] in published:
            live = sha256_file(Path(item["repo_path"]), within=repo)
            if live:
                hashes[item["path"]] = live
    state["file_hashes"] = hashes
    state["last_publish_at"] = now_iso()
    save_state(ctx, state)
    snap = build_snapshot(ctx, fetch=False)
    snap["published"] = published
    snap["committed"] = committed
    snap["pushed"] = pushed
    if push_error:
        snap["push_error"] = push_error
        snap["ok"] = True
        snap["message"] = (
            f"Saved {len(published)} file{'s' if len(published) != 1 else ''} in the repo, but push failed: {push_error}"
        )
    else:
        snap["message"] = (
            f"Published {len(published)} file{'s' if len(published) != 1 else ''} to the repo"
            + (" and pushed." if pushed else ". Commit is local until you push.")
        )
    return snap


def cmd_resync(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    """Make this machine match the repo, or publish this machine over the repo."""
    side = (args.side or "repo").strip().lower()
    if side in {"local", "ours", "this"}:
        side = "local"
    else:
        side = "repo"

    repo = configured_repo(ctx)
    fetch_error = fetch_repo(repo)
    git_fields = git_status_fields(repo, fetch_error)
    git_fields = maybe_fast_forward(repo, git_fields)
    if git_fields["behind"] and side == "repo":
        merge = run_git(repo, ["merge", git_fields["upstream"] or "origin/" + (git_fields["branch"] or "main")], timeout=40)
        if merge.returncode != 0:
            conflicts = git_status_fields(repo).get("conflicts") or []
            if conflicts:
                raise SyncError(
                    "Git merge conflicts. Resolve them, then Resync from repo again.",
                    extra={"conflicts": conflicts},
                )
            raise SyncError(git_error_message(["merge"], merge))

    state = load_state(ctx)
    diff = annotate_diff(ctx, repo, state)
    incoming = {"repo", "added-repo", "differs", "both"}
    outgoing = {"local", "added-local", "differs", "both"}
    wanted_status = incoming if side == "repo" else outgoing

    files = [
        item["path"]
        for item in diff["files"]
        if not item.get("hidden")
        and item.get("status") in wanted_status
        and (item.get("portable") or args.include_machine)
        and (item.get("repo_exists") if side == "repo" else item.get("local_exists"))
    ]
    shortcuts = [
        item["keys"]
        for item in (diff.get("shortcuts") or [])
        if not item.get("hidden") and item.get("status") in wanted_status
    ]
    plugins = [
        item["plugin_id"]
        for item in (diff.get("bundles") or [])
        if not item.get("hidden") and item.get("kind") == "plugin" and item.get("status") in wanted_status and item.get("plugin_id")
    ]
    theme = any(item["path"] == THEME_REL and not item.get("hidden") and item.get("status") in wanted_status for item in diff["files"])

    nested = argparse.Namespace(
        explicit=True,
        files=",".join(files),
        shortcut=shortcuts,
        plugin=plugins,
        theme=theme,
        fetch=False,
        push=side == "local" or bool(getattr(args, "push", False)),
        include_machine=bool(getattr(args, "include_machine", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        message=getattr(args, "message", None),
        delete_clone=False,
        side=side,
        args=[],
        command="apply",
        url=None,
    )

    if side == "repo":
        if not files and not shortcuts and not plugins and not theme:
            snap = build_snapshot(ctx, fetch=False)
            snap["message"] = "Nothing from the repo to apply. This machine may already match."
            return snap
        result = cmd_apply(ctx, nested)
        result["message"] = "Resynced this machine from the repo. " + str(result.get("message") or "")
        result["resync"] = "repo"
        return result

    nested.push = True
    if not files and not shortcuts and not plugins and not theme and not git_fields.get("ahead"):
        snap = build_snapshot(ctx, fetch=False)
        snap["message"] = "Nothing local to publish."
        return snap
    result = cmd_publish(ctx, nested)
    result["message"] = "Published this machine as the source of truth. " + str(result.get("message") or "")
    result["resync"] = "local"
    return result


def cmd_pull(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    repo = configured_repo(ctx)
    fetch_error = fetch_repo(repo)
    if fetch_error:
        raise SyncError(fetch_error)
    git_fields = git_status_fields(repo)
    if not git_fields["behind"] and not git_fields["conflicts"]:
        snap = build_snapshot(ctx, fetch=False)
        snap["message"] = "Already up to date with origin."
        snap["pulled"] = False
        return snap
    result = run_git(repo, ["merge", git_fields["upstream"] or "origin/" + git_fields["branch"]], timeout=40)
    if result.returncode != 0:
        conflicts = git_status_fields(repo).get("conflicts") or []
        if conflicts:
            return fail(
                "Merge conflicts. Keep local (ours) or take incoming (theirs) for each file.",
                conflicts=conflicts,
                sync_state="conflicts",
            )
        raise SyncError(git_error_message(["merge"], result))
    snap = build_snapshot(ctx, fetch=False)
    snap["pulled"] = True
    snap["message"] = "Pulled the latest commits from origin."
    return snap


def cmd_resolve(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    repo = configured_repo(ctx)
    if not args.args:
        raise SyncError("Pass the conflicted path to resolve.")
    rel = args.args[0]
    side = (args.side or (args.args[1] if len(args.args) > 1 else "") or "").strip().lower()
    if side in {"local", "ours"}:
        checkout = "--ours"
        label = "local (ours)"
    elif side in {"repo", "theirs", "incoming"}:
        checkout = "--theirs"
        label = "incoming (theirs)"
    else:
        raise SyncError("Side must be ours/local or theirs/repo.")
    result = run_git(repo, ["checkout", checkout, "--", rel])
    if result.returncode != 0:
        raise SyncError(git_error_message(["checkout", checkout, rel], result))
    run_git(repo, ["add", "--", rel], check=True)
    remaining = git_status_fields(repo).get("conflicts") or []
    if not remaining:
        # Finish the merge if one is in progress and everything is staged.
        if (repo / ".git" / "MERGE_HEAD").exists():
            ensure_git_identity(repo)
            run_git(repo, ["commit", "--no-edit"], timeout=20)
    snap = build_snapshot(ctx, fetch=False)
    snap["resolved"] = rel
    snap["side"] = label
    snap["remaining_conflicts"] = remaining
    snap["message"] = f"Resolved {rel} using {label}."
    return snap


def cmd_set_url(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    repo = configured_repo(ctx)
    source_raw = ""
    if getattr(args, "stdin", False):
        source_raw = sys.stdin.read(MAX_URL_INPUT_BYTES).strip()
    if not source_raw:
        source_raw = " ".join(args.args).strip() or (args.url or "")
    kind, value = normalize_source(source_raw)
    if kind != "url":
        raise SyncError("set-url expects a git remote URL.")
    clean_url, cred_file = prepare_git_credentials(ctx, value)
    result = run_git(repo, ["remote", "get-url", "origin"])
    if result.returncode != 0:
        run_git(repo, ["remote", "add", "origin", clean_url], check=True)
    else:
        run_git(repo, ["remote", "set-url", "origin", clean_url], check=True)
    persist_git_credential_helper(repo, cred_file)
    state = load_state(ctx)
    state["repo_url"] = clean_url
    save_state(ctx, state)
    snap = build_snapshot(ctx, fetch=True)
    snap["message"] = f"Origin set to {clean_url}"
    return snap


def open_in_file_manager(target_path: str | Path) -> bool:
    target = Path(target_path).expanduser().resolve()
    if not target.exists() and target.parent.exists():
        target = target.parent
    if not target.exists():
        return False

    uri = target.as_uri()
    devnull = subprocess.DEVNULL
    dbus = shutil.which("dbus-send")
    if dbus:
        try:
            res = subprocess.run(
                [
                    dbus,
                    "--session",
                    "--type=method_call",
                    "--dest=org.freedesktop.FileManager1",
                    "/org/freedesktop/FileManager1",
                    "org.freedesktop.FileManager1.ShowItems",
                    f"array:string:{uri}",
                    "string:",
                ],
                capture_output=True,
                timeout=3,
            )
            if res.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass

    for fm, flag in [("nautilus", "--select"), ("dolphin", "--select"), ("nemo", "--no-desktop")]:
        binary = shutil.which(fm)
        if binary:
            try:
                subprocess.Popen(
                    [binary, flag, str(target)],
                    start_new_session=True,
                    stdin=devnull,
                    stdout=devnull,
                    stderr=devnull,
                    close_fds=True,
                )
                return True
            except OSError:
                pass

    xdg_open = shutil.which("xdg-open")
    if xdg_open:
        open_target = target if target.is_dir() else target.parent
        try:
            subprocess.Popen(
                [xdg_open, str(open_target)],
                start_new_session=True,
                stdin=devnull,
                stdout=devnull,
                stderr=devnull,
                close_fds=True,
            )
            return True
        except OSError:
            pass

    return False


def cmd_open(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    raw_path = args.args[0] if args.args else getattr(args, "files", None) or ""
    if not raw_path:
        raise SyncError("No path provided to open.")
    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        try:
            repo = configured_repo(ctx)
        except Exception:
            repo = ctx.default_clone
        inv = collect_inventory(ctx, repo)
        mapped = next((i for i in inv if i["path"] == raw_path), None)
        if mapped and Path(mapped["local_path"]).exists():
            target = Path(mapped["local_path"])
        elif mapped and Path(mapped["repo_path"]).exists():
            target = Path(mapped["repo_path"])
        elif (ctx.home / ".config" / raw_path).exists():
            target = ctx.home / ".config" / raw_path
        elif (ctx.local_bin / raw_path.removeprefix("bin/")).exists():
            target = ctx.local_bin / raw_path.removeprefix("bin/")
        elif (repo / raw_path).exists():
            target = repo / raw_path

    success = open_in_file_manager(target)
    return ok({"opened": str(target), "success": success})


def open_in_terminal(target_path: str | Path) -> bool:
    target = Path(target_path).expanduser().resolve()
    directory = target if target.is_dir() else target.parent
    if not directory.exists() and directory.parent.exists():
        directory = directory.parent
    if not directory.exists():
        return False

    devnull = subprocess.DEVNULL

    # 1. Try omarchy / uwsm-app + xdg-terminal-exec
    if shutil.which("uwsm-app") and shutil.which("xdg-terminal-exec"):
        try:
            subprocess.Popen(
                ["uwsm-app", "--", "xdg-terminal-exec", f"--dir={directory}"],
                start_new_session=True,
                stdin=devnull,
                stdout=devnull,
                stderr=devnull,
                close_fds=True,
            )
            return True
        except OSError:
            pass

    if shutil.which("xdg-terminal-exec"):
        try:
            subprocess.Popen(
                ["xdg-terminal-exec", f"--dir={directory}"],
                start_new_session=True,
                stdin=devnull,
                stdout=devnull,
                stderr=devnull,
                close_fds=True,
            )
            return True
        except OSError:
            pass

    # 2. Try specific terminals
    for term, flag in [("foot", "-D"), ("ghostty", "--working-directory="), ("alacritty", "--working-directory"), ("kitty", "--directory")]:
        binary = shutil.which(term)
        if binary:
            try:
                cmd = [binary, f"{flag}{directory}"] if flag.endswith("=") else [binary, flag, str(directory)]
                subprocess.Popen(
                    cmd,
                    start_new_session=True,
                    stdin=devnull,
                    stdout=devnull,
                    stderr=devnull,
                    close_fds=True,
                )
                return True
            except OSError:
                pass

    return False


def cmd_terminal(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    raw_path = args.args[0] if args.args else getattr(args, "files", None) or ""
    if not raw_path:
        raise SyncError("No path provided to open in terminal.")
    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        try:
            repo = configured_repo(ctx)
        except Exception:
            repo = ctx.default_clone
        inv = collect_inventory(ctx, repo)
        mapped = next((i for i in inv if i["path"] == raw_path), None)
        if mapped and Path(mapped["local_path"]).exists():
            target = Path(mapped["local_path"])
        elif mapped and Path(mapped["repo_path"]).exists():
            target = Path(mapped["repo_path"])
        elif (ctx.home / ".config" / raw_path).exists():
            target = ctx.home / ".config" / raw_path
        elif (ctx.local_bin / raw_path.removeprefix("bin/")).exists():
            target = ctx.local_bin / raw_path.removeprefix("bin/")
        elif (repo / raw_path).exists():
            target = repo / raw_path

    success = open_in_terminal(target)
    return ok({"opened_terminal": str(target), "success": success})


def cmd_hide(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(ctx)
    hidden = list(state.get("hidden") or [])
    keys = list(args.args)
    if args.files:
        keys.extend(p.strip() for p in args.files.split(",") if p.strip())
    if not keys:
        raise SyncError("Provide at least one item to hide.")
    for k in keys:
        if k not in hidden:
            hidden.append(k)
    state["hidden"] = hidden
    save_state(ctx, state)
    snap = build_snapshot(ctx, fetch=False)
    snap["hidden"] = hidden
    snap["message"] = f"Hidden {len(keys)} item{'s' if len(keys) != 1 else ''}."
    return snap


def cmd_unhide(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(ctx)
    hidden = list(state.get("hidden") or [])
    if getattr(args, "all", False) or "all" in args.args:
        count = len(hidden)
        state["hidden"] = []
        save_state(ctx, state)
        snap = build_snapshot(ctx, fetch=False)
        snap["hidden"] = []
        snap["message"] = f"Unhid all {count} item{'s' if count != 1 else ''}."
        return snap
    keys = list(args.args)
    if args.files:
        keys.extend(p.strip() for p in args.files.split(",") if p.strip())
    if not keys:
        raise SyncError("Provide at least one item to unhide, or use --all.")
    remove_set = set(keys)
    state["hidden"] = [
        k
        for k in hidden
        if k not in remove_set
        and f"f:{k}" not in remove_set
        and k.removeprefix("f:") not in remove_set
        and f"s:{k}" not in remove_set
        and k.removeprefix("s:") not in remove_set
        and f"g:{k}" not in remove_set
        and k.removeprefix("g:") not in remove_set
        and f"p:{k}" not in remove_set
        and k.removeprefix("p:") not in remove_set
        and f"t:{k}" not in remove_set
        and k.removeprefix("t:") not in remove_set
    ]
    save_state(ctx, state)
    snap = build_snapshot(ctx, fetch=False)
    snap["hidden"] = state["hidden"]
    snap["message"] = f"Unhid {len(keys)} item{'s' if len(keys) != 1 else ''}."
    return snap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Omarchy config-sync backend")
    parser.add_argument(
        "command",
        choices=[
            "snapshot",
            "connect",
            "disconnect",
            "apply",
            "publish",
            "pull",
            "resolve",
            "set-url",
            "inspect",
            "status",
            "resync",
            "hide",
            "unhide",
            "open",
            "terminal",
        ],
    )
    parser.add_argument("args", nargs="*")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--include-machine", action="store_true")
    parser.add_argument("--files", default=None)
    parser.add_argument("--explicit", action="store_true")
    parser.add_argument("--shortcut", action="append", default=None)
    parser.add_argument("--plugin", action="append", default=None)
    parser.add_argument("--theme", action="store_true")
    parser.add_argument("--message", default=None)
    parser.add_argument("--delete-clone", action="store_true")
    parser.add_argument("--side", default=None)
    parser.add_argument("--url", default=None)
    parser.add_argument("--stdin", action="store_true", help="Read input URL from stdin")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def dispatch(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command in {"snapshot", "status", "inspect"}:
        return cmd_snapshot(ctx, args)
    if command == "connect":
        return cmd_connect(ctx, args)
    if command == "disconnect":
        return cmd_disconnect(ctx, args)
    if command == "apply":
        return cmd_apply(ctx, args)
    if command == "publish":
        return cmd_publish(ctx, args)
    if command == "pull":
        return cmd_pull(ctx, args)
    if command == "resync":
        return cmd_resync(ctx, args)
    if command == "resolve":
        return cmd_resolve(ctx, args)
    if command == "set-url":
        return cmd_set_url(ctx, args)
    if command == "hide":
        return cmd_hide(ctx, args)
    if command == "unhide":
        return cmd_unhide(ctx, args)
    if command == "open":
        return cmd_open(ctx, args)
    if command == "terminal":
        return cmd_terminal(ctx, args)
    raise SyncError(f"Unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context.from_env()
    try:
        result = dispatch(ctx, args)
    except SyncError as exc:
        result = fail(str(exc), **exc.extra)
    except Exception as exc:  # noqa: BLE001 — CLI must never print a traceback to QML
        result = fail(str(exc) or exc.__class__.__name__)
    payload = json.dumps(result, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_RESPONSE_BYTES:
        # Enforce the bound before writing, not after the panel has buffered it all.
        result = fail("Sync response exceeded the maximum size (5MB); the repo has too much changed data to display safely.")
        payload = json.dumps(result, ensure_ascii=False)
    sys.stdout.write(payload + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
