"""End-to-end tests for the clone wizard backend.

The "new machine" is a second temporary home directory (OMARCHY_CLONE_FAKE_HOME):
every remote command runs locally under that HOME, so the whole flow is covered
without SSH and without touching this machine's desktop session.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_config_sync import TempHome, argparse_ns, commit_all, git_test_env, make_config_repo, write  # noqa: E402

import config_sync as cs  # noqa: E402
import clone_machine as cm  # noqa: E402


def ns(*argv: str):
    return cm.build_parser().parse_args(list(argv))


class CloneFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, git_test_env())
        self.env.start()
        self.addCleanup(self.env.stop)
        self.src = TempHome()
        self.addCleanup(self.src.close)
        self.repo = make_config_repo(self.src.home / "cfg")
        cs.cmd_connect(self.src.ctx, argparse_ns(args=[str(self.repo)]))
        self.target_tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.target_tmp, True)
        self.target = Path(self.target_tmp) / "home"
        (self.target / ".config" / "hypr").mkdir(parents=True)
        (self.target / ".config" / "hypr" / "looknfeel.lua").write_text("-- the new machine's own look\n")
        (self.target / ".config" / "hypr" / "only-here.lua").write_text("-- target-only file\n")
        fake = patch.dict(os.environ, {"OMARCHY_CLONE_FAKE_HOME": str(self.target)})
        fake.start()
        self.addCleanup(fake.stop)

    def preview(self) -> dict:
        res = cm.cmd_preview(self.src.ctx, ns("preview", "--target", "newbox"))
        self.assertTrue(res["ok"], res)
        return res

    def test_preview_ships_bundle_and_dry_runs_without_touching_target(self) -> None:
        before = (self.target / ".config/hypr/looknfeel.lua").read_text()
        res = self.preview()
        plan = res["plan"]
        self.assertGreater(len(plan["files"]), 0)
        self.assertIn("hypr/looknfeel.lua", [f["path"] for f in plan["files"]])
        self.assertGreaterEqual(plan["overwrite"], 1)
        self.assertEqual((self.target / ".config/hypr/looknfeel.lua").read_text(), before)
        self.assertFalse((self.target / cm.STATE_REL / "state.json").exists())
        self.assertTrue(res["not_cloned"])

    def test_clone_then_undo_round_trip(self) -> None:
        res = self.preview()
        out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                            "--confirm", res["target_hostname"]))
        self.assertTrue(out["ok"], out)
        repo_look = (self.repo / "hypr/looknfeel.lua").read_text()
        self.assertEqual((self.target / ".config/hypr/looknfeel.lua").read_text(), repo_look)
        # Files only on the new machine are kept.
        self.assertTrue((self.target / ".config/hypr/only-here.lua").is_file())
        state = json.loads((self.target / cm.STATE_REL / "state.json").read_text())
        self.assertEqual(state["lineage"]["commit"], cs.git_out(self.repo, "rev-parse", "HEAD"))
        undo_dir = Path(state["clone_undo"])
        self.assertEqual(oct(undo_dir.stat().st_mode & 0o777), "0o700")
        self.assertTrue((self.target / cm.PLUGIN_REL / "scripts/clone_machine.py").is_file())
        # The working copy on the target is removed after the clone.
        self.assertFalse((self.target / cm.REMOTE_BASE / res["runid"]).exists())

        undone = cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))
        self.assertTrue(undone["ok"], undone)
        self.assertEqual((self.target / ".config/hypr/looknfeel.lua").read_text(), "-- the new machine's own look\n")
        self.assertFalse((self.target / ".config/hypr/bindings.lua").exists())
        self.assertFalse((self.target / cm.PLUGIN_REL).exists())
        # Folders the clone made are gone too; the target's own are untouched.
        self.assertFalse((self.target / ".config/omarchy/plugins/demo.widget").exists())
        self.assertTrue((self.target / ".config/hypr").is_dir())

    def test_wallpaper_travels_and_undo_restores_the_old_background(self) -> None:
        wall = self.src.home / ".config/omarchy/backgrounds/city.jpg"
        wall.parent.mkdir(parents=True)
        wall.write_bytes(b"\xff\xd8jpeg")
        link = self.src.home / cm.BACKGROUND_LINK_REL
        link.parent.mkdir(parents=True)
        link.symlink_to(wall)
        tlink = self.target / cm.BACKGROUND_LINK_REL
        tlink.parent.mkdir(parents=True)
        old = self.target / "old-wall.png"
        old.write_bytes(b"png")
        tlink.symlink_to(old)
        res = self.preview()
        self.assertEqual(res["wallpapers"]["count"], 1)
        out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                            "--confirm", res["target_hostname"]))
        self.assertTrue(out["ok"], out)
        copied = self.target / ".config/omarchy/backgrounds/city.jpg"
        self.assertEqual(copied.read_bytes(), b"\xff\xd8jpeg")
        self.assertEqual(os.readlink(tlink), str(copied))
        cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))
        self.assertFalse(copied.exists())
        self.assertEqual(os.readlink(tlink), str(old))

    def test_private_plugin_is_copied_from_source_and_undo_removes_it(self) -> None:
        from test_config_sync import write_plugin_list, make_git_plugin
        write_plugin_list(self.repo, [{"id": "acme.secret", "name": "Secret", "version": "1.0.0",
                                       "source": "git@github.com:acme/secret.git"}])
        commit_all(self.repo, "list private plugin")
        cs.run_git(cs.configured_repo(self.src.ctx), ["pull", "--ff-only", "--quiet"])
        make_git_plugin(self.src.ctx.config_plugins / "acme.secret", "acme.secret", "1.0.0", "git@github.com:acme/secret.git")
        with patch.object(cm, "anonymous_source", return_value=""):
            res = cm.cmd_preview(self.src.ctx, ns("preview", "--target", "newbox", "--plugins", "fresh"))
        self.assertEqual(res["copy_plugins"], ["acme.secret"])
        out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                            "--confirm", res["target_hostname"]))
        self.assertTrue(out["ok"], out)
        copied = self.target / ".config/omarchy/plugins/acme.secret/manifest.json"
        self.assertTrue(copied.is_file())
        cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))
        self.assertFalse(copied.parent.exists())

    def test_exact_mode_copies_this_machines_plugin_commit(self) -> None:
        from test_config_sync import write_plugin_list, make_git_plugin
        write_plugin_list(self.repo, [{"id": "acme.pub", "name": "Pub", "version": "1.0.0",
                                       "source": "https://github.com/acme/pub.git"}])
        commit_all(self.repo, "list public plugin")
        cs.run_git(cs.configured_repo(self.src.ctx), ["pull", "--ff-only", "--quiet"])
        plugin = make_git_plugin(self.src.ctx.config_plugins / "acme.pub", "acme.pub", "1.0.0", "https://github.com/acme/pub.git")
        cs.run_git(plugin, ["update-ref", "refs/remotes/origin/main", "HEAD"])  # everything pushed
        head = cs.git_out(plugin, "rev-parse", "HEAD")
        with patch.object(cm, "anonymous_source", return_value="https://github.com/acme/pub.git"):
            res = self.preview()
        self.assertEqual((res["copy_plugins"], res["plugin_cmds"]), (["acme.pub"], []))
        out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                            "--confirm", res["target_hostname"]))
        self.assertTrue(out["ok"], out)
        copied = self.target / ".config/omarchy/plugins/acme.pub"
        self.assertEqual(cs.git_out(copied, "rev-parse", "HEAD"), head)

    def test_compiled_programs_are_never_copied(self) -> None:
        (self.repo / "bin").mkdir(exist_ok=True)
        (self.repo / "bin" / "fastthing").write_bytes(b"\x7fELF\x02\x01\x01" + b"\0" * 64)
        commit_all(self.repo, "a compiled helper")
        cs.run_git(cs.configured_repo(self.src.ctx), ["pull", "--ff-only", "--quiet"])
        res = self.preview()
        self.assertEqual(res["built"], ["bin/fastthing"])
        out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                            "--confirm", res["target_hostname"]))
        self.assertTrue(out["ok"], out)
        self.assertFalse((self.target / ".local/bin/fastthing").exists())
        self.assertTrue((self.target / ".local/bin/useful-tool").exists())  # scripts still travel

    def test_exact_mode_replaces_other_version_and_undo_restores_it(self) -> None:
        from test_config_sync import write_plugin_list, make_git_plugin
        write_plugin_list(self.repo, [{"id": "acme.pub", "name": "Pub", "version": "2.0.0",
                                       "source": "https://github.com/acme/pub.git"}])
        commit_all(self.repo, "list plugin")
        cs.run_git(cs.configured_repo(self.src.ctx), ["pull", "--ff-only", "--quiet"])
        mine = make_git_plugin(self.src.ctx.config_plugins / "acme.pub", "acme.pub", "2.0.0", "https://github.com/acme/pub.git")
        cs.run_git(mine, ["update-ref", "refs/remotes/origin/main", "HEAD"])  # everything pushed
        theirs = make_git_plugin(self.target / ".config/omarchy/plugins/acme.pub", "acme.pub", "1.0.0", "https://github.com/acme/pub.git")
        old_head = cs.git_out(theirs, "rev-parse", "HEAD")
        with patch.object(cm, "anonymous_source", return_value="https://github.com/acme/pub.git"):
            res = self.preview()
        self.assertIn("acme.pub", res["copy_plugins"])
        out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                            "--confirm", res["target_hostname"]))
        self.assertTrue(out["ok"], out)
        self.assertEqual(cs.git_out(theirs, "rev-parse", "HEAD"), cs.git_out(mine, "rev-parse", "HEAD"))
        cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))
        self.assertEqual(cs.git_out(theirs, "rev-parse", "HEAD"), old_head)

    def test_private_plugin_history_never_travels(self) -> None:
        from test_config_sync import write_plugin_list, make_git_plugin
        write_plugin_list(self.repo, [{"id": "acme.priv", "name": "Priv", "version": "1.0.0",
                                       "source": "git@github.com:acme/priv.git"}])
        commit_all(self.repo, "list private plugin")
        cs.run_git(cs.configured_repo(self.src.ctx), ["pull", "--ff-only", "--quiet"])
        make_git_plugin(self.src.ctx.config_plugins / "acme.priv", "acme.priv", "1.0.0", "git@github.com:acme/priv.git")
        with patch.object(cm, "anonymous_source", return_value=""):
            res = self.preview()
        out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                            "--confirm", res["target_hostname"]))
        self.assertTrue(out["ok"], out)
        copied = self.target / ".config/omarchy/plugins/acme.priv"
        self.assertTrue((copied / "manifest.json").is_file())
        self.assertFalse((copied / ".git").exists())

    def test_failed_first_stage_can_be_undone(self) -> None:
        old = self.target / cm.PLUGIN_REL
        (old / "scripts").mkdir(parents=True)
        (old / "manifest.json").write_text('{"id": "gladimdim.config-sync", "version": "0.9"}')
        res = self.preview()
        real_py = cm.Target.py

        def fail_after_move(self, remote_dir, command, **kw):
            if command[0] == "target-link":
                raise cs.SyncError("connection dropped")
            return real_py(self, remote_dir, command, **kw)

        with patch.object(cm.Target, "py", fail_after_move):
            with self.assertRaises(cs.SyncError):
                cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                              "--confirm", res["target_hostname"]))
        out = cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))
        self.assertTrue(out["ok"], out)
        self.assertIn('"0.9"', (old / "manifest.json").read_text())

    def test_unpushed_plugin_history_stays_home_and_tokens_are_stripped(self) -> None:
        from test_config_sync import write_plugin_list, make_git_plugin
        write_plugin_list(self.repo, [{"id": "acme.pub", "name": "Pub", "version": "1.0.0",
                                       "source": "https://github.com/acme/pub.git"}])
        commit_all(self.repo, "list public plugin")
        cs.run_git(cs.configured_repo(self.src.ctx), ["pull", "--ff-only", "--quiet"])
        plugin = make_git_plugin(self.src.ctx.config_plugins / "acme.pub", "acme.pub", "1.0.0",
                                 "https://user:" + "gh" + "p_SECRETTOKEN0000@github.com/acme/pub.git")
        cs.run_git(plugin, ["update-ref", "refs/remotes/origin/main", "HEAD"])
        with patch.object(cm, "anonymous_source", return_value="https://github.com/acme/pub.git"):
            res = self.preview()
        cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                      "--confirm", res["target_hostname"]))
        cfg = (self.target / ".config/omarchy/plugins/acme.pub/.git/config").read_text()
        self.assertNotIn("SECRETTOKEN", cfg)
        self.assertIn("https://github.com/acme/pub.git", cfg)
        # An unpushed commit keeps the whole history at home.
        write(plugin / "extra.qml", "// local only\n")
        cs.run_git(plugin, ["add", "-A"])
        cs.run_git(plugin, ["commit", "-qm", "local"])
        with patch.object(cm, "anonymous_source", return_value="https://github.com/acme/pub.git"):
            self.assertEqual(cm.public_plugin_repos(self.src.ctx, ["acme.pub"]), [])

    def test_undo_never_deletes_an_original_that_was_not_moved_yet(self) -> None:
        res = self.preview()
        cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                      "--confirm", res["target_hostname"]))
        # Simulate a crash right after recording a replacement, before the move.
        orig = self.target / ".config/omarchy/plugins/their.own"
        orig.mkdir(parents=True)
        (orig / "manifest.json").write_text('{"id": "their.own"}')
        d = self.target / cm.UNDO_REL / res["runid"]
        man = json.loads((d / "manifest.json").read_text())
        rel = ".config/omarchy/plugins/their.own"
        man.setdefault("created_trees", []).append(rel)
        man["replaced_trees"] = {rel: str(self.target / ".config/omarchy/plugins-backup/their.own.pre-clone-x")}
        (d / "manifest.json").write_text(json.dumps(man))
        self.assertTrue(cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))["ok"])
        self.assertTrue((orig / "manifest.json").is_file())

    def test_confirmation_must_match_the_target_name(self) -> None:
        res = self.preview()
        with self.assertRaises(cs.SyncError):
            cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"], "--confirm", "wrong"))
        self.assertFalse((self.target / cm.STATE_REL / "state.json").exists())

    def test_secrets_and_history_never_reach_the_target(self) -> None:
        # A secret committed and deleted in the past, and one still in the tree.
        write(self.repo / "bin" / "old-helper", "#!/bin/sh\nTOKEN=" + "gh" + "p_" + "b" * 36 + "\n")
        commit_all(self.repo, "old helper with a token")
        (self.repo / "bin" / "old-helper").unlink()
        commit_all(self.repo, "remove it")
        write(self.repo / "bin" / "deploy-helper", "#!/bin/sh\nTOKEN=" + "gh" + "p_" + "a" * 36 + "\n")
        commit_all(self.repo, "helper with a token")
        clone = cs.configured_repo(self.src.ctx)
        cs.run_git(clone, ["pull", "--ff-only", "--quiet"])
        res = self.preview()
        self.assertIn("bin/deploy-helper", [s["path"] for s in res["secrets"]])
        self.assertNotIn("bin/deploy-helper", [f["path"] for f in res["plan"]["files"]])
        out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                            "--confirm", res["target_hostname"]))
        self.assertTrue(out["ok"], out)
        self.assertFalse((self.target / ".local/bin/deploy-helper").exists())
        seed = self.target / cm.SEED_REL
        self.assertEqual(cs.git_out(seed, "rev-list", "--all", "--count"), "1")
        for rel in ("bin/deploy-helper", "bin/old-helper"):
            res2 = cs.run_git(seed, ["log", "--all", "--oneline", "--", rel])
            self.assertEqual(res2.stdout.strip(), "", rel)

    def test_invalid_exclude_is_refused_not_ignored(self) -> None:
        res = self.preview()
        with self.assertRaises(cs.SyncError):
            cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                          "--confirm", res["target_hostname"], "--exclude", '["../../.ssh/config"]'))

    def test_undo_twice_is_refused(self) -> None:
        res = self.preview()
        cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                      "--confirm", res["target_hostname"]))
        self.assertTrue(cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))["ok"])
        with self.assertRaises(cs.SyncError):
            cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))

    def test_symlinked_target_file_comes_back_as_a_link(self) -> None:
        real = self.target / "dotfiles" / "looknfeel.lua"
        real.parent.mkdir()
        real.write_text("-- managed by stow\n")
        link = self.target / ".config/hypr/looknfeel.lua"
        link.unlink()
        link.symlink_to(real)
        res = self.preview()
        cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                      "--confirm", res["target_hostname"]))
        cm.cmd_undo(self.src.ctx, ns("undo", "--target", "newbox"))
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(real))

    def test_half_finished_clone_blocks_a_new_preview(self) -> None:
        res = self.preview()
        with patch.object(cm.Target, "py", side_effect=[{"ok": True, "previous": ""}, cs.SyncError("drop")]):
            with self.assertRaises(cs.SyncError):
                cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                              "--confirm", res["target_hostname"]))
        with self.assertRaises(cs.SyncError) as err:
            self.preview()
        self.assertTrue(err.exception.extra.get("unfinished"))

    def test_repo_moving_after_preview_requires_a_new_preview(self) -> None:
        res = self.preview()
        clone = cs.configured_repo(self.src.ctx)
        write(clone / "hypr" / "extra.lua", "-- new\n")
        commit_all(clone, "moved")
        with self.assertRaises(cs.SyncError):
            cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                          "--confirm", res["target_hostname"]))

    def test_resume_skips_finished_stages_and_keeps_first_undo_record(self) -> None:
        res = self.preview()
        real_py = cm.Target.py
        calls = {"n": 0}

        def flaky(self, remote_dir, command, **kw):
            if command[0] == "target-apply" and calls["n"] == 0:
                calls["n"] += 1
                raise cs.SyncError("connection dropped")
            return real_py(self, remote_dir, command, **kw)

        with patch.object(cm.Target, "py", flaky):
            with self.assertRaises(cs.SyncError) as err:
                cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--runid", res["runid"],
                                              "--confirm", res["target_hostname"]))
            self.assertTrue(err.exception.extra.get("resumable"))
            out = cm.cmd_clone(self.src.ctx, ns("clone", "--target", "newbox", "--resume"))
        self.assertTrue(out["ok"], out)
        stages = {e["stage"]: e["status"] for e in out["log"]}
        self.assertEqual(stages["plugin"], "skipped")
        self.assertEqual(stages["apply"], "done")


class GuardTests(unittest.TestCase):
    def test_destination_validation_blocks_option_injection(self) -> None:
        for bad in ["-oProxyCommand=touch /tmp/x", "host;rm -rf ~", "a b", "", "user@@host", "$(id)"]:
            with self.assertRaises(cs.SyncError, msg=bad):
                cm.validate_dest(bad)
        for good in ["newbox", "pi@newbox", "pi@10.0.0.5", "newbox.example-tailnet.ts.net"]:
            self.assertEqual(cm.validate_dest(good), good)

    def test_target_commands_refuse_foreign_directories(self) -> None:
        for bad in ["/etc", "../x", ".cache/other", ".cache/omarchy-config-sync-clone/../../x"]:
            out = io_main(["target-plan", "--dir", bad])
            self.assertFalse(out["ok"], bad)

    def test_terminal_commands_quote_everything(self) -> None:
        with TempHome() as env:
            cm.save_run(env.ctx, "newbox", {"repo_pkgs": ["foo", "bar-baz"], "aur_pkgs": [], "services": []})
            launched = []
            with patch.object(cs, "launch_omarchy_terminal", side_effect=lambda c: launched.append(c) or True):
                cm.cmd_terminal(env.ctx, ns("terminal", "--target", "newbox", "--kind", "packages"))
                cm.cmd_terminal(env.ctx, ns("terminal", "--target", "pi@newbox", "--kind", "copy-id"))
            self.assertIn("ssh -t -- newbox", launched[0])
            self.assertIn("omarchy-pkg-add foo bar-baz", launched[0])
            self.assertNotIn("pacman -Syu", launched[0])
            self.assertIn("ssh-copy-id -i ~/.ssh/id_ed25519.pub -- pi@newbox", launched[1])

    def test_autostart_programs_are_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "hypr").mkdir()
            (repo / "hypr" / "autostart.lua").write_text(
                '-- o.launch_on_start("commented-out")\no.launch_on_start("solaar --window=hide")\n'
                'o.launch_on_start("kdeconnect-indicator")\nhl.exec_once("uwsm-app -- waybar")\n')
            self.assertEqual(sorted(cm.autostart_commands(repo)), ["kdeconnect-indicator", "solaar", "waybar"])

    def test_same_machine_needs_both_id_and_host_key(self) -> None:
        a = {"machine_id": "m1", "host_key": "k1"}
        self.assertTrue(cm.same_machine(a, {"machine_id": "m1", "host_key": "k1"}))
        self.assertFalse(cm.same_machine(a, {"machine_id": "m1", "host_key": "k2"}))  # imaged twins
        self.assertFalse(cm.same_machine(a, {"machine_id": "m2", "host_key": "k1"}))
        self.assertIsNone(cm.same_machine({}, {}))

    def test_discovery_sources_parse(self) -> None:
        status = {"Peer": {
            "a": {"Online": True, "OS": "linux", "HostName": "alpha", "DNSName": "alpha.tail.ts.net.", "TailscaleIPs": ["100.1.1.1"]},
            "b": {"Online": False, "OS": "linux", "HostName": "off", "DNSName": "off.tail.ts.net.", "TailscaleIPs": ["100.1.1.2"]},
            "c": {"Online": True, "OS": "iOS", "HostName": "phone", "DNSName": "phone.tail.ts.net.", "TailscaleIPs": ["100.1.1.3"]}}}
        self.assertEqual([p["dest"] for p in cm.tailscale_peers(status)], ["alpha.tail.ts.net"])
        cfg = "Host alpha beta\n  User pi\nHost *.exe.xyz\nHost !bad\n# Host commented\nHost box # trailing\n"
        self.assertEqual(cm.ssh_config_hosts(cfg), ["alpha", "beta", "box"])
        neigh = "10.0.0.5 dev wlo1 lladdr aa REACHABLE\n10.0.0.6 dev wlo1 FAILED\nfe80::1 dev wlo1 lladdr bb STALE\n"
        self.assertEqual(cm.lan_neighbors(neigh), ["10.0.0.5"])
        avahi = "=;wlo1;IPv4;newbox;_ssh._tcp;local;newbox.local;10.0.0.9;22;\n+;wlo1;IPv4;x;_ssh._tcp;local\n"
        self.assertEqual(cm.mdns_hosts(avahi), [("newbox.local", "10.0.0.9")])

    def test_secret_scanner_names_and_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            os.system(f"git -C {repo} init -q")
            (repo / "hypr").mkdir()
            (repo / "hypr" / "envs.lua").write_text('env("LIBVA_DRIVER_NAME", "nvidia") -- srcbox\n')
            (repo / ".env").write_text("X=1\n")
            (repo / "key.txt").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
            os.system(f"git -C {repo} add -A")
            secrets, specific = cm.scan_repo(repo, "srcbox", ["100.1.2.3"])
            self.assertEqual({s["path"] for s in secrets}, {".env", "key.txt"})
            self.assertEqual(specific[0]["path"], "hypr/envs.lua")
            self.assertIn("NVIDIA graphics settings", specific[0]["reasons"])


def io_main(argv):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cm.main(argv)
    return json.loads(buf.getvalue())


if __name__ == "__main__":
    unittest.main()
