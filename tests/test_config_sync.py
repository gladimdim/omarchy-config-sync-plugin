#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import config_sync as cs  # noqa: E402

OMARCHY_CONFIG = Path("/home/gladimdim/Github/omarchy-config")


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_config_repo(root: Path, *, with_monitor: bool = True) -> Path:
    init_repo(root)
    write(
        root / "hypr" / "bindings.lua",
        '-- header\n'
        'o.bind("SUPER + SHIFT + R", "Region screen recording", "screenrecord-region-toggle")\n'
        'o.bind("CTRL + 9", "English layout", "hyprctl switchxkblayout all 0")\n'
        'hl.unbind("SUPER + 6")\n'
        'o.bind("SUPER + SHIFT + R", "Region screen recording", "dup")\n',
    )
    write(root / "hypr" / "looknfeel.lua", "hl.decoration({ rounding = 8 })\n")
    if with_monitor:
        write(root / "hypr" / "monitors.lua", 'hl.monitor({ output = "eDP-1" })\n')
    write(
        root / "omarchy" / "shell.json",
        json.dumps(
            {
                "version": 1,
                "idle": {"lock": 600, "screensaver": 300},
                "bar": {
                    "position": "bottom",
                    "layout": {
                        "left": [{"id": "omarchy.menu"}],
                        "center": [{"id": "omarchy.clock"}],
                        "right": [{"id": "omarchy.audio"}],
                    },
                },
                "plugins": [],
            },
            indent=2,
        )
        + "\n",
    )
    write(
        root / "plugins" / "demo.widget" / "manifest.json",
        json.dumps(
            {
                "schemaVersion": 1,
                "id": "demo.widget",
                "name": "Demo Widget",
                "version": "1.2.0",
                "description": "A demo bar widget",
                "kinds": ["bar-widget"],
                "entryPoints": {"barWidget": "Main.qml"},
            }
        ),
    )
    write(root / "plugins" / "demo.widget" / "Main.qml", "import QtQuick\nItem {}\n")
    write(root / "apply.sh", "#!/usr/bin/env bash\necho apply\n")
    write(root / "bin" / "useful-tool", "#!/usr/bin/env bash\necho hi\n")
    write(root / "terminals" / "kitty.conf", "font_size 12\n")
    write(root / "omarchy" / "hooks" / "post-update.d" / "setup-agent.hook", "#!/bin/bash\ntrue\n")
    commit_all(root, "initial omarchy config")
    return root


class TempHome:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.data = self.home / ".local" / "share"
        self.data.mkdir(parents=True)
        self.ctx = cs.Context(home=self.home, state_dir=self.data / "omarchy-config-sync", default_clone=self.data / "omarchy-config-sync" / "repo")

    def close(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> "TempHome":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class NormalizeTests(unittest.TestCase):
    def test_https(self) -> None:
        self.assertEqual(cs.normalize_source("https://github.com/a/b.git"), ("url", "https://github.com/a/b.git"))

    def test_ssh(self) -> None:
        self.assertEqual(cs.normalize_source("git@github.com:a/b.git"), ("url", "git@github.com:a/b.git"))

    def test_bare_github(self) -> None:
        self.assertEqual(cs.normalize_source("github.com/a/b"), ("url", "https://github.com/a/b"))

    def test_owner_repo_shorthand(self) -> None:
        self.assertEqual(cs.normalize_source("gladimdim/omarchy-config"), ("url", "https://github.com/gladimdim/omarchy-config.git"))

    def test_empty(self) -> None:
        with self.assertRaises(cs.SyncError):
            cs.normalize_source("  ")

    def test_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo"
            path.mkdir()
            kind, value = cs.normalize_source(str(path))
            self.assertEqual(kind, "path")
            self.assertEqual(value, str(path.resolve()))


class ValidateTests(unittest.TestCase):
    def test_real_omarchy_config(self) -> None:
        if not OMARCHY_CONFIG.is_dir():
            self.skipTest("omarchy-config fixture missing")
        result = cs.validate_repo(OMARCHY_CONFIG)
        self.assertTrue(result["valid"], result)
        self.assertGreaterEqual(result["score"], 5)
        self.assertTrue(result["has_shell"])
        self.assertIn("gladimdim.hardware.info", result["plugin_ids"])

    def test_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = cs.validate_repo(Path(tmp))
            self.assertFalse(result["valid"])

    def test_random_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "README.md", "hello")
            write(Path(tmp) / "src" / "main.py", "print(1)\n")
            self.assertFalse(cs.validate_repo(Path(tmp))["valid"])

    def test_marker_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / cs.MARKER_NAME, json.dumps({"format": cs.MARKER_FORMAT, "version": 1}))
            result = cs.validate_repo(Path(tmp))
            self.assertTrue(result["valid"])

    def test_mini_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_config_repo(Path(tmp) / "cfg")
            result = cs.validate_repo(repo)
            self.assertTrue(result["valid"], result)


class ShortcutTests(unittest.TestCase):
    def test_parse_dedupes_and_labels(self) -> None:
        text = (OMARCHY_CONFIG / "hypr" / "bindings.lua").read_text(encoding="utf-8") if OMARCHY_CONFIG.is_dir() else (
            'o.bind("SUPER + SHIFT + R", "Region screen recording", "x")\n'
            'o.bind("SUPER + SHIFT + R", "dup", "x")\n'
            'hl.unbind("SUPER + 6")\n'
            'o.bind("CTRL + 9", nil, "hyprctl")\n'
        )
        rows = cs.parse_shortcuts(text)
        keys = [r["keys"] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        if OMARCHY_CONFIG.is_dir():
            labels = {r["keys"]: r["label"] for r in rows}
            self.assertEqual(labels["SUPER + SHIFT + R"], "Region screen recording")
            self.assertEqual(labels["CTRL + 9"], "English layout")

    def test_unbind_without_bind(self) -> None:
        rows = cs.parse_shortcuts('hl.unbind("SUPER + SHIFT + B")\n')
        self.assertEqual(rows, [{"keys": "SUPER + SHIFT + B", "label": "Unbound default", "kind": "unbind"}])


class ClassifyTests(unittest.TestCase):
    def _item(self, local: str | None, repo: str | None) -> dict:
        return {
            "local_exists": local is not None,
            "repo_exists": repo is not None,
            "local_hash": local,
            "repo_hash": repo,
        }

    def test_identical(self) -> None:
        self.assertEqual(cs.classify_file(self._item("aaa", "aaa"), "aaa"), "identical")

    def test_local_ahead(self) -> None:
        self.assertEqual(cs.classify_file(self._item("bbb", "aaa"), "aaa"), "local")

    def test_repo_ahead(self) -> None:
        self.assertEqual(cs.classify_file(self._item("aaa", "ccc"), "aaa"), "repo")

    def test_both(self) -> None:
        self.assertEqual(cs.classify_file(self._item("bbb", "ccc"), "aaa"), "both")

    def test_first_connect_differs(self) -> None:
        self.assertEqual(cs.classify_file(self._item("bbb", "aaa"), None), "differs")

    def test_added_local(self) -> None:
        self.assertEqual(cs.classify_file(self._item("bbb", None), None), "added-local")

    def test_added_repo(self) -> None:
        self.assertEqual(cs.classify_file(self._item(None, "aaa"), None), "added-repo")


class InspectAndSyncTests(unittest.TestCase):
    def test_inspect_mini_repo(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            inspect = cs.inspect_repo(env.ctx, repo)
            self.assertTrue(inspect["valid"])
            self.assertEqual(inspect["idle"]["lock"], 600)
            self.assertEqual(inspect["bar"]["position"], "bottom")
            self.assertIn("omarchy.clock", inspect["bar"]["widgets"]["center"])
            plugin_ids = [p["id"] for p in inspect["plugins"]]
            self.assertIn("demo.widget", plugin_ids)
            shortcuts = {s["keys"]: s["label"] for s in inspect["shortcuts"]}
            self.assertEqual(shortcuts["SUPER + SHIFT + R"], "Region screen recording")
            self.assertTrue(any(h["name"] == "setup-agent.hook" for h in inspect["hooks"]))
            self.assertIn("useful-tool", inspect["bins"])
            self.assertTrue(any(c["path"] == "hypr/monitors.lua" and c["portable"] is False for c in inspect["configs"]))

    def test_connect_local_and_apply_preserves_self_widget(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            # Local machine already has the sync plugin in the bar, plus different bindings.
            write(
                env.ctx.config_omarchy / "shell.json",
                json.dumps(
                    {
                        "version": 1,
                        "bar": {
                            "layout": {
                                "left": [{"id": "omarchy.menu"}],
                                "center": [],
                                "right": [{"id": "gladimdim.tray"}, {"id": cs.PLUGIN_ID, "note": "keep-me"}],
                            }
                        },
                    }
                ),
            )
            write(env.ctx.config_hypr / "bindings.lua", 'o.bind("SUPER + Q", "Old", "true")\n')
            write(env.ctx.config_hypr / "monitors.lua", 'hl.monitor({ output = "LOCAL" })\n')

            snap = cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            self.assertTrue(snap["ok"], snap)
            self.assertTrue(snap["configured"])
            self.assertEqual(snap["sync_state"], "ready")

            applied = cs.cmd_apply(env.ctx, argparse_ns())
            self.assertTrue(applied["ok"], applied)
            self.assertIn("hypr/bindings.lua", applied["applied"])
            self.assertNotIn("hypr/monitors.lua", applied["applied"])
            self.assertEqual(
                (env.ctx.config_hypr / "bindings.lua").read_text(encoding="utf-8").splitlines()[1],
                'o.bind("SUPER + SHIFT + R", "Region screen recording", "screenrecord-region-toggle")',
            )
            self.assertIn("LOCAL", (env.ctx.config_hypr / "monitors.lua").read_text(encoding="utf-8"))
            shell = json.loads((env.ctx.config_omarchy / "shell.json").read_text())
            right = [e.get("id") if isinstance(e, dict) else e for e in shell["bar"]["layout"]["right"]]
            self.assertIn(cs.PLUGIN_ID, right)
            kept = [e for e in shell["bar"]["layout"]["right"] if isinstance(e, dict) and e.get("id") == cs.PLUGIN_ID][0]
            self.assertEqual(kept.get("note"), "keep-me")
            self.assertTrue((env.ctx.config_plugins / "demo.widget" / "manifest.json").is_file())
            self.assertTrue((env.ctx.local_bin / "useful-tool").is_file())
            self.assertTrue(os.access(env.ctx.local_bin / "useful-tool", os.X_OK))
            self.assertTrue(Path(applied["backup_dir"]).is_dir())

    def test_publish_local_shortcut_and_skip_protected_plugin(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            # First apply so hashes exist, then edit locally.
            cs.cmd_apply(env.ctx, argparse_ns())
            bindings = env.ctx.config_hypr / "bindings.lua"
            text = bindings.read_text(encoding="utf-8")
            bindings.write_text(text + 'o.bind("SUPER + Y", "New shortcut", "true")\n', encoding="utf-8")
            write(env.ctx.config_plugins / cs.PLUGIN_ID / "manifest.json", json.dumps({"id": cs.PLUGIN_ID}))

            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertEqual(snap["sync_state"], "local-ahead", snap["status"])
            published = cs.cmd_publish(env.ctx, argparse_ns())
            self.assertTrue(published["ok"], published)
            self.assertIn("hypr/bindings.lua", published["published"])
            repo_bindings = (repo / "hypr" / "bindings.lua").read_text(encoding="utf-8")
            self.assertIn("SUPER + Y", repo_bindings)
            self.assertFalse((repo / "plugins" / cs.PLUGIN_ID).exists())
            self.assertTrue(published["committed"])
            log = git(repo, "log", "-1", "--pretty=%s").stdout
            self.assertIn("Sync config from", log)

    def test_both_changed_requires_explicit_files(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_apply(env.ctx, argparse_ns())
            # Local and repo both edit bindings after the snapshot hashes were stored.
            write(env.ctx.config_hypr / "bindings.lua", 'o.bind("SUPER + L", "Local", "true")\n')
            write(repo / "hypr" / "bindings.lua", 'o.bind("SUPER + R", "Repo", "true")\n')
            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            statuses = {f["path"]: f["status"] for f in snap["diff"]["files"]}
            self.assertEqual(statuses["hypr/bindings.lua"], "both")
            with self.assertRaises(cs.SyncError) as raised:
                cs.cmd_apply(env.ctx, argparse_ns())
            self.assertIn("both", str(raised.exception).lower())
            forced = cs.cmd_apply(env.ctx, argparse_ns(files="hypr/bindings.lua"))
            self.assertTrue(forced["ok"], forced)
            self.assertIn("SUPER + R", (env.ctx.config_hypr / "bindings.lua").read_text(encoding="utf-8"))

    def test_include_machine_applies_monitors(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            write(env.ctx.config_hypr / "monitors.lua", "LOCAL\n")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_apply(env.ctx, argparse_ns(include_machine=True, files="hypr/monitors.lua"))
            self.assertIn("eDP-1", (env.ctx.config_hypr / "monitors.lua").read_text(encoding="utf-8"))

    def test_disconnect_keeps_existing_clone(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_disconnect(env.ctx, argparse_ns(delete_clone=True))
            self.assertTrue(repo.is_dir())
            self.assertFalse(env.ctx.state_path.exists())

    def test_reject_non_config_repo(self) -> None:
        with TempHome() as env:
            junk = env.home / "junk"
            init_repo(junk)
            write(junk / "README.md", "nope")
            commit_all(junk, "readme")
            with self.assertRaises(cs.SyncError):
                cs.cmd_connect(env.ctx, argparse_ns(args=[str(junk)]))

    def test_clone_from_local_git_url(self) -> None:
        with TempHome() as env:
            origin = make_config_repo(env.home / "origin")
            snap = cs.cmd_connect(env.ctx, argparse_ns(args=[str(origin)]))
            # Local path uses the existing clone in place.
            self.assertTrue(snap["status"]["using_existing_clone"])
            # Connecting via file URL clones into XDG.
            other = TempHome()
            try:
                remote = make_config_repo(other.home / "origin")
                url_snap = cs.cmd_connect(env.ctx, argparse_ns(args=[f"file://{remote}"]))
                self.assertTrue(url_snap["ok"], url_snap)
                self.assertFalse(url_snap["status"]["using_existing_clone"])
                self.assertTrue(Path(url_snap["status"]["clone_path"]).is_dir())
                self.assertTrue((Path(url_snap["status"]["clone_path"]) / "hypr" / "bindings.lua").is_file())
            finally:
                other.close()


class CliTests(unittest.TestCase):
    def test_snapshot_not_configured(self) -> None:
        with TempHome() as env:
            os.environ["HOME"] = str(env.home)
            os.environ["XDG_DATA_HOME"] = str(env.data)
            code = cs.main(["snapshot"])
            # main writes to stdout; we just care it does not crash
            self.assertIn(code, (0, 1))


def argparse_ns(**kwargs):
    class N:
        fetch = False
        push = False
        include_machine = False
        files = None
        message = None
        delete_clone = False
        side = None
        url = None
        dry_run = True
        args = []

    n = N()
    for k, v in kwargs.items():
        setattr(n, k, v)
    return n


class RealRepoInspect(unittest.TestCase):
    def test_inspect_live_omarchy_config(self) -> None:
        if not OMARCHY_CONFIG.is_dir():
            self.skipTest("omarchy-config fixture missing")
        with TempHome() as env:
            inspect = cs.inspect_repo(env.ctx, OMARCHY_CONFIG)
            self.assertTrue(inspect["valid"])
            self.assertGreater(len(inspect["shortcuts"]), 3)
            self.assertGreater(len(inspect["plugins"]), 3)
            ids = [p["id"] for p in inspect["plugins"]]
            self.assertIn("gladimdim.hardware.info", ids)
            self.assertIn("ranjithraj.news-reader", ids)


if __name__ == "__main__":
    unittest.main()
