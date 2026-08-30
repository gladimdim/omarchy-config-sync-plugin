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
            self.assertTrue((env.ctx.config_plugins / "demo.widget" / "manifest.json").is_file())
            self.assertTrue((env.ctx.local_bin / "useful-tool").is_file())
            self.assertTrue(os.access(env.ctx.local_bin / "useful-tool", os.X_OK))
            self.assertTrue(Path(applied["backup_dir"]).is_dir())

    def test_publish_local_shortcut_and_ignores_config_sync_plugin(self) -> None:
        with TempHome() as env:
            repo = make_config_repo(env.home / "cfg")
            cs.cmd_connect(env.ctx, argparse_ns(args=[str(repo)]))
            # First apply so hashes exist, then edit locally.
            cs.cmd_apply(env.ctx, argparse_ns())
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
