#!/usr/bin/env python3
"""Omarchy config-sync backend.

Every command prints a single JSON object to stdout. QML and tests both
speak this interface. Network git operations never prompt; they fail with
a message instead of hanging the panel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ID = "gladimdim.config-sync"
STATE_VERSION = 1
MARKER_NAME = ".omarchy-config.json"
MARKER_FORMAT = "omarchy-config"
MAX_DIFF_LINES = 48
MAX_DIFF_BYTES = 12_000
CLONE_TIMEOUT = 120
FETCH_TIMEOUT = 25
PUSH_TIMEOUT = 60

BIND_RE = re.compile(
    r"""o\.bind\(\s*"([^"]+)"\s*,\s*(?:nil|"([^"]*)")""",
    re.MULTILINE,
)
UNBIND_RE = re.compile(r"""hl\.unbind\(\s*"([^"]+)"\s*\)""")

SKIP_DIR_NAMES = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules"}
SKIP_FILE_NAMES = {".DS_Store"}
SKIP_NAME_RE = re.compile(r"\.bak(\.|$)")
PROTECTED_PLUGINS = {PLUGIN_ID}

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
}

MACHINE_LOCAL_PATHS = {"hypr/monitors.lua"}


class SyncError(Exception):
    def __init__(self, message: str, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.extra = extra or {}


@dataclass
class Context:
    home: Path
    state_dir: Path
    default_clone: Path

    @classmethod
    def from_env(cls) -> "Context":
        home = Path(os.environ.get("HOME") or Path.home()).expanduser()
        data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local/share"))
        state_dir = data / "omarchy-config-sync"
        return cls(home=home, state_dir=state_dir, default_clone=state_dir / "repo")

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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_json(path: Path, data: Any) -> None:
    ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_shell_bytes(path: Path) -> bytes | None:
    """Hash shell.json without this plugin's bar entry.

    Apply restores the tray widget after copying, so a naive hash would
    always look dirty. Stripping our own id keeps real bar edits visible.
    """
    if not path.is_file():
        return None
    data = load_json(path, default=None)
    if not isinstance(data, dict):
        try:
            return path.read_bytes()
        except OSError:
            return None
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


def file_hash(path: Path, rel: str) -> str | None:
    if rel == "omarchy/shell.json":
        data = canonical_shell_bytes(path)
        return sha256_bytes(data) if data is not None else None
    return sha256_file(path)


def is_skipped_file(name: str) -> bool:
    return name in SKIP_FILE_NAMES or bool(SKIP_NAME_RE.search(name))


def normalize_source(raw: str) -> tuple[str, str]:
    src = (raw or "").strip()
    if not src:
        raise SyncError("Paste a git URL or a local path to your Omarchy config repo.")
    if src.startswith("git@") or src.startswith("ssh://") or src.startswith("file://"):
        return "url", src
    if src.startswith("http://") or src.startswith("https://"):
        return "url", src
    if src.startswith("github.com/") or src.startswith("gitlab.com/") or src.startswith("codeberg.org/"):
        return "url", "https://" + src
    if re.fullmatch(r"[\w.-]+/[\w.-]+", src):
        return "url", f"https://github.com/{src}.git"
    path = Path(os.path.expanduser(src)).resolve()
    if path.exists():
        return "path", str(path)
    raise SyncError(
        f"Not a local path, and not a git URL: {src}. "
        "Use https://github.com/you/omarchy-config.git or ~/Github/omarchy-config."
    )


def git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = env.get("GIT_ASKPASS") or "true"
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return env


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
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=git_env(),
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and result.returncode != 0:
        raise SyncError(git_error_message(args, result))
    return result


def git_error_message(args: list[str], result: subprocess.CompletedProcess[str]) -> str:
    err = (result.stderr or result.stdout or "").strip()
    err = re.sub(r"\s+", " ", err)
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
    data = load_json(ctx.state_path, default={})
    if not isinstance(data, dict):
        return {}
    return data


def save_state(ctx: Context, state: dict[str, Any]) -> None:
    state["version"] = STATE_VERSION
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    write_json(ctx.state_path, state)


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
        marker_data = load_json(marker, default={})
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
    existing = load_json(marker_path, default={})
    data = existing if isinstance(existing, dict) else {}
    data.update({"format": MARKER_FORMAT, "version": 1, "synced_by": PLUGIN_ID})
    write_json(marker_path, data)


def summary_for(rel: str) -> str:
    if rel in FILE_SUMMARIES:
        return FILE_SUMMARIES[rel]
    if rel.startswith("plugins/"):
        name = rel.split("/")[1] if "/" in rel else rel
        return f"Plugin {name}"
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


def iter_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") and name not in {MARKER_NAME}:
                continue
            if is_skipped_file(name):
                continue
            out.append(Path(dirpath) / name)
    return out


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def terminal_map(ctx: Context) -> dict[str, Path]:
    return {
        "terminals/alacritty.toml": ctx.home / ".config" / "alacritty" / "alacritty.toml",
        "terminals/ghostty.config": ctx.home / ".config" / "ghostty" / "config",
        "terminals/kitty.conf": ctx.home / ".config" / "kitty" / "kitty.conf",
        "terminals/foot.ini": ctx.home / ".config" / "foot" / "foot.ini",
    }


def collect_inventory(ctx: Context, repo: Path) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}

    def add(rel: str, local: Path, repo_file: Path, group: str, extra: dict[str, Any] | None = None) -> None:
        if rel in items:
            return
        items[rel] = {
            "path": rel,
            "group": group,
            "summary": summary_for(rel),
            "portable": not is_machine_local(rel),
            "local_path": str(local),
            "repo_path": str(repo_file),
            "local_exists": local.is_file(),
            "repo_exists": repo_file.is_file(),
            "local_hash": file_hash(local, rel),
            "repo_hash": file_hash(repo_file, rel),
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
        plugin_ids.update(p.name for p in repo_plugins.iterdir() if p.is_dir() and not p.name.startswith("."))
    if ctx.config_plugins.is_dir():
        plugin_ids.update(p.name for p in ctx.config_plugins.iterdir() if p.is_dir() and not p.name.startswith("."))
    for plugin_id in sorted(plugin_ids):
        if plugin_id in PROTECTED_PLUGINS:
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


def unified_preview(local_path: Path, repo_path: Path) -> str:
    try:
        import difflib
    except Exception:
        return ""
    local_text = read_text(local_path) if local_path.is_file() else ""
    repo_text = read_text(repo_path) if repo_path.is_file() else ""
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
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    text = strip_lua_comments(text)
    for match in BIND_RE.finditer(text):
        keys = match.group(1).strip()
        label = (match.group(2) or "").strip()
        if keys in seen:
            continue
        seen.add(keys)
        if not label:
            label = "Custom binding"
        out.append({"keys": keys, "label": label, "kind": "bind"})
    for match in UNBIND_RE.finditer(text):
        keys = match.group(1).strip()
        if keys in seen:
            continue
        seen.add(keys)
        out.append({"keys": keys, "label": "Unbound default", "kind": "unbind"})
    return out


def inspect_repo(ctx: Context, repo: Path, prefer_local: bool = False) -> dict[str, Any]:
    validation = validate_repo(repo)
    hypr_root = ctx.config_hypr if prefer_local else repo / "hypr"
    omarchy_root = ctx.config_omarchy if prefer_local else repo / "omarchy"
    plugins_dir = ctx.config_plugins if prefer_local else repo / "plugins"
    shortcuts: list[dict[str, str]] = []
    bindings = hypr_root / "bindings.lua"
    if bindings.is_file():
        shortcuts = parse_shortcuts(read_text(bindings))

    plugins = []
    if plugins_dir.is_dir():
        for child in sorted(plugins_dir.iterdir()):
            manifest_path = child / "manifest.json"
            if not child.is_dir() or not manifest_path.is_file():
                continue
            if child.name in PROTECTED_PLUGINS:
                continue
            manifest = load_json(manifest_path, default={}) or {}
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
    shell = load_json(omarchy_root / "shell.json", default={}) or {}
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
    }
    for item in collect_inventory(ctx, repo):
        status = classify_file(item, stored.get(item["path"]))
        item["status"] = status
        item["default_apply"] = (
            default_apply_status(status)
            and item["portable"]
            and item["repo_exists"]
            and not item.get("git_managed")
        )
        item["default_publish"] = default_publish_status(status) and item["local_exists"]
        if status not in {"identical", "machine"}:
            item["preview"] = unified_preview(Path(item["local_path"]), Path(item["repo_path"]))
            counts["changed"] += 1
        else:
            item["preview"] = ""
        counts[status] = counts.get(status, 0) + 1
        files.append(item)
    return {"files": files, "counts": counts}


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
                "diff": {"files": [], "counts": {}},
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
    }
    return ok(
        {
            "configured": True,
            "sync_state": sync_state,
            "status": status,
            "inspect": inspect,
            "diff": diff,
        }
    )


def cmd_snapshot(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    return build_snapshot(ctx, fetch=bool(args.fetch))


def cmd_connect(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
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

    clone_path = ctx.default_clone
    existing_state = load_state(ctx)
    if clone_path.exists() and (clone_path / ".git").exists():
        current_origin = git_out(clone_path, "remote", "get-url", "origin")
        if current_origin.rstrip("/") == value.rstrip("/") or current_origin.rstrip("/").removesuffix(".git") == value.rstrip("/").removesuffix(".git"):
            fetch_error = fetch_repo(clone_path)
            if fetch_error:
                raise SyncError(fetch_error)
            run_git(clone_path, ["pull", "--ff-only"], timeout=40)
        else:
            # Different remote: move the old clone aside rather than deleting blindly.
            backup = ctx.state_dir / f"repo.bak.{int(time.time())}"
            clone_path.rename(backup)
            clone_path = ctx.default_clone
            result = run_git(None, ["clone", value, str(clone_path)], timeout=CLONE_TIMEOUT, cwd=ctx.state_dir)
            if result.returncode != 0:
                raise SyncError(git_error_message(["clone", value], result))
    else:
        if clone_path.exists():
            shutil.rmtree(clone_path)
        result = run_git(None, ["clone", value, str(clone_path)], timeout=CLONE_TIMEOUT, cwd=ctx.state_dir)
        if result.returncode != 0:
            if clone_path.exists():
                shutil.rmtree(clone_path, ignore_errors=True)
            raise SyncError(git_error_message(["clone", value], result))

    try:
        return finish_connect(ctx, clone_path, value, using_existing=False, fetch=False)
    except SyncError:
        if clone_path.exists() and not existing_state.get("using_existing_clone"):
            shutil.rmtree(clone_path, ignore_errors=True)
        raise


def finish_connect(ctx: Context, repo: Path, repo_url: str, using_existing: bool, fetch: bool) -> dict[str, Any]:
    validation = validate_repo(repo)
    empty = is_seedable_empty(repo)
    if not validation["valid"] and not empty:
        raise SyncError(
            "That git repo is not an Omarchy config repo, and it is not empty either. "
            "Use a private repo that is empty (to seed from this laptop) or one that already "
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
            "Linked an empty private repo. Review the Shortcuts, Plugins, and Configs tabs "
            "(this laptop), then Publish to seed the repo. Keep it private."
        )
    return snap


def cmd_disconnect(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    state = load_state(ctx)
    clone = Path(state.get("clone_path") or "")
    using_existing = bool(state.get("using_existing_clone"))
    deleted = False
    if args.delete_clone and clone.is_dir() and not using_existing and ctx.state_dir in clone.parents:
        shutil.rmtree(clone, ignore_errors=True)
        deleted = True
    if ctx.state_path.exists():
        ctx.state_path.unlink()
    return ok({"disconnected": True, "deleted_clone": deleted})


def copy_mapped_file(item: dict[str, Any], direction: str) -> None:
    src = Path(item["repo_path"] if direction == "apply" else item["local_path"])
    dst = Path(item["local_path"] if direction == "apply" else item["repo_path"])
    if not src.is_file():
        raise SyncError(f"Missing source file: {src}")
    ensure_parent(dst)
    shutil.copy2(src, dst)
    if direction == "apply" and (item["path"].startswith("bin/") or src.suffix == ".py" or src.suffix == ".sh" or item["path"].endswith(".hook")):
        mode = dst.stat().st_mode
        dst.chmod(mode | 0o111)


def backup_local(ctx: Context, files: list[dict[str, Any]]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ctx.home / ".config" / f"omarchy-backup.{stamp}"
    copied = 0
    for item in files:
        src = Path(item["local_path"])
        if not src.is_file():
            continue
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
        ensure_parent(dest)
        shutil.copy2(src, dest)
        copied += 1
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "README.txt").write_text(
        f"Omarchy config-sync backup of {copied} files at {now_iso()}\n", encoding="utf-8"
    )
    return backup_dir


def selected_items(diff_files: list[dict[str, Any]], wanted: set[str] | None, include_machine: bool, direction: str) -> list[dict[str, Any]]:
    chosen = []
    for item in diff_files:
        rel = item["path"]
        if wanted is not None:
            if rel not in wanted:
                continue
        else:
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


def parse_files_arg(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return set(parts) if parts else None


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


def restore_widget_entry(shell_path: Path, section: str | None, entry: dict[str, Any] | None, index: int) -> None:
    if not entry or not shell_path.is_file():
        return
    data = load_json(shell_path, default=None)
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
    write_json(shell_path, data)


def reload_desktop() -> dict[str, str]:
    notes = {}
    hypr = shutil.which("hyprctl")
    if hypr:
        result = subprocess.run([hypr, "reload"], capture_output=True, text=True, timeout=20)
        notes["hyprctl"] = "ok" if result.returncode == 0 else (result.stderr or result.stdout or "failed").strip()
    shell = shutil.which("omarchy-shell")
    if shell:
        for cmd in (["shell", "reloadConfig"], ["shell", "rescanPlugins"]):
            result = subprocess.run([shell, *cmd], capture_output=True, text=True, timeout=20)
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
    wanted = parse_files_arg(args.files)
    unresolved_both = [
        i
        for i in diff["files"]
        if i["status"] == "both" and (i["portable"] or args.include_machine) and (wanted is None or i["path"] in wanted)
    ]
    if unresolved_both and wanted is None:
        raise SyncError(
            "Some files changed on both this machine and the repo. Pick Keep local or Take repo for each, then Apply.",
            extra={"both": [i["path"] for i in unresolved_both]},
        )
    chosen = selected_items(diff["files"], wanted, bool(args.include_machine), "apply")
    if not chosen:
        snap = build_snapshot(ctx, fetch=False)
        snap["applied"] = []
        snap["message"] = "Nothing to apply."
        return snap

    shell_path = ctx.config_omarchy / "shell.json"
    section, widget_entry, widget_index = extract_widget_entry(load_json(shell_path, default={}))
    backup_dir = backup_local(ctx, [i for i in chosen if i["local_exists"]])
    applied = []
    for item in chosen:
        if not Path(item["repo_path"]).is_file():
            continue
        copy_mapped_file(item, "apply")
        applied.append(item["path"])
    restore_widget_entry(shell_path, section, widget_entry, widget_index)

    # Refresh hashes for every tracked file after apply.
    post = collect_inventory(ctx, repo)
    hashes = dict(state.get("file_hashes") or {})
    applied_set = set(applied)
    for item in post:
        live = file_hash(Path(item["local_path"]), item["path"]) if Path(item["local_path"]).is_file() else None
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
    snap = build_snapshot(ctx, fetch=False)
    snap["applied"] = applied
    snap["backup_dir"] = str(backup_dir)
    snap["reload"] = notes
    snap["message"] = f"Applied {len(applied)} file{'s' if len(applied) != 1 else ''} from the repo."
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
            "Remote has commits this clone does not. Pull/merge first so you do not overwrite another laptop.",
            extra={"ahead": git_fields["ahead"], "behind": git_fields["behind"]},
        )
    diff = annotate_diff(ctx, repo, state)
    wanted = parse_files_arg(args.files)
    unresolved_both = [
        i
        for i in diff["files"]
        if i["status"] == "both" and (i["portable"] or args.include_machine) and (wanted is None or i["path"] in wanted)
    ]
    if unresolved_both and wanted is None:
        raise SyncError(
            "Some files changed on both this machine and the repo. Pick Keep local or Take repo for each, then Publish.",
            extra={"both": [i["path"] for i in unresolved_both]},
        )
    chosen = selected_items(diff["files"], wanted, bool(args.include_machine), "publish")
    if not chosen:
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
        copy_mapped_file(item, "publish")
        published.append(item["path"])

    # Strip any accidental .git dirs copied with plugins
    plugins_dir = repo / "plugins"
    if plugins_dir.is_dir():
        for child in plugins_dir.iterdir():
            gitdir = child / ".git"
            if gitdir.exists():
                shutil.rmtree(gitdir, ignore_errors=True)

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
            live = sha256_file(Path(item["repo_path"]))
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
    source_raw = " ".join(args.args).strip()
    kind, value = normalize_source(source_raw)
    if kind != "url":
        raise SyncError("set-url expects a git remote URL.")
    result = run_git(repo, ["remote", "get-url", "origin"])
    if result.returncode != 0:
        run_git(repo, ["remote", "add", "origin", value], check=True)
    else:
        run_git(repo, ["remote", "set-url", "origin", value], check=True)
    state = load_state(ctx)
    state["repo_url"] = value
    save_state(ctx, state)
    snap = build_snapshot(ctx, fetch=True)
    snap["message"] = f"Origin set to {value}"
    return snap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Omarchy config-sync backend")
    parser.add_argument(
        "command",
        choices=["snapshot", "connect", "disconnect", "apply", "publish", "pull", "resolve", "set-url", "inspect", "status"],
    )
    parser.add_argument("args", nargs="*")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--include-machine", action="store_true")
    parser.add_argument("--files", default=None)
    parser.add_argument("--message", default=None)
    parser.add_argument("--delete-clone", action="store_true")
    parser.add_argument("--side", default=None)
    parser.add_argument("--url", default=None)
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
    if command == "resolve":
        return cmd_resolve(ctx, args)
    if command == "set-url":
        return cmd_set_url(ctx, args)
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
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
