#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
        'hl.unbind("SUPER + 6")\n',
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
                "version": "1.2.4",
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


def attach_plugin(env: TempHome) -> Path:
    plugin = env.home / ".config" / "omarchy" / "plugins" / cs.PLUGIN_ID
    plugin.mkdir(parents=True, exist_ok=True)
    env.ctx.plugin_root = plugin
    return plugin


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
            self.assertTrue(cs.is_seedable_empty(Path(tmp)))

    def test_readme_only_is_seedable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "README.md", "# mine\n")
            write(root / "LICENSE", "MIT\n")
            self.assertTrue(cs.is_seedable_empty(root))
            self.assertFalse(cs.validate_repo(root)["valid"])

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
        text = (
            'o.bind("SUPER + SHIFT + R", "Region screen recording", "x")\n'
            'o.bind("SUPER + SHIFT + R", "dup", "x")\n'
            'hl.unbind("SUPER + 6")\n'
            'o.bind("CTRL + 9", nil, "hyprctl")\n'
        )
        rows = cs.parse_shortcuts(text)
        keys = [r["keys"] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        labels = {r["keys"]: r["label"] for r in rows}
        self.assertEqual(labels["SUPER + SHIFT + R"], "dup")
        self.assertEqual(labels["CTRL + 9"], "Custom binding")

    def test_unbind_then_bind_keeps_the_bind(self) -> None:
        rows = cs.extract_bind_statements(
            'hl.unbind("XF86MonBrightnessUp")\n'
            'o.bind("XF86MonBrightnessUp", "Brightness up", "up")\n'
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "bind")
        self.assertEqual(rows[0]["label"], "Brightness up")
        self.assertIn("o.bind", rows[0]["raw"])

    def test_unbind_without_bind(self) -> None:
        rows = cs.parse_shortcuts('hl.unbind("SUPER + SHIFT + B")\n')
        self.assertEqual(rows, [{"keys": "SUPER + SHIFT + B", "label": "Unbound default", "kind": "unbind"}])

    def test_rebind_vs_unbind_only_is_a_shortcut_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.lua"
            repo = Path(tmp) / "repo.lua"
            write(
                local,
                'hl.unbind("XF86MonBrightnessUp")\n'
                'o.bind("XF86MonBrightnessUp", "Brightness up", "up")\n',
            )
            write(repo, 'hl.unbind("XF86MonBrightnessUp")\n')
            stored = cs.file_hash(local, "hypr/bindings.lua")
            rows = {r["keys"]: r for r in cs.shortcut_diff(local, repo, stored)}
            self.assertIn("XF86MonBrightnessUp", rows)
            self.assertEqual(rows["XF86MonBrightnessUp"]["status"], "repo")
            self.assertEqual(rows["XF86MonBrightnessUp"]["repo_label"], "Unbound default")
            self.assertEqual(rows["XF86MonBrightnessUp"]["local_label"], "Brightness up")

    def test_comment_only_bindings_file_has_no_shortcut_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.lua"
            repo = Path(tmp) / "repo.lua"
            write(local, '-- local note\no.bind("SUPER + A", "Alpha", "a")\n')
            write(repo, '-- repo note\no.bind("SUPER + A", "Alpha", "a")\n')
            stored = cs.file_hash(local, "hypr/bindings.lua")
            self.assertEqual(cs.shortcut_diff(local, repo, stored), [])
            self.assertNotEqual(cs.file_hash(local, "hypr/bindings.lua"), cs.file_hash(repo, "hypr/bindings.lua"))

    def test_comment_only_bindings_does_not_report_incoming(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            applied = cs.cmd_apply(env.ctx, argparse_ns())
            self.assertTrue(applied["ok"], applied)
            # Plugins/hooks/bin run code, so they need a separate explicit opt-in apply.
            bundle_applied = cs.cmd_apply(
                env.ctx,
                argparse_ns(
                    explicit=True,
                    files="bin/useful-tool,omarchy/hooks/post-update.d/setup-agent.hook",
                    plugin=["demo.widget"],
                ),
            )
            self.assertTrue(bundle_applied["ok"], bundle_applied)
            original = (repo / "hypr" / "bindings.lua").read_text(encoding="utf-8")
            write(repo / "hypr" / "bindings.lua", "-- only a comment changed\n" + original)
            commit_all(repo, "comment-only bindings")
            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertEqual(snap["sync_state"], "in-sync", snap["status"].get("counts"))
            bindings = next(f for f in snap["diff"]["files"] if f["path"] == "hypr/bindings.lua")
            self.assertEqual(bindings["status"], "identical")
            self.assertEqual(snap["diff"]["shortcuts"], [])
            self.assertEqual(snap["status"]["repo_changes"], 0)

    def test_incoming_changed_and_added_are_not_both(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.lua"
            repo = Path(tmp) / "repo.lua"
            write(
                local,
                'o.bind("SUPER + A", "Alpha", "a")\n'
                'o.bind("SUPER + B", "Beta", "b")\n'
                'o.bind("SUPER + C", "Gamma", "c")\n',
            )
            write(
                repo,
                'o.bind("SUPER + A", "Alpha 2", "a2")\n'
                'o.bind("SUPER + B", "Beta", "b")\n'
                'o.bind("SUPER + C", "Gamma 2", "c2")\n'
                'o.bind("SUPER + D", "Delta", "d")\n'
                'o.bind("SUPER + E", "Epsilon", "e")\n',
            )
            stored = cs.file_hash(local, "hypr/bindings.lua")
            rows = {r["keys"]: r for r in cs.shortcut_diff(local, repo, stored)}
            self.assertEqual(rows["SUPER + A"]["status"], "repo")
            self.assertEqual(rows["SUPER + A"]["change"], "changed")
            self.assertNotIn("SUPER + B", rows)
            self.assertEqual(rows["SUPER + C"]["status"], "repo")
            self.assertEqual(rows["SUPER + D"]["status"], "added-repo")
            self.assertEqual(rows["SUPER + E"]["status"], "added-repo")
            self.assertEqual(rows["SUPER + D"]["change"], "added")

    def test_apply_cherry_picks_incoming_shortcuts(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            write(
                env.ctx.config_hypr / "bindings.lua",
                'o.bind("SUPER + A", "Alpha", "a")\n'
                'o.bind("SUPER + C", "Gamma", "c")\n',
            )
            write(
                repo / "hypr" / "bindings.lua",
                'o.bind("SUPER + A", "Alpha 2", "a2")\n'
                'o.bind("SUPER + C", "Gamma", "c")\n'
                'o.bind("SUPER + D", "Delta", "d")\n'
                'o.bind("SUPER + E", "Epsilon", "e")\n',
            )
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            applied = cs.cmd_apply(
                env.ctx,
                argparse_ns(explicit=True, files="", shortcut=["SUPER + A", "SUPER + D"]),
            )
            self.assertTrue(applied["ok"], applied)
            text = (env.ctx.config_hypr / "bindings.lua").read_text(encoding="utf-8")
            self.assertIn("Alpha 2", text)
            self.assertIn("Delta", text)
            self.assertNotIn("Epsilon", text)
            self.assertIn("Gamma", text)

    def test_upsert_keeps_other_binds(self) -> None:
        dest = (
            'o.bind("SUPER + A", "Alpha", "a")\n'
            'o.bind("SUPER + B", "Beta", "b")\n'
        )
        src = extract_map(
            'o.bind("SUPER + B", "Beta 2", "b2")\n'
            'o.bind("SUPER + C", "Gamma", "c")\n'
        )
        merged = cs.upsert_shortcut_lines(dest, src, ["SUPER + B", "SUPER + C"])
        self.assertIn('o.bind("SUPER + A", "Alpha", "a")', merged)
        self.assertIn('o.bind("SUPER + B", "Beta 2", "b2")', merged)
        self.assertIn('o.bind("SUPER + C", "Gamma", "c")', merged)
        self.assertNotIn('o.bind("SUPER + B", "Beta", "b")', merged)

    def test_upsert_keeps_unbind_when_replacing_bind(self) -> None:
        dest = (
            'hl.unbind("SUPER + SHIFT + C")\n'
            'o.bind("SUPER + SHIFT + C", "Screenshot", "old")\n'
        )
        src = extract_map('o.bind("SUPER + SHIFT + C", "Screenshot", "new")\n')
        merged = cs.upsert_shortcut_lines(dest, src, ["SUPER + SHIFT + C"])
        self.assertIn('hl.unbind("SUPER + SHIFT + C")', merged)
        self.assertIn('o.bind("SUPER + SHIFT + C", "Screenshot", "new")', merged)
        self.assertNotIn('"old"', merged)


def extract_map(text: str) -> dict:
    return {e["keys"]: e for e in cs.extract_bind_statements(text)}


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
            self.assertTrue(Path(applied["backup_dir"]).is_dir())

            # Plugins/bin run code, so they require a separate explicit opt-in apply
            # rather than landing via the default (unselected) Apply above.
            bundle_applied = cs.cmd_apply(
                env.ctx,
                argparse_ns(explicit=True, files="bin/useful-tool", plugin=["demo.widget"]),
            )
            self.assertTrue(bundle_applied["ok"], bundle_applied)
            self.assertTrue((env.ctx.config_plugins / "demo.widget" / "manifest.json").is_file())
            self.assertTrue((env.ctx.local_bin / "useful-tool").is_file())
            self.assertTrue(os.access(env.ctx.local_bin / "useful-tool", os.X_OK))

    def test_publish_local_shortcut_and_ignores_config_sync_plugin(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            # First apply so hashes exist, then edit locally. Plugins/hooks/bin
            # run code, so they need a separate explicit opt-in apply.
            cs.cmd_apply(env.ctx, argparse_ns())
            cs.cmd_apply(
                env.ctx,
                argparse_ns(
                    explicit=True,
                    files="bin/useful-tool,omarchy/hooks/post-update.d/setup-agent.hook",
                    plugin=["demo.widget"],
                ),
            )
            bindings = env.ctx.config_hypr / "bindings.lua"
            text = bindings.read_text(encoding="utf-8")
            bindings.write_text(text + 'o.bind("SUPER + Y", "New shortcut", "true")\n', encoding="utf-8")
            write(env.ctx.config_plugins / cs.PLUGIN_ID / "manifest.json", json.dumps({"id": cs.PLUGIN_ID, "name": "Config Sync"}))

            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertEqual(snap["sync_state"], "local-ahead", snap["status"])
            published = cs.cmd_publish(env.ctx, argparse_ns())
            self.assertTrue(published["ok"], published)
            self.assertIn("hypr/bindings.lua", published["published"])
            repo_bindings = (repo / "hypr" / "bindings.lua").read_text(encoding="utf-8")
            self.assertIn("SUPER + Y", repo_bindings)
            # config-sync plugin must NOT be published to repo
            self.assertFalse((repo / "plugins" / cs.PLUGIN_ID / "manifest.json").is_file())
            self.assertTrue(published["committed"])
            log = git(repo, "log", "-1", "--pretty=%s").stdout
            self.assertIn("Sync config from", log)

    def test_self_plugin_is_ignored_from_diffs_and_bundles(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_apply(env.ctx, argparse_ns())

            # Put different content locally and on repo in config-sync plugin directory
            write(env.ctx.config_plugins / cs.PLUGIN_ID / "Panel.qml", "// local version")
            write(repo / "plugins" / cs.PLUGIN_ID / "Panel.qml", "// repo version")

            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            paths = [f["path"] for f in snap["diff"]["files"] if cs.PLUGIN_ID in f["path"]]
            self.assertEqual(paths, [])
            bundles = [b["id"] for b in snap["diff"]["bundles"] if cs.PLUGIN_ID in b["id"]]
            self.assertEqual(bundles, [])

    def test_resync_from_repo_takes_both_and_incoming(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_apply(env.ctx, argparse_ns())
            write(env.ctx.config_hypr / "bindings.lua", 'o.bind("SUPER + L", "Local", "true")\n')
            write(repo / "hypr" / "bindings.lua", 'o.bind("SUPER + R", "Repo", "true")\n')
            write(repo / "plugins" / "news.reader" / "manifest.json", '{"id":"news.reader","name":"News"}')
            result = cs.cmd_resync(env.ctx, argparse_ns(side="repo"))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result.get("resync"), "repo")
            self.assertIn("SUPER + R", (env.ctx.config_hypr / "bindings.lua").read_text(encoding="utf-8"))
            self.assertTrue((env.ctx.config_plugins / "news.reader" / "manifest.json").is_file())

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

    def test_new_plugin_is_one_bundle(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            for i in range(17):
                write(repo / "plugins" / "news.reader" / f"file{i}.qml", f"Item {{ /* {i} */ }}\n")
            write(
                repo / "plugins" / "news.reader" / "manifest.json",
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "id": "news.reader",
                        "name": "News Reader",
                        "kinds": ["bar-widget"],
                        "entryPoints": {"barWidget": "file0.qml"},
                    }
                ),
            )
            commit_all(repo, "add news plugin")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            bundles = snap["diff"]["bundles"]
            plugin_bundles = [b for b in bundles if b["kind"] == "plugin" and b["plugin_id"] == "news.reader"]
            self.assertEqual(len(plugin_bundles), 1, bundles)
            self.assertGreaterEqual(plugin_bundles[0]["changed_count"], 17)
            self.assertEqual(plugin_bundles[0]["status"], "added-repo")
            self.assertIn("New plugin", plugin_bundles[0]["summary"])
            incoming_files = [
                f["path"]
                for f in snap["diff"]["files"]
                if f["status"] == "added-repo" and not str(f["path"]).startswith("plugins/")
            ]
            self.assertNotIn("plugins/news.reader/file0.qml", incoming_files)

    def test_switch_git_repo(self) -> None:
        with TempHome() as env:
            first = make_config_repo(env.home / "first")
            second = make_config_repo(env.home / "second")
            write(second / "hypr" / "bindings.lua", 'o.bind("SUPER + Z", "Other machine", "true")\n')
            commit_all(second, "other bind")
            one = cs.cmd_connect(env.ctx, argparse_ns(args=[f"file://{first}"]))
            self.assertTrue(one["ok"], one)
            two = cs.cmd_connect(env.ctx, argparse_ns(args=[f"file://{second}"]))
            self.assertTrue(two["ok"], two)
            keys = [s["keys"] for s in two["inspect"]["shortcuts"]]
            self.assertIn("SUPER + Z", keys)
            self.assertIn(str(second), two["status"]["repo_url"])

    def test_disconnect_keeps_existing_clone(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_disconnect(env.ctx, argparse_ns(delete_clone=True))
            self.assertTrue(repo.is_dir())
            self.assertFalse(env.ctx.state_path.exists())

    def test_reinstall_forgets_linked_repo(self) -> None:
        with TempHome() as env:
            plugin = attach_plugin(env)
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            self.assertTrue(env.ctx.state_path.exists())
            self.assertTrue((plugin / cs.SESSION_FILE).is_file())
            shutil.rmtree(plugin)
            plugin.mkdir(parents=True)
            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertFalse(snap["configured"])
            self.assertFalse(env.ctx.state_path.exists())
            self.assertTrue(repo.is_dir())

    def test_reinstall_removes_managed_clone(self) -> None:
        with TempHome() as env:
            plugin = attach_plugin(env)
            origin = make_config_repo(env.home / "origin")
            snap = cs.cmd_connect(env.ctx, argparse_ns(args=[f"file://{origin}"]))
            clone = Path(snap["status"]["clone_path"])
            self.assertTrue(clone.is_dir())
            shutil.rmtree(plugin)
            plugin.mkdir(parents=True)
            after = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertFalse(after["configured"])
            self.assertFalse(clone.exists())
            self.assertTrue(origin.is_dir())

    def test_upgrade_without_session_keeps_linked_repo(self) -> None:
        with TempHome() as env:
            plugin = attach_plugin(env)
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            (plugin / cs.SESSION_FILE).unlink()
            state = json.loads(env.ctx.state_path.read_text(encoding="utf-8"))
            state.pop("plugin_instance", None)
            env.ctx.state_path.write_text(json.dumps(state), encoding="utf-8")
            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertTrue(snap["configured"])
            self.assertTrue(env.ctx.state_path.exists())
            bound = json.loads(env.ctx.state_path.read_text(encoding="utf-8"))
            self.assertTrue(bound.get("plugin_instance"))

    def test_reject_non_config_repo(self) -> None:
        with TempHome() as env:
            junk = env.home / "junk"
            init_repo(junk)
            write(junk / "README.md", "nope")
            write(junk / "src" / "main.py", "print(1)\n")
            commit_all(junk, "readme")
            with self.assertRaises(cs.SyncError):
                cs.cmd_connect(env.ctx, argparse_ns(args=[str(junk)]))

    def test_sync_selected_theme_and_overlay(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            write(env.ctx.theme_name_path, "catppuccin\n")
            write(env.ctx.user_themes / "catppuccin" / "colors.toml", 'background = "#111111"\n')
            write(env.ctx.user_themes / "catppuccin" / "preview.png", "not-synced")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            published = cs.cmd_publish(env.ctx, argparse_ns(explicit=True, files="", theme=True))
            self.assertTrue(published["ok"], published)
            self.assertIn("omarchy/theme.name", published["published"])
            self.assertEqual((repo / "omarchy" / "theme.name").read_text(encoding="utf-8").strip(), "catppuccin")
            self.assertTrue((repo / "omarchy" / "themes" / "catppuccin" / "colors.toml").is_file())
            self.assertFalse((repo / "omarchy" / "themes" / "catppuccin" / "preview.png").exists())
            # Incoming apply onto a machine still on tokyo-night
            write(env.ctx.theme_name_path, "tokyo-night\n")
            applied = cs.cmd_apply(env.ctx, argparse_ns(explicit=True, files="", theme=True))
            self.assertTrue(applied["ok"], applied)
            self.assertEqual(env.ctx.theme_name_path.read_text(encoding="utf-8").strip(), "catppuccin")
            self.assertIn("background", (env.ctx.user_themes / "catppuccin" / "colors.toml").read_text())
            inspect = cs.inspect_repo(env.ctx, repo)
            self.assertEqual(inspect["theme"]["slug"], "catppuccin")
            self.assertEqual(inspect["theme"]["display"], "Catppuccin")

    def test_cherrypick_one_shortcut_and_one_plugin(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_apply(env.ctx, argparse_ns())
            bindings = env.ctx.config_hypr / "bindings.lua"
            bindings.write_text(
                bindings.read_text(encoding="utf-8")
                + 'o.bind("SUPER + Y", "Only this", "true")\n'
                + 'o.bind("SUPER + Z", "Leave this", "true")\n',
                encoding="utf-8",
            )
            write(env.ctx.config_plugins / "demo.widget" / "Main.qml", "import QtQuick\nItem { objectName: \"changed\" }\n")
            write(
                env.ctx.config_plugins / "other.widget" / "manifest.json",
                json.dumps({"schemaVersion": 1, "id": "other.widget", "name": "Other", "kinds": ["bar-widget"], "entryPoints": {"barWidget": "Main.qml"}}),
            )
            write(env.ctx.config_plugins / "other.widget" / "Main.qml", "Item {}\n")
            published = cs.cmd_publish(
                env.ctx,
                argparse_ns(explicit=True, files="", shortcut=["SUPER + Y"], plugin=["other.widget"]),
            )
            self.assertTrue(published["ok"], published)
            repo_bind = (repo / "hypr" / "bindings.lua").read_text(encoding="utf-8")
            self.assertIn("Only this", repo_bind)
            self.assertNotIn("Leave this", repo_bind)
            self.assertTrue((repo / "plugins" / "other.widget" / "manifest.json").is_file())
            self.assertNotIn("changed", (repo / "plugins" / "demo.widget" / "Main.qml").read_text(encoding="utf-8"))

    def test_connect_empty_repo_and_publish_seeds(self) -> None:
        with TempHome() as env:
            empty = env.home / "empty"
            init_repo(empty)
            write(empty / "README.md", "# my private omarchy config\n")
            commit_all(empty, "Initial commit")
            write(env.ctx.config_hypr / "bindings.lua", 'o.bind("SUPER + Y", "Seeded shortcut", "true")\n')
            write(
                env.ctx.config_omarchy / "shell.json",
                json.dumps({"version": 1, "bar": {"layout": {"right": [{"id": "omarchy.audio"}]}}}),
            )
            snap = cs.cmd_connect(env.ctx, argparse_ns(args=[str(empty)]))
            self.assertTrue(snap["ok"], snap)
            self.assertEqual(snap["sync_state"], "empty")
            self.assertTrue(snap.get("empty") or snap["status"].get("empty"))
            self.assertEqual(snap["inspect"]["source"], "local")
            labels = {s["keys"]: s["label"] for s in snap["inspect"]["shortcuts"]}
            self.assertEqual(labels["SUPER + Y"], "Seeded shortcut")
            published = cs.cmd_publish(env.ctx, argparse_ns())
            self.assertTrue(published["ok"], published)
            self.assertIn("hypr/bindings.lua", published["published"])
            self.assertIn("SUPER + Y", (empty / "hypr" / "bindings.lua").read_text(encoding="utf-8"))
            self.assertTrue((empty / cs.MARKER_NAME).is_file())
            after = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertNotEqual(after["sync_state"], "empty")

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
            from io import StringIO
            from unittest.mock import patch
            buf = StringIO()
            with patch("sys.stdout", buf):
                code = cs.main(["snapshot"])
            payload = json.loads(buf.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["configured"])


def argparse_ns(**kwargs):
    class N:
        fetch = False
        push = False
        include_machine = False
        files = None
        explicit = False
        shortcut = None
        plugin = None
        theme = False
        message = None
        delete_clone = False
        side = None
        url = None
        stdin = False
        all = False
        dry_run = True
        args = []

    n = N()
    for k, v in kwargs.items():
        setattr(n, k, v)
    return n


class HideTests(unittest.TestCase):
    def test_hide_and_unhide_file(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            snap = cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            self.assertTrue(snap["configured"])
            initial_repo_changes = snap["status"]["repo_changes"]
            self.assertGreater(initial_repo_changes, 0)

            # Hide looknfeel
            hide_snap = cs.cmd_hide(env.ctx, argparse_ns(args=["f:hypr/looknfeel.lua"]))
            self.assertIn("f:hypr/looknfeel.lua", hide_snap["hidden"])
            self.assertEqual(hide_snap["status"]["repo_changes"], initial_repo_changes - 1)

            # Check that item is marked hidden
            looknfeel_item = next(f for f in hide_snap["diff"]["files"] if f["path"] == "hypr/looknfeel.lua")
            self.assertTrue(looknfeel_item["hidden"])

            # Unhide looknfeel
            unhide_snap = cs.cmd_unhide(env.ctx, argparse_ns(args=["f:hypr/looknfeel.lua"]))
            self.assertNotIn("f:hypr/looknfeel.lua", unhide_snap["hidden"])
            self.assertEqual(unhide_snap["status"]["repo_changes"], initial_repo_changes)

    def test_hide_bundle(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            snap = cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            hide_snap = cs.cmd_hide(env.ctx, argparse_ns(args=["g:plugin:demo.widget"]))
            self.assertIn("g:plugin:demo.widget", hide_snap["hidden"])

            # Files inside plugin should be considered hidden
            qml_item = next(f for f in hide_snap["diff"]["files"] if f["path"] == "plugins/demo.widget/Main.qml")
            self.assertTrue(qml_item["hidden"])

            bundle_item = next(b for b in hide_snap["diff"]["bundles"] if b["id"] == "plugin:demo.widget")
            self.assertTrue(bundle_item["hidden"])

    def test_hide_shortcut(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            snap = cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            shortcuts = snap["diff"]["shortcuts"]
            if shortcuts:
                key = shortcuts[0]["keys"]
                hide_snap = cs.cmd_hide(env.ctx, argparse_ns(args=[f"s:{key}"]))
                self.assertIn(f"s:{key}", hide_snap["hidden"])
                s_item = next(s for s in hide_snap["diff"]["shortcuts"] if s["keys"] == key)
                self.assertTrue(s_item["hidden"])

    def test_unhide_all(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_hide(env.ctx, argparse_ns(args=["f:hypr/looknfeel.lua", "g:bin"]))
            state = cs.load_state(env.ctx)
            self.assertEqual(len(state.get("hidden", [])), 2)

            unhide_snap = cs.cmd_unhide(env.ctx, argparse_ns(all=True, args=[]))
            self.assertEqual(len(unhide_snap["hidden"]), 0)
            self.assertEqual(len(cs.load_state(env.ctx).get("hidden", [])), 0)

    def test_apply_skips_hidden_items(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            cs.cmd_hide(env.ctx, argparse_ns(args=["f:hypr/looknfeel.lua"]))
            # Apply all non-explicit
            apply_snap = cs.cmd_apply(env.ctx, argparse_ns(dry_run=False))
            self.assertNotIn("hypr/looknfeel.lua", apply_snap.get("applied", []))
            self.assertFalse((env.home / ".config" / "hypr" / "looknfeel.lua").is_file())


class SecurityTests(unittest.TestCase):
    def test_validate_safe_rel_path(self) -> None:
        self.assertTrue(cs.validate_safe_rel_path("hypr/looknfeel.lua"))
        self.assertTrue(cs.validate_safe_rel_path("plugins/my.plugin/manifest.json"))
        self.assertTrue(cs.validate_safe_rel_path("bin/helper-script"))
        self.assertFalse(cs.validate_safe_rel_path("../../../etc/shadow"))
        self.assertFalse(cs.validate_safe_rel_path("/etc/shadow"))
        self.assertFalse(cs.validate_safe_rel_path("hypr/../../secret"))
        self.assertFalse(cs.validate_safe_rel_path("--flag"))
        self.assertFalse(cs.validate_safe_rel_path("hypr/look\0nfeel.lua"))

    def test_normalize_source_rejects_flags(self) -> None:
        with self.assertRaises(cs.SyncError):
            cs.normalize_source("--upload-pack=evil")
        with self.assertRaises(cs.SyncError):
            cs.normalize_source("-v")
        with self.assertRaises(cs.SyncError):
            cs.normalize_source("https://github.com/you/repo\0.git")

    def test_theme_slug_validation(self) -> None:
        self.assertEqual(cs.apply_omarchy_theme("--help", dry_run=False), "Invalid theme slug")
        self.assertEqual(cs.apply_omarchy_theme("cat; rm -rf /", dry_run=False), "Invalid theme slug")
        self.assertEqual(cs.apply_omarchy_theme("-v", dry_run=False), "Invalid theme slug")

    def test_parse_files_arg_filters_unsafe(self) -> None:
        parsed = cs.parse_files_arg("hypr/looknfeel.lua, ../../../etc/passwd, bin/tool")
        self.assertEqual(parsed, {"hypr/looknfeel.lua", "bin/tool"})

    def test_copy_mapped_file_unlinks_destination_symlink(self) -> None:
        with TempHome() as env:
            target_outside = env.home / "sensitive.txt"
            target_outside.write_text("precious", encoding="utf-8")

            local_file = env.home / ".config" / "hypr" / "looknfeel.lua"
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.symlink_to(target_outside)

            repo_file = env.home / "repo" / "hypr" / "looknfeel.lua"
            repo_file.parent.mkdir(parents=True, exist_ok=True)
            repo_file.write_text("new_look_and_feel", encoding="utf-8")

            item = {
                "path": "hypr/looknfeel.lua",
                "repo_path": str(repo_file),
                "local_path": str(local_file),
            }
            cs.copy_mapped_file(item, direction="apply")

            # Local file is now a regular file, not a symlink
            self.assertFalse(local_file.is_symlink())
            self.assertEqual(local_file.read_text(encoding="utf-8"), "new_look_and_feel")
            # The target outside was untouched
            self.assertEqual(target_outside.read_text(encoding="utf-8"), "precious")



    def test_cmd_terminal(self) -> None:
        with TempHome() as env:
            local_file = env.home / ".config" / "hypr" / "looknfeel.lua"
            local_file.parent.mkdir(parents=True, exist_ok=True)
            local_file.write_text("content", encoding="utf-8")
            res = cs.cmd_terminal(env.ctx, argparse_ns(args=["hypr/looknfeel.lua"]))
            self.assertTrue(res["ok"])
            self.assertEqual(res["opened_terminal"], str(local_file))


class ModelJsTests(unittest.TestCase):
    def test_incoming_and_outgoing_plugin_isolation(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        model_path = ROOT / "Model.js"
        script = f"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync({json.dumps(str(model_path))}, 'utf8').replace(/^\\.pragma\\s+library\\s*/m, '');
const ctx = {{}};
vm.createContext(ctx);
vm.runInContext(code, ctx);

// Case 1: Newly installed local plugin
const localPluginBundle = [{{
  id: 'plugin:local.tool',
  kind: 'plugin',
  plugin_id: 'local.tool',
  name: 'local.tool',
  summary: 'New on this machine · 2 files',
  status: 'added-local',
  files: ['plugins/local.tool/manifest.json', 'plugins/local.tool/Main.qml'],
  changed_count: 2,
  default_apply: false,
  default_publish: true
}}];
const localDiffFiles = [
  {{ path: 'plugins/local.tool/manifest.json', status: 'added-local', group: 'plugin', local_exists: true, repo_exists: false, portable: true }},
  {{ path: 'plugins/local.tool/Main.qml', status: 'added-local', group: 'plugin', local_exists: true, repo_exists: false, portable: true }}
];

const inBundles1 = ctx.filesByStatus(localPluginBundle, ['repo', 'added-repo', 'differs']);
const outBundles1 = ctx.filesByStatus(localPluginBundle, ['local', 'added-local']);
const bothBundles1 = ctx.filesByStatus(localPluginBundle, ['both']);

const inItems1 = ctx.buildIncomingItems([], [], [], inBundles1, [], localDiffFiles, {{}});
const outItems1 = ctx.buildOutgoingItems([], [], [], outBundles1, [], localDiffFiles, {{}});
const bothItems1 = ctx.buildBothItems([], [], bothBundles1, [], localDiffFiles, {{}});

// Case 2: New plugin incoming from repo
const repoPluginBundle = [{{
  id: 'plugin:remote.tool',
  kind: 'plugin',
  plugin_id: 'remote.tool',
  name: 'remote.tool',
  summary: 'New plugin · 2 files',
  status: 'added-repo',
  files: ['plugins/remote.tool/manifest.json', 'plugins/remote.tool/Main.qml'],
  changed_count: 2,
  default_apply: true,
  default_publish: false
}}];
const repoDiffFiles = [
  {{ path: 'plugins/remote.tool/manifest.json', status: 'added-repo', group: 'plugin', local_exists: false, repo_exists: true, portable: true }},
  {{ path: 'plugins/remote.tool/Main.qml', status: 'added-repo', group: 'plugin', local_exists: false, repo_exists: true, portable: true }}
];

const inBundles2 = ctx.filesByStatus(repoPluginBundle, ['repo', 'added-repo', 'differs']);
const outBundles2 = ctx.filesByStatus(repoPluginBundle, ['local', 'added-local']);
const bothBundles2 = ctx.filesByStatus(repoPluginBundle, ['both']);

const inItems2 = ctx.buildIncomingItems([], [], [], inBundles2, [], repoDiffFiles, {{}});
const outItems2 = ctx.buildOutgoingItems([], [], [], outBundles2, [], repoDiffFiles, {{}});
const bothItems2 = ctx.buildBothItems([], [], bothBundles2, [], repoDiffFiles, {{}});

console.log(JSON.stringify({{
  local: {{
    incoming: inItems1.length,
    outgoing: outItems1.length,
    both: bothItems1.length,
    outId: outItems1.length > 0 ? outItems1[0].itemId : null
  }},
  repo: {{
    incoming: inItems2.length,
    outgoing: outItems2.length,
    both: bothItems2.length,
    inId: inItems2.length > 0 ? inItems2[0].itemId : null
  }}
}}));
"""
        proc = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout.strip())
        # Local-only plugin should only be in outgoing list
        self.assertEqual(data["local"]["incoming"], 0)
        self.assertEqual(data["local"]["outgoing"], 1)
        self.assertEqual(data["local"]["both"], 0)
        self.assertEqual(data["local"]["outId"], "plugin:local.tool")

        # Repo-only plugin should only be in incoming list
        self.assertEqual(data["repo"]["incoming"], 1)
        self.assertEqual(data["repo"]["outgoing"], 0)
        self.assertEqual(data["repo"]["both"], 0)
        self.assertEqual(data["repo"]["inId"], "plugin:remote.tool")


class SecurityHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cs-sec-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_symlink_refusal_on_src(self) -> None:
        target = self.tmp / "secret.txt"
        target.write_text("topsecret", encoding="utf-8")
        symlink = self.tmp / "symlink.txt"
        symlink.symlink_to(target)
        dest = self.tmp / "out.txt"

        item = {
            "path": "hypr/autostart.lua",
            "repo_path": str(symlink),
            "local_path": str(dest),
        }
        with self.assertRaises(cs.SyncError) as cm:
            cs.copy_mapped_file(item, "apply")
        self.assertIn("Refusing to copy symlink", str(cm.exception))
        self.assertFalse(dest.exists())

    def test_symlink_atomic_replacement_on_dst(self) -> None:
        src = self.tmp / "config.lua"
        src.write_text("new_config = true\n", encoding="utf-8")
        canary = self.tmp / "canary.txt"
        canary.write_text("untouched\n", encoding="utf-8")
        dst = self.tmp / "dst.lua"
        dst.symlink_to(canary)

        item = {
            "path": "hypr/config.lua",
            "repo_path": str(src),
            "local_path": str(dst),
        }
        cs.copy_mapped_file(item, "apply")
        # dst should now be a regular file, NOT a symlink
        self.assertFalse(dst.is_symlink())
        self.assertEqual(dst.read_text(encoding="utf-8"), "new_config = true\n")
        # canary must NOT have been written through
        self.assertEqual(canary.read_text(encoding="utf-8"), "untouched\n")

    def test_atomic_write_text_and_write_json_mode(self) -> None:
        target = self.tmp / "state.json"
        cs.write_json(target, {"hello": "world"})
        self.assertTrue(target.is_file())
        mode = target.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        loaded = cs.load_json(target)
        self.assertEqual(loaded, {"hello": "world"})

    def test_sanitize_url(self) -> None:
        url_with_token = "https://oauth2:ghp_secretToken123@github.com/user/repo.git"
        sanitized = cs.sanitize_url(url_with_token)
        self.assertNotIn("ghp_secretToken123", sanitized)
        self.assertEqual(sanitized, "https://***:***@github.com/user/repo.git")

        url_with_user_only = "https://ghp_secretToken123@github.com/user/repo.git"
        sanitized2 = cs.sanitize_url(url_with_user_only)
        self.assertNotIn("ghp_secretToken123", sanitized2)
        self.assertEqual(sanitized2, "https://***@github.com/user/repo.git")

    def _ctx(self) -> "cs.Context":
        return cs.Context(home=self.tmp, state_dir=self.tmp / "state", default_clone=self.tmp / "state" / "repo")

    def test_prepare_git_credentials_strips_embedded_secret(self) -> None:
        ctx = self._ctx()
        clean_url, cred_file = cs.prepare_git_credentials(
            ctx, "https://oauth2:ghp_secretToken123@github.com/user/repo.git"
        )
        self.assertEqual(clean_url, "https://github.com/user/repo.git")
        self.assertNotIn("ghp_secretToken123", clean_url)
        self.assertIsNotNone(cred_file)
        self.assertTrue(cred_file.is_file())
        mode = cred_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        # The secret lives only in the 0600 credential store, never in the clean URL.
        self.assertIn("ghp_secretToken123", cred_file.read_text(encoding="utf-8"))

    def test_prepare_git_credentials_noop_without_embedded_secret(self) -> None:
        ctx = self._ctx()
        clean_url, cred_file = cs.prepare_git_credentials(ctx, "https://github.com/user/repo.git")
        self.assertEqual(clean_url, "https://github.com/user/repo.git")
        self.assertIsNone(cred_file)

        ssh_url = "git@github.com:user/repo.git"
        clean_ssh, cred_ssh = cs.prepare_git_credentials(ctx, ssh_url)
        self.assertEqual(clean_ssh, ssh_url)
        self.assertIsNone(cred_ssh)

    def test_connect_stdin_uses_first_line_only(self) -> None:
        # Quickshell writes URL + newline and leaves stdin open. read(64KiB)
        # would consume extra bytes (or hang on a pipe); readline stops at \n.
        ctx = self._ctx()
        origin = self.tmp / "origin"
        init_repo(origin)
        write(origin / "README.md", "seed\n")
        commit_all(origin, "init")
        buf = io.StringIO(str(origin) + "\nthis-second-line-must-be-ignored\n")
        with patch("sys.stdin", buf):
            snap = cs.cmd_connect(ctx, argparse_ns(stdin=True, args=[], url=None))
        self.assertTrue(snap["ok"], snap)
        self.assertTrue(snap.get("connected") or snap["status"]["configured"])
        self.assertEqual(buf.tell(), len(str(origin)) + 1)

    def test_connect_stdin_does_not_wait_for_eof(self) -> None:
        # Reproduce the bar hang: write one line, keep the pipe open.
        with TempHome() as env:
            origin = env.home / "origin"
            init_repo(origin)
            write(origin / "README.md", "seed\n")
            commit_all(origin, "init")
            child_env = os.environ.copy()
            child_env["HOME"] = str(env.home)
            child_env["XDG_DATA_HOME"] = str(env.data)
            proc = subprocess.Popen(
                [sys.executable, "-u", str(SCRIPTS / "config_sync.py"), "connect", "--stdin"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=child_env,
            )
            assert proc.stdin is not None
            proc.stdin.write(str(origin) + "\n")
            proc.stdin.flush()
            try:
                out, err = proc.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("connect --stdin hung waiting for EOF after one line")
            self.assertEqual(proc.returncode, 0, err)
            data = json.loads(out)
            self.assertTrue(data.get("ok"), data)

    def test_cmd_connect_never_passes_credentials_as_argv(self) -> None:
        ctx = self._ctx()
        seen_argv: list[list[str]] = []
        real_popen = subprocess.Popen

        def spy(cmd, *a, **kw):
            if isinstance(cmd, list) and cmd:
                seen_argv.append([str(c) for c in cmd])
            return real_popen(cmd, *a, **kw)

        credentialed = "https://x-access-token:supersecrettoken@example-git-host.invalid/user/repo.git"
        # Not a resolvable host; just verify the argv-building path never emits the
        # raw secret, independent of whether the clone itself succeeds.
        with patch.object(cs.subprocess, "Popen", side_effect=spy):
            try:
                cs.cmd_connect(ctx, argparse_ns(args=[], stdin=False, url=credentialed))
            except cs.SyncError:
                pass
        git_cmds = [c for c in seen_argv if c and c[0] == "git"]
        self.assertTrue(git_cmds, "expected at least one git invocation to be captured")
        for cmd in seen_argv:
            self.assertNotIn("supersecrettoken", " ".join(cmd))

    def test_plugin_groups_never_default_apply_incoming(self) -> None:
        for status in ("added-repo", "repo", "differs", "both"):
            files = [{"path": "plugins/evil.plugin/Panel.qml", "status": status}]
            groups = cs.plugin_groups(files)
            self.assertEqual(len(groups), 1)
            self.assertFalse(groups[0]["default_apply"], f"status={status} must not default-apply a plugin")

    def test_file_bundles_never_default_apply_incoming(self) -> None:
        paths = [
            "omarchy/hooks/pre-apply.d/evil.sh",
            "omarchy/agents/evil-agent.md",
            "bin/evil-helper",
            "omarchy/extensions/evil-ext.js",
        ]
        for path in paths:
            bundles = cs.file_bundles([{"path": path, "status": "added-repo"}])
            self.assertEqual(len(bundles), 1)
            self.assertFalse(bundles[0]["default_apply"], f"{path} should not default-apply")

    def test_annotate_diff_default_apply_excludes_bundled_paths(self) -> None:
        self.assertTrue(cs.is_bundled_path("plugins/foo/Panel.qml"))
        self.assertTrue(cs.is_bundled_path("omarchy/hooks/pre-apply.d/x.sh"))
        self.assertTrue(cs.is_bundled_path("omarchy/agents/x.md"))
        self.assertTrue(cs.is_bundled_path("bin/x"))
        self.assertFalse(cs.is_bundled_path("hypr/bindings.lua"))

    def test_credential_store_rejects_existing_symlink(self) -> None:
        ctx = self._ctx()
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.tmp / "elsewhere.txt"
        target.write_text("do-not-touch", encoding="utf-8")
        (ctx.state_dir / ".git-credentials").symlink_to(target)
        with self.assertRaises(cs.SyncError):
            cs.prepare_git_credentials(ctx, "https://user:secret@github.com/x/y.git")
        # The symlink target must never have been written through.
        self.assertEqual(target.read_text(encoding="utf-8"), "do-not-touch")

    def test_prepare_git_credentials_rejects_crlf_injection(self) -> None:
        ctx = self._ctx()
        with self.assertRaises(cs.SyncError):
            cs.prepare_git_credentials(ctx, "https://user:pa%0d%0ahost=evil.example%0a@github.com/x/y.git")

    def test_run_bounded_caps_output_size(self) -> None:
        # The child may exit normally or be killed for overflow depending on
        # timing; either way only max_bytes of output is ever kept.
        result = cs.run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('A' * 200)"],
            timeout=5,
            max_bytes=50,
        )
        self.assertEqual(len(result.stdout), 50)

    def test_run_bounded_kills_runaway_child_before_timeout(self) -> None:
        start = time.monotonic()
        result = cs.run_bounded(
            [sys.executable, "-u", "-c", "while True: print('x' * 8192)"],
            timeout=30,
            max_bytes=10_000,
        )
        elapsed = time.monotonic() - start
        # Enforced while streaming: the writer is stopped long before the
        # 30s timeout, and nothing beyond the cap is retained.
        self.assertLess(elapsed, 10)
        self.assertLessEqual(len(result.stdout), 10_000)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("output truncated", result.stderr)

    def test_run_bounded_passes_input_and_captures_streams(self) -> None:
        result = cs.run_bounded(
            [sys.executable, "-c", "import sys; d=sys.stdin.read(); sys.stdout.write(d.upper()); sys.stderr.write('warn')"],
            input="hello",
            timeout=5,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "HELLO")
        self.assertEqual(result.stderr, "warn")

    def test_credential_store_rejects_fifo_via_descriptor_check(self) -> None:
        ctx = self._ctx()
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        os.mkfifo(ctx.state_dir / ".git-credentials")
        with self.assertRaises(cs.SyncError):
            cs.prepare_git_credentials(ctx, "https://user:secret@github.com/x/y.git")

    def test_credential_store_written_without_subprocess_and_percent_encoded(self) -> None:
        ctx = self._ctx()

        def fail_popen(*a, **kw):
            raise AssertionError("no subprocess may be spawned while storing the credential")

        with patch.object(cs.subprocess, "Popen", side_effect=fail_popen):
            clean_url, cred_file = cs.prepare_git_credentials(
                ctx, "https://oauth2:p%40ss%3Aword@github.com/user/repo.git"
            )
        self.assertEqual(clean_url, "https://github.com/user/repo.git")
        content = cred_file.read_text(encoding="utf-8")
        # Special characters in the secret round-trip percent-encoded so they
        # cannot corrupt the line-based store format.
        self.assertEqual(content, "https://oauth2:p%40ss%3Aword@github.com\n")

    def test_cred_helper_value_is_shell_quoted(self) -> None:
        value = cs._cred_helper_value(Path("/home/o d'd/.git-credentials"))
        self.assertEqual(value, "store --file='/home/o d'\\''d/.git-credentials'")
        with self.assertRaises(cs.SyncError):
            cs._cred_helper_value(Path("/tmp/bad\npath"))

    def test_iter_files_refuses_symlinked_root(self) -> None:
        real_dir = self.tmp / "real"
        real_dir.mkdir()
        (real_dir / "secret.txt").write_text("s", encoding="utf-8")
        link = self.tmp / "link"
        link.symlink_to(real_dir)
        self.assertEqual(cs.iter_files(link), [])

    def test_collect_inventory_skips_symlinked_hook_dir(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            outside = env.home / "outside"
            outside.mkdir()
            (outside / "stolen.txt").write_text("secret", encoding="utf-8")
            hooks_dir = repo / "omarchy" / "hooks"
            shutil.rmtree(hooks_dir)
            hooks_dir.symlink_to(outside)
            items = cs.collect_inventory(env.ctx, repo)
            hook_paths = [i["path"] for i in items if i["path"].startswith("omarchy/hooks/")]
            self.assertEqual(hook_paths, [])

    def test_read_text_and_load_json_cap_oversized_files(self) -> None:
        big = self.tmp / "big.lua"
        big.write_text("x" * 100, encoding="utf-8")
        with patch.object(cs, "MAX_TEXT_FILE_BYTES", 10):
            self.assertEqual(cs.read_text(big), "")
            self.assertEqual(cs.load_json(big, default={"d": 1}), {"d": 1})

    def test_theme_scripts_never_default_or_exec(self) -> None:
        # A theme checkbox must not expand to script files on apply...
        files = [
            {"path": "omarchy/themes/x/colors.conf", "group": "theme", "repo_exists": True, "local_exists": False},
            {"path": "omarchy/themes/x/evil.sh", "group": "theme", "repo_exists": True, "local_exists": False},
        ]
        self.assertEqual(cs.expand_theme_paths(files, "apply"), {"omarchy/themes/x/colors.conf"})
        # ...and a script applied outside bin//hooks/ never gets the exec bit.
        src = self.tmp / "evil.sh"
        src.write_text("#!/bin/sh\n", encoding="utf-8")
        dst = self.tmp / "dst" / "evil.sh"
        cs.copy_mapped_file({"path": "omarchy/themes/x/evil.sh", "repo_path": str(src), "local_path": str(dst)}, "apply")
        self.assertEqual(dst.stat().st_mode & 0o777, 0o600)
        bin_dst = self.tmp / "dst" / "tool"
        cs.copy_mapped_file({"path": "bin/tool", "repo_path": str(src), "local_path": str(bin_dst)}, "apply")
        self.assertEqual(bin_dst.stat().st_mode & 0o777, 0o755)

    def test_merge_shortcuts_refuses_oversized_bindings(self) -> None:
        source = self.tmp / "src.lua"
        dest = self.tmp / "dest.lua"
        source.write_text('o.bind("SUPER + A", "Alpha", "a")\n', encoding="utf-8")
        dest.write_text("-- mine\n", encoding="utf-8")
        with patch.object(cs, "MAX_TEXT_FILE_BYTES", 4):
            with self.assertRaises(cs.SyncError):
                cs.merge_shortcuts_file(dest, source, ["SUPER + A"])
        self.assertEqual(dest.read_text(encoding="utf-8"), "-- mine\n")

    def test_read_text_is_inode_bound(self) -> None:
        real = self.tmp / "real.txt"
        real.write_text("secret", encoding="utf-8")
        link = self.tmp / "link.txt"
        link.symlink_to(real)
        # A symlink leaf is refused at open time (O_NOFOLLOW), not via a
        # separate racy pathname check.
        self.assertEqual(cs.read_text(link), "")
        fifo = self.tmp / "fifo.txt"
        os.mkfifo(fifo)
        # A FIFO neither blocks the open (O_NONBLOCK) nor passes fstat S_ISREG.
        self.assertEqual(cs.read_text(fifo), "")
        self.assertEqual(cs.read_text(real), "secret")

    def test_sha256_file_refuses_non_regular_inodes(self) -> None:
        real = self.tmp / "real.bin"
        real.write_bytes(b"data")
        link = self.tmp / "link.bin"
        link.symlink_to(real)
        fifo = self.tmp / "fifo.bin"
        os.mkfifo(fifo)
        self.assertIsNone(cs.sha256_file(link))
        self.assertIsNone(cs.sha256_file(fifo))
        self.assertIsNotNone(cs.sha256_file(real))

    def test_copy_mapped_file_refuses_fifo_and_oversized_src(self) -> None:
        fifo = self.tmp / "fifo.lua"
        os.mkfifo(fifo)
        dst = self.tmp / "out" / "f.lua"
        with self.assertRaises(cs.SyncError) as cm:
            cs.copy_mapped_file({"path": "hypr/f.lua", "repo_path": str(fifo), "local_path": str(dst)}, "apply")
        self.assertIn("non-regular", str(cm.exception))
        big = self.tmp / "big.lua"
        big.write_text("x" * 100, encoding="utf-8")
        with patch.object(cs, "MAX_SYNC_FILE_BYTES", 10):
            with self.assertRaises(cs.SyncError) as cm2:
                cs.copy_mapped_file({"path": "hypr/f.lua", "repo_path": str(big), "local_path": str(dst)}, "apply")
        self.assertIn("oversized", str(cm2.exception))
        self.assertFalse(dst.exists())

    def test_copy_mapped_file_containment_bound_to_descriptor(self) -> None:
        clone = self.tmp / "clone"
        (clone / "hypr").mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "leak.lua").write_text("private", encoding="utf-8")
        # A directory symlink inside the "clone" aliases a tracked path to a
        # regular file outside it: the leaf open succeeds, but the descriptor
        # containment recheck must refuse it.
        (clone / "sub").symlink_to(outside)
        dst = self.tmp / "out" / "leak.lua"
        item = {"path": "sub/leak.lua", "repo_path": str(clone / "sub" / "leak.lua"), "local_path": str(dst)}
        with self.assertRaises(cs.SyncError) as cm:
            cs.copy_mapped_file(item, "apply", src_root=clone)
        self.assertIn("outside", str(cm.exception))
        self.assertFalse(dst.exists())
        # The same copy without escaping succeeds.
        (clone / "hypr" / "ok.lua").write_text("fine", encoding="utf-8")
        ok_dst = self.tmp / "out" / "ok.lua"
        cs.copy_mapped_file(
            {"path": "hypr/ok.lua", "repo_path": str(clone / "hypr" / "ok.lua"), "local_path": str(ok_dst)},
            "apply",
            src_root=clone,
        )
        self.assertEqual(ok_dst.read_text(encoding="utf-8"), "fine")

    def test_collect_inventory_enforces_file_cap(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            with patch.object(cs, "MAX_INVENTORY_FILES", 2):
                with self.assertRaises(cs.SyncError):
                    cs.collect_inventory(env.ctx, repo)

    def test_backup_local_refuses_sources_outside_home(self) -> None:
        ctx = self._ctx()
        outside = Path(tempfile.mkdtemp(prefix="cs-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "secret.txt").write_text("private", encoding="utf-8")
        # A symlinked parent inside $HOME aliases the "local" path to a file
        # outside the home tree; O_NOFOLLOW alone would not catch it.
        (self.tmp / "link").symlink_to(outside)
        good = self.tmp / ".config" / "hypr" / "a.lua"
        good.parent.mkdir(parents=True)
        good.write_text("keep me", encoding="utf-8")
        backup_dir = cs.backup_local(
            ctx,
            [
                {"path": "hypr/secret.txt", "local_path": str(self.tmp / "link" / "secret.txt")},
                {"path": "hypr/a.lua", "local_path": str(good)},
            ],
        )
        self.assertEqual((backup_dir / "hypr" / "a.lua").read_text(encoding="utf-8"), "keep me")
        self.assertFalse((backup_dir / "hypr" / "secret.txt").exists())
        self.assertIn("1 files", (backup_dir / "README.txt").read_text(encoding="utf-8"))

    def test_backup_local_bounds_copy_of_growing_file(self) -> None:
        ctx = self._ctx()
        big = self.tmp / ".config" / "grown.lua"
        big.parent.mkdir(parents=True)
        big.write_text("x" * 100, encoding="utf-8")
        # Simulate a file that passed the fstat size check and then grew: hand
        # backup_local a descriptor whose content exceeds the cap and assert
        # the running byte budget aborts the copy instead of spooling it all.
        def fake_open_bound(path, max_bytes=None, within=None):
            return os.open(str(big), os.O_RDONLY)

        with patch.object(cs, "MAX_SYNC_FILE_BYTES", 4), patch.object(cs, "_open_bound", fake_open_bound):
            with self.assertRaises(cs.SyncError) as cm:
                cs.backup_local(ctx, [{"path": "hypr/grown.lua", "local_path": str(big)}])
        self.assertIn("grew past the size limit", str(cm.exception))

    def test_copy_mapped_file_dest_containment_bound_to_descriptor(self) -> None:
        repo = self.tmp / "clone"
        repo.mkdir()
        outside = Path(tempfile.mkdtemp(prefix="cs-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        # A symlinked directory committed to the repo aliases the destination
        # parent to somewhere outside the clone: publish must refuse to write
        # through it, verified on the opened directory descriptor.
        (repo / "hypr").symlink_to(outside)
        src = self.tmp / ".config" / "hypr" / "b.lua"
        src.parent.mkdir(parents=True)
        src.write_text("mine", encoding="utf-8")
        item = {"path": "hypr/b.lua", "local_path": str(src), "repo_path": str(repo / "hypr" / "b.lua")}
        with self.assertRaises(cs.SyncError) as cm:
            cs.copy_mapped_file(item, "publish", src_root=self.tmp, dst_root=repo)
        self.assertIn("outside", str(cm.exception))
        self.assertEqual(list(outside.iterdir()), [])
        # A symlink that stays inside the root (dotfiles-style) still works,
        # and missing destination directories are created on the way.
        (repo / "real").mkdir()
        (repo / "alias").symlink_to(repo / "real")
        ok_item = {"path": "alias/c.lua", "local_path": str(src), "repo_path": str(repo / "alias" / "c.lua")}
        cs.copy_mapped_file(ok_item, "publish", src_root=self.tmp, dst_root=repo)
        self.assertEqual((repo / "real" / "c.lua").read_text(encoding="utf-8"), "mine")
        deep_item = {"path": "omarchy/hooks/new.d/x.hook", "local_path": str(src), "repo_path": str(repo / "omarchy" / "hooks" / "new.d" / "x.hook")}
        cs.copy_mapped_file(deep_item, "publish", src_root=self.tmp, dst_root=repo)
        self.assertEqual((repo / "omarchy" / "hooks" / "new.d" / "x.hook").read_text(encoding="utf-8"), "mine")

    def test_atomic_write_text_within_refuses_escape(self) -> None:
        root = self.tmp / "tree"
        root.mkdir()
        outside = Path(tempfile.mkdtemp(prefix="cs-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (root / "d").symlink_to(outside)
        with self.assertRaises(cs.SyncError):
            cs.atomic_write_text(root / "d" / "x.txt", "hi", within=root)
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(cs.SyncError):
            cs.atomic_write_text(outside / "y.txt", "hi", within=root)
        cs.atomic_write_text(root / "nested" / "x.txt", "hi", within=root)
        self.assertEqual((root / "nested" / "x.txt").read_text(encoding="utf-8"), "hi")

    def test_read_helpers_refuse_parent_symlink_escape_with_within(self) -> None:
        root = self.tmp / "tree"
        root.mkdir()
        outside = Path(tempfile.mkdtemp(prefix="cs-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "secret.txt").write_text("private", encoding="utf-8")
        (outside / "secret.json").write_text('{"k": 1}', encoding="utf-8")
        (root / "esc").symlink_to(outside)
        self.assertEqual(cs.read_text(root / "esc" / "secret.txt", within=root), "")
        self.assertIsNone(cs.sha256_file(root / "esc" / "secret.txt", within=root))
        self.assertEqual(cs.load_json(root / "esc" / "secret.json", default={"d": 1}, within=root), {"d": 1})
        # An internal symlink that resolves inside the root keeps working.
        (root / "realdir").mkdir()
        (root / "realdir" / "ok.txt").write_text("fine", encoding="utf-8")
        (root / "in").symlink_to(root / "realdir")
        self.assertEqual(cs.read_text(root / "in" / "ok.txt", within=root), "fine")

    def test_strip_plugin_git_dirs_refuses_symlinked_plugin_dirs(self) -> None:
        repo = self.tmp / "clone"
        (repo / "plugins").mkdir(parents=True)
        # A real plugin dir with a stray .git gets stripped...
        real = repo / "plugins" / "demo.widget"
        (real / ".git").mkdir(parents=True)
        (real / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        # ...but a symlinked plugin dir pointing at another checkout must not
        # have that checkout's .git deleted through the link.
        victim = Path(tempfile.mkdtemp(prefix="cs-victim-"))
        self.addCleanup(shutil.rmtree, victim, ignore_errors=True)
        (victim / ".git").mkdir()
        (victim / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (repo / "plugins" / "evil").symlink_to(victim)
        # A .git that is itself a symlink is left alone too.
        real2 = repo / "plugins" / "linked.git"
        real2.mkdir()
        (real2 / ".git").symlink_to(victim / ".git")
        cs.strip_plugin_git_dirs(repo)
        self.assertFalse((real / ".git").exists())
        self.assertTrue((victim / ".git" / "HEAD").is_file())
        self.assertTrue((real2 / ".git").is_symlink())

    def test_open_dir_bound_never_creates_outside_root(self) -> None:
        root = self.tmp / "tree"
        root.mkdir()
        outside = Path(tempfile.mkdtemp(prefix="cs-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (root / "esc").symlink_to(outside)
        # The old pathname mkdir(parents=True) would have created esc/sub/
        # outside the root before the containment check refused the write;
        # the descriptor-relative walk must not create anything out there.
        with self.assertRaises(cs.SyncError):
            cs.atomic_write_text(root / "esc" / "sub" / "x.txt", "hi", within=root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_purge_saved_settings_deletes_only_in_state_clones(self) -> None:
        ctx = self._ctx()
        ctx.state_dir.mkdir(parents=True)
        # Clone recorded outside state_dir is never deleted.
        keep = self.tmp / "mycheckout"
        (keep / ".git").mkdir(parents=True)
        cs.write_json(ctx.state_path, {"clone_path": str(keep)}, within=ctx.state_dir)
        self.assertFalse(cs.purge_saved_settings(ctx))
        self.assertTrue((keep / ".git").is_dir())
        # Clone inside state_dir is deleted, descriptor-relative.
        ctx.state_dir.mkdir(parents=True)
        clone = ctx.state_dir / "repo"
        (clone / ".git").mkdir(parents=True)
        cs.write_json(ctx.state_path, {"clone_path": str(clone)}, within=ctx.state_dir)
        self.assertTrue(cs.purge_saved_settings(ctx))
        self.assertFalse(clone.exists())
        self.assertFalse(ctx.state_dir.exists())

    def test_writers_survive_short_writes(self) -> None:
        real_write = os.write

        def one_byte_write(fd, data):
            # Emulate the POSIX short-write case: only one byte lands per call.
            return real_write(fd, memoryview(data)[:1])

        src = self.tmp / "src.lua"
        payload = "abcdefghij" * 300
        src.write_text(payload, encoding="utf-8")
        dst = self.tmp / "out" / "dst.lua"
        root = self.tmp / "tree"
        root.mkdir()
        with patch.object(cs.os, "write", one_byte_write):
            cs.copy_mapped_file(
                {"path": "hypr/dst.lua", "repo_path": str(src), "local_path": str(dst)},
                "apply",
                src_root=self.tmp,
                dst_root=self.tmp,
            )
            cs.atomic_write_text(root / "state.json", payload, within=root)
            cs._write_credential_store(self.tmp / "creds", "https", "github.com", "user", "s3cret" * 50)
        self.assertEqual(dst.read_text(encoding="utf-8"), payload)
        self.assertEqual((root / "state.json").read_text(encoding="utf-8"), payload)
        self.assertIn("s3cret" * 50, (self.tmp / "creds").read_text(encoding="utf-8"))

    def test_writers_fail_closed_on_zero_progress_write(self) -> None:
        src = self.tmp / "src.lua"
        src.write_text("content", encoding="utf-8")
        dst_dir = self.tmp / "out"
        dst_dir.mkdir()
        dst = dst_dir / "dst.lua"
        with patch.object(cs.os, "write", lambda fd, data: 0):
            with self.assertRaises(OSError):
                cs.copy_mapped_file(
                    {"path": "hypr/dst.lua", "repo_path": str(src), "local_path": str(dst)},
                    "apply",
                    src_root=self.tmp,
                    dst_root=self.tmp,
                )
        # Nothing installed and no temp file left behind.
        self.assertEqual(list(dst_dir.iterdir()), [])

    def test_byte_budget_bounds_aggregate_copies(self) -> None:
        budget = cs.ByteBudget(10, "Apply")
        budget.consume(6)
        budget.consume(4)
        with self.assertRaises(cs.SyncError) as cm:
            budget.consume(1)
        self.assertIn("per-operation size limit", str(cm.exception))
        # The budget is shared across copies of one operation.
        src = self.tmp / "src.lua"
        src.write_text("x" * 100, encoding="utf-8")
        shared = cs.ByteBudget(150, "Apply")
        cs.copy_mapped_file({"path": "hypr/a.lua", "repo_path": str(src), "local_path": str(self.tmp / "o" / "a.lua")}, "apply", src_root=self.tmp, dst_root=self.tmp, budget=shared)
        with self.assertRaises(cs.SyncError):
            cs.copy_mapped_file({"path": "hypr/b.lua", "repo_path": str(src), "local_path": str(self.tmp / "o" / "b.lua")}, "apply", src_root=self.tmp, dst_root=self.tmp, budget=shared)
        self.assertFalse((self.tmp / "o" / "b.lua").exists())

    def test_run_bounded_kills_child_exceeding_disk_budget(self) -> None:
        grow = self.tmp / "grow"
        grow.mkdir()
        script = "i=0; while :; do head -c 65536 /dev/zero > f$i; i=$((i+1)); done"
        started = time.monotonic()
        result = cs.run_bounded(
            ["sh", "-c", script],
            cwd=str(grow),
            timeout=30,
            disk_root=grow,
            max_disk_bytes=1024 * 1024,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("on-disk budget", result.stderr)
        # The child was stopped long before the timeout let it fill the disk.
        self.assertLess(cs._tree_disk_usage(grow), 16 * 1024 * 1024)

    def test_run_bounded_disk_budget_is_hard_even_after_fast_exit(self) -> None:
        # A child that finishes between two watcher samples is still caught by
        # the post-exit check, so the bound does not depend on poll timing.
        d = self.tmp / "d"
        d.mkdir()
        result = cs.run_bounded(
            ["sh", "-c", "head -c 300000 /dev/zero > big"],
            cwd=str(d),
            timeout=30,
            disk_root=d,
            max_disk_bytes=64 * 1024,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("on-disk budget", result.stderr)
        ok = cs.run_bounded(["sh", "-c", "echo fine > small"], cwd=str(d), timeout=30, disk_root=d / "nope", max_disk_bytes=64 * 1024)
        self.assertEqual(ok.returncode, 0)

    def test_cmd_connect_removes_clone_that_exceeds_disk_budget(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            with patch.object(cs, "MAX_REPO_DISK_BYTES", 4096):
                with self.assertRaises(cs.SyncError) as cm:
                    cs.cmd_connect(env.ctx, argparse_ns(args=[f"file://{repo}"]))
            self.assertIn("on-disk budget", str(cm.exception))
            # The incomplete managed clone was cleaned up, the source untouched.
            self.assertFalse(env.ctx.default_clone.exists())
            self.assertTrue((repo / ".git").is_dir())

    def test_collect_inventory_refuses_oversized_clone(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            with patch.object(cs, "MAX_REPO_DISK_BYTES", 1):
                with self.assertRaises(cs.SyncError) as cm:
                    cs.collect_inventory(env.ctx, repo)
            self.assertIn("on disk", str(cm.exception))

    def test_apply_refuses_selection_over_aggregate_budget(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            with patch.object(cs, "MAX_SYNC_TOTAL_BYTES", 10):
                with self.assertRaises(cs.SyncError) as cm:
                    cs.cmd_apply(env.ctx, argparse_ns())
            self.assertIn("per-operation size limit", str(cm.exception))
            # Nothing was installed or backed up.
            self.assertFalse((env.home / ".config" / "hypr" / "looknfeel.lua").exists())
            self.assertEqual([p for p in (env.home / ".config").glob("omarchy-backup.*")], [])

    def test_remove_managed_clone_never_touches_outside_checkout(self) -> None:
        ctx = self._ctx()
        ctx.state_dir.mkdir(parents=True)
        outside = self.tmp / "checkout"
        (outside / ".git").mkdir(parents=True)
        self.assertFalse(cs._remove_managed_clone(ctx, outside))
        self.assertTrue((outside / ".git").is_dir())
        inside = ctx.state_dir / "repo"
        (inside / ".git").mkdir(parents=True)
        self.assertTrue(cs._remove_managed_clone(ctx, inside))
        self.assertFalse(inside.exists())

    def test_main_enforces_response_size_cap_before_writing(self) -> None:
        huge = cs.ok({"blob": "x" * (cs.MAX_RESPONSE_BYTES + 1000)})
        buf = io.StringIO()
        with patch.object(cs, "dispatch", return_value=huge), patch("sys.stdout", buf):
            cs.main(["snapshot"])
        out = buf.getvalue()
        self.assertLess(len(out.encode("utf-8")), cs.MAX_RESPONSE_BYTES)
        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertIn("exceeded", data["error"])


class SemanticDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cs-sem-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shell_json_semantic_diff(self) -> None:
        loc = self.tmp / "local.json"
        rep = self.tmp / "repo.json"
        loc.write_text(
            json.dumps({
                "bar": {"position": "top", "transparent": False, "layout": {"left": ["omarchy.menu"]}},
                "idle": {"lock": 300},
            })
        )
        rep.write_text(
            json.dumps({
                "bar": {"position": "bottom", "transparent": True, "layout": {"left": ["omarchy.menu", "gladimdim.audio"]}},
                "idle": {"lock": 600},
            })
        )

        # Incoming from repo
        summary, changes = cs.summarize_file_diff("omarchy/shell.json", loc, rep, "repo")
        self.assertIn("Dock Bar: top → bottom", changes)
        self.assertIn("Bar transparency: off → on", changes)
        self.assertIn("Idle Lock: 5m → 10m", changes)
        self.assertIn("+audio", changes)
        self.assertIn("Dock Bar: top → bottom", summary)

        # Outgoing from local
        summary, changes = cs.summarize_file_diff("omarchy/shell.json", loc, rep, "local")
        self.assertIn("Dock Bar: bottom → top", changes)
        self.assertIn("Idle Lock: 10m → 5m", changes)

    def test_shell_toml_semantic_diff(self) -> None:
        loc = self.tmp / "local.toml"
        rep = self.tmp / "repo.toml"
        loc.write_text("[font]\nbase-size = 12\n")
        rep.write_text("[font]\nbase-size = 18\n")
        summary, changes = cs.summarize_file_diff("omarchy/shell.toml", loc, rep, "repo")
        self.assertEqual(changes, ["Font base-size: 12px → 18px"])

    def test_terminal_font_semantic_diff(self) -> None:
        loc = self.tmp / "local.config"
        rep = self.tmp / "repo.config"
        loc.write_text('font-size = 9\nfont-family = "Adwaita Mono"\n')
        rep.write_text('font-size = 12\nfont-family = "JetBrains Mono"\n')
        summary, changes = cs.summarize_file_diff("terminals/ghostty.config", loc, rep, "repo")
        self.assertIn("Font size: 9 → 12", changes)
        self.assertIn("Font: Adwaita Mono → JetBrains Mono", changes)

    def test_looknfeel_semantic_diff(self) -> None:
        loc = self.tmp / "local.lua"
        rep = self.tmp / "repo.lua"
        loc.write_text('hl.config({ general = { gaps_in = 5, gaps_out = 10, border_size = 2 }, decoration = { rounding = 8 } })\n')
        rep.write_text('hl.config({ general = { gaps_in = 0, gaps_out = 0, border_size = 1 }, decoration = { rounding = 12 } })\n')
        summary, changes = cs.summarize_file_diff("hypr/looknfeel.lua", loc, rep, "repo")
        self.assertIn("Gaps: 5/10 → 0/0", changes)
        self.assertIn("Border: 2px → 1px", changes)
        self.assertIn("Corners: 8px → 12px", changes)

    def test_input_lua_semantic_diff(self) -> None:
        loc = self.tmp / "local.lua"
        rep = self.tmp / "repo.lua"
        loc.write_text('hl.config({ input = { kb_layout = "us", touchpad = { tap_to_click = false } } })\n')
        rep.write_text('hl.config({ input = { kb_layout = "us,ua", touchpad = { tap_to_click = true } } })\n')
        summary, changes = cs.summarize_file_diff("hypr/input.lua", loc, rep, "repo")
        self.assertIn("Keyboard: us → us,ua", changes)
        self.assertIn("Tap-to-click: off → on", changes)

    def test_generic_fallback_diff(self) -> None:
        loc = self.tmp / "local.sh"
        rep = self.tmp / "repo.sh"
        loc.write_text("echo hello\n")
        rep.write_text("echo hello\necho world\necho again\n")
        summary, changes = cs.summarize_file_diff("bin/test.sh", loc, rep, "repo")
        self.assertEqual(changes, ["+2, -0 lines"])


class DivergedCloneTests(unittest.TestCase):
    """Two machines publishing in turn leaves the clone ahead AND behind."""

    def _connect_to_shared_origin(self, env: TempHome) -> tuple[Path, Path]:
        source = make_config_repo(env.home / "source")
        origin = env.home / "origin.git"
        subprocess.run(["git", "clone", "--bare", str(source), str(origin)], check=True, capture_output=True)
        snap = cs.cmd_connect(env.ctx, argparse_ns(args=[f"file://{origin}"]))
        self.assertTrue(snap["ok"], snap)
        clone = Path(snap["status"]["clone_path"])
        return origin, clone

    def _push_from_another_machine(self, env: TempHome, origin: Path, rel: str, content: str) -> None:
        other = env.home / "other"
        subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
        git(other, "config", "user.name", "Other")
        git(other, "config", "user.email", "other@example.com")
        write(other / rel, content)
        commit_all(other, "Sync config from other machine")
        git(other, "push", "origin", "main")

    def test_snapshot_auto_merges_a_clean_divergence(self) -> None:
        with TempHome() as env:
            origin, clone = self._connect_to_shared_origin(env)
            # This machine committed locally but never pushed.
            write(clone / "hypr" / "looknfeel.lua", "hl.decoration({ rounding = 12 })\n")
            commit_all(clone, "Sync config from this machine")
            # The other machine pushed a change to a different file.
            self._push_from_another_machine(env, origin, "terminals/kitty.conf", "font_size 14\n")

            before = cs.git_status_fields(clone, cs.fetch_repo(clone))
            self.assertEqual((before["ahead"], before["behind"]), (1, 1))

            snap = cs.cmd_snapshot(env.ctx, argparse_ns(fetch=True))
            self.assertTrue(snap["ok"], snap)
            self.assertEqual(snap["status"]["behind"], 0)
            self.assertEqual(snap["status"]["ahead"], 2)
            self.assertEqual(snap["status"]["conflicts"], [])
            self.assertNotEqual(snap["sync_state"], "conflicts")
            # Both sides' work survived the merge.
            self.assertIn("rounding = 12", (clone / "hypr" / "looknfeel.lua").read_text(encoding="utf-8"))
            self.assertIn("font_size 14", (clone / "terminals" / "kitty.conf").read_text(encoding="utf-8"))

    def test_apply_and_publish_are_not_blocked_by_a_clean_divergence(self) -> None:
        with TempHome() as env:
            origin, clone = self._connect_to_shared_origin(env)
            write(clone / "hypr" / "looknfeel.lua", "hl.decoration({ rounding = 12 })\n")
            commit_all(clone, "Sync config from this machine")
            self._push_from_another_machine(env, origin, "terminals/kitty.conf", "font_size 14\n")

            applied = cs.cmd_apply(env.ctx, argparse_ns(dry_run=True))
            self.assertTrue(applied["ok"], applied)

            published = cs.cmd_publish(env.ctx, argparse_ns(dry_run=True))
            self.assertTrue(published["ok"], published)

    def test_overlapping_edits_still_surface_per_file_conflicts(self) -> None:
        with TempHome() as env:
            origin, clone = self._connect_to_shared_origin(env)
            write(clone / "terminals" / "kitty.conf", "font_size 20\n")
            commit_all(clone, "Sync config from this machine")
            self._push_from_another_machine(env, origin, "terminals/kitty.conf", "font_size 14\n")

            snap = cs.cmd_snapshot(env.ctx, argparse_ns(fetch=True))
            self.assertEqual(snap["sync_state"], "conflicts")
            self.assertEqual(snap["status"]["conflicts"], ["terminals/kitty.conf"])

            resolved = cs.cmd_resolve(env.ctx, argparse_ns(args=["terminals/kitty.conf"], side="ours"))
            self.assertTrue(resolved["ok"], resolved)
            self.assertEqual(resolved["remaining_conflicts"], [])
            self.assertEqual(resolved["status"]["conflicts"], [])
            self.assertNotEqual(resolved["status"]["sync_state"], "conflicts")
            self.assertIn("font_size 20", (clone / "terminals" / "kitty.conf").read_text(encoding="utf-8"))

    def test_dirty_clone_reports_why_the_merge_was_skipped(self) -> None:
        with TempHome() as env:
            origin, clone = self._connect_to_shared_origin(env)
            self._push_from_another_machine(env, origin, "terminals/kitty.conf", "font_size 14\n")
            write(clone / "hypr" / "looknfeel.lua", "hl.decoration({ rounding = 99 })\n")

            with self.assertRaises(cs.SyncError) as caught:
                cs.cmd_publish(env.ctx, argparse_ns(dry_run=True))
            self.assertIn("uncommitted changes", str(caught.exception))


class RemovalSyncTests(unittest.TestCase):
    """Removing a plugin on one machine must reach the other machines."""

    def _install_plugin(self, env: TempHome, pid: str = "demo.widget") -> Path:
        lp = env.ctx.config_plugins / pid
        write(
            lp / "manifest.json",
            json.dumps({"schemaVersion": 1, "id": pid, "name": "Demo", "version": "1", "kinds": ["bar-widget"], "entryPoints": {"barWidget": "Main.qml"}}),
        )
        write(lp / "Main.qml", "import QtQuick\nItem {}\n")
        return lp

    def _linked(self, env: TempHome) -> tuple[Path, Path]:
        repo = make_config_repo(env.home / "cfg")
        lp = self._install_plugin(env)
        cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
        cs.cmd_publish(env.ctx, argparse_ns(dry_run=True))
        self.assertTrue((repo / "plugins" / "demo.widget" / "Main.qml").is_file())
        self.assertIn("plugins/demo.widget/Main.qml", cs.load_state(env.ctx).get("file_hashes") or {})
        return repo, lp

    def test_local_removal_is_labelled_as_a_removal_not_local_only(self) -> None:
        with TempHome() as env:
            repo, lp = self._linked(env)
            shutil.rmtree(lp)
            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            items = [f for f in snap["diff"]["files"] if f["path"].startswith("plugins/demo.widget")]
            self.assertTrue(items)
            for f in items:
                self.assertEqual(f["status"], "local")
                self.assertTrue(f["removal"])
                # A delete is never checked for you.
                self.assertFalse(f["default_publish"])
            bundle = next(b for b in snap["diff"]["bundles"] if b.get("plugin_id") == "demo.widget")
            self.assertTrue(bundle["removal"])
            self.assertEqual(bundle["summary"], "Removed here · 2 files")
            self.assertFalse(bundle["default_publish"])

    def test_publishing_a_removal_deletes_it_from_the_repo(self) -> None:
        with TempHome() as env:
            repo, lp = self._linked(env)
            shutil.rmtree(lp)
            pub = cs.cmd_publish(env.ctx, argparse_ns(explicit=True, plugin=["demo.widget"], dry_run=True))
            self.assertTrue(pub["ok"], pub)
            self.assertEqual(sorted(pub["removed"]), ["plugins/demo.widget/Main.qml", "plugins/demo.widget/manifest.json"])
            self.assertTrue(pub["committed"])
            self.assertFalse((repo / "plugins" / "demo.widget").exists(), "empty plugin dir should be pruned")
            self.assertIn("removed plugins/demo.widget", git(repo, "log", "-1", "--pretty=%B").stdout)
            # The baseline is cleared, so the rows stop reappearing forever.
            after = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertEqual([f for f in after["diff"]["files"] if f["path"].startswith("plugins/demo.widget")], [])
            self.assertNotIn("plugins/demo.widget/Main.qml", cs.load_state(env.ctx).get("file_hashes") or {})

    def test_applying_a_repo_removal_uninstalls_it_here(self) -> None:
        with TempHome() as env:
            repo, lp = self._linked(env)
            shutil.rmtree(repo / "plugins" / "demo.widget")
            commit_all(repo, "Remove demo.widget on the other machine")

            snap = cs.cmd_snapshot(env.ctx, argparse_ns())
            bundle = next(b for b in snap["diff"]["bundles"] if b.get("plugin_id") == "demo.widget")
            self.assertTrue(bundle["removal"])
            self.assertEqual(bundle["summary"], "Removed in the repo · 2 files")
            self.assertFalse(bundle["default_apply"])

            ap = cs.cmd_apply(env.ctx, argparse_ns(explicit=True, plugin=["demo.widget"], dry_run=True))
            self.assertTrue(ap["ok"], ap)
            self.assertEqual(len(ap["removed"]), 2)
            self.assertFalse(lp.exists(), "empty plugin dir should be pruned")
            # Backed up before deletion, so an accidental removal is recoverable.
            backup = Path(ap["backup_dir"])
            self.assertTrue((backup / "omarchy" / "plugins" / "demo.widget" / "Main.qml").is_file(), sorted(backup.rglob("*")))
            after = cs.cmd_snapshot(env.ctx, argparse_ns())
            self.assertEqual([f for f in after["diff"]["files"] if f["path"].startswith("plugins/demo.widget")], [])

    def test_removals_are_never_swept_up_by_a_default_publish(self) -> None:
        with TempHome() as env:
            repo, lp = self._linked(env)
            shutil.rmtree(lp)
            # No explicit selection: Publish must leave the repo copy alone.
            pub = cs.cmd_publish(env.ctx, argparse_ns(dry_run=True))
            self.assertEqual(pub.get("removed"), [])
            self.assertTrue((repo / "plugins" / "demo.widget" / "Main.qml").is_file())

    def test_removals_are_never_swept_up_by_a_default_apply(self) -> None:
        with TempHome() as env:
            repo, lp = self._linked(env)
            shutil.rmtree(repo / "plugins" / "demo.widget")
            commit_all(repo, "Remove demo.widget on the other machine")
            ap = cs.cmd_apply(env.ctx, argparse_ns(dry_run=True))
            self.assertEqual(ap.get("removed"), [])
            self.assertTrue((lp / "Main.qml").is_file())

    def test_remove_mapped_file_refuses_to_escape_its_root(self) -> None:
        with TempHome() as env:
            outside = env.home / "outside.txt"
            write(outside, "keep me\n")
            root = env.home / "root"
            root.mkdir()
            item = {"path": "x", "local_path": str(outside), "repo_path": str(outside)}
            with self.assertRaises(cs.SyncError):
                cs.remove_mapped_file(item, "apply", root)
            self.assertTrue(outside.is_file())

    def test_prune_stops_at_the_root(self) -> None:
        with TempHome() as env:
            root = env.home / "root"
            write(root / "a" / "b" / "f.txt", "x\n")
            item = {"path": "a/b/f.txt", "local_path": str(root / "a" / "b" / "f.txt"), "repo_path": ""}
            self.assertTrue(cs.remove_mapped_file(item, "apply", root))
            self.assertFalse((root / "a").exists())
            self.assertTrue(root.is_dir(), "the root itself must survive")

    def test_removing_an_already_gone_file_is_a_no_op(self) -> None:
        with TempHome() as env:
            root = env.home / "root"
            root.mkdir()
            item = {"path": "nope.txt", "local_path": str(root / "nope.txt"), "repo_path": ""}
            self.assertFalse(cs.remove_mapped_file(item, "apply", root))


if __name__ == "__main__":
    unittest.main()
