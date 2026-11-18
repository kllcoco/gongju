"""WP1 policy tests (§21.2 policy row): authorization formula, PathGuard, deny_write, CommandGuard, URL rules 5a–5c,
secret_write, kill guard, child env, interpreter / sys.path / PATH hard denies, Limits."""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hive import catalog, policy as policy_mod, util
from hive.errors import HiveError
from hive.netutil import VettedTarget
from hive.policy import EffectivePolicy, Policy, windows_string_checks
from hive.secretstore import SecretStore
from tests import helpers
from tests.wp1util import (CAUTIOUS, FILES, OPERATOR, READNET, READONLY, FakeHome, build_policy, ident)

SPECS = catalog.SPECS


def eff(pol: Policy, who=OPERATOR, **kw) -> EffectivePolicy:
    return pol.effective(who, paused=kw.pop("paused", False), **kw)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.__exit__, None, None, None)

    def assertRule(self, fn, code: str, rule=None):  # noqa: N802
        with self.assertRaises(HiveError) as cm:
            fn()
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        if rule is not None:
            self.assertEqual(cm.exception.details.get("rule"), rule, cm.exception.message)
        return cm.exception


class TestAuthorization(Base):
    def test_formula_truth_table(self):
        std = build_policy(self.fh)
        ro = build_policy(self.fh, policy={"read_only": True})
        noexec = build_policy(self.fh, policy={"exec": {"enabled": False}})
        denyshell = build_policy(self.fh, policy={"deny_actions": ["shell.*"]})
        rows = [
            (std, OPERATOR, "shell.run", False, None),
            (std, READONLY, "fs.write", False, "forbidden"),
            (std, READONLY, "fs.read", False, None),
            (std, READNET, "net.download", False, "forbidden"),      # extra_classes write
            (std, READNET, "model.pull", False, "forbidden"),
            (std, ident(("persist",)), "service.define", False, "forbidden"),   # needs exec too
            (std, ident(name="d", deny_actions=("fs.*",)), "fs.read", False, "forbidden"),
            (ro, OPERATOR, "fs.write", False, "read_only"),
            (ro, OPERATOR, "fs.read", False, None),
            (noexec, OPERATOR, "service.define", False, "exec_disabled"),
            (noexec, OPERATOR, "shell.run", False, "exec_disabled"),
            (noexec, OPERATOR, "fs.write", False, None),
            (denyshell, OPERATOR, "shell.run", False, "deny_actions"),
            (std, OPERATOR, "fs.write", True, "paused"),
            (std, OPERATOR, "fs.read", True, None),
        ]
        for pol, who, action, paused, expect in rows:
            with self.subTest(who=who.name, action=action, paused=paused, expect=expect):
                e = pol.effective(who, paused=paused)
                if expect is None:
                    e.check_action(SPECS[action])
                elif expect == "forbidden":
                    self.assertRule(lambda: e.check_action(SPECS[action]), "forbidden")
                else:
                    self.assertRule(lambda: e.check_action(SPECS[action]), "policy_denied", expect)

    def test_hub_token_role_caps(self):
        pol = build_policy(self.fh)
        hub_files = ident(("read", "write"), name="hub-home", kind="hub", via="hub")
        self.assertRule(lambda: eff(pol, hub_files).check_action(SPECS["shell.run"]), "forbidden")
        err = self.assertRule(lambda: eff(pol, hub_files).check_action(SPECS["model.forward"]), "forbidden")
        self.assertIn("hub token on this node needs the net class", err.hint)
        relay = ident(("read", "write"), name="relay", kind="relay", via="relay")
        err = self.assertRule(lambda: eff(pol, relay).check_action(SPECS["model.forward"]), "forbidden")
        self.assertIn("relay role on this pod needs the net class for the gateway (role is files)", err.hint)
        summary = eff(pol, hub_files).summary()
        self.assertIn("shell.run", summary["denied_actions"])
        self.assertNotIn("fs.read", summary["denied_actions"])

    def test_needs_approval_direct_agents_only(self):
        pol = build_policy(self.fh)
        self.assertTrue(eff(pol, CAUTIOUS).needs_approval(SPECS["shell.run"]))
        self.assertFalse(eff(pol, CAUTIOUS).needs_approval(SPECS["fs.read"]))
        self.assertFalse(eff(pol, dataclasses.replace(CAUTIOUS, via="hub")).needs_approval(SPECS["shell.run"]))
        self.assertFalse(eff(pol, dataclasses.replace(CAUTIOUS, kind="hub")).needs_approval(SPECS["shell.run"]))
        self.assertIn("shell.run", eff(pol, CAUTIOUS).summary()["approval_actions"])
        self.assertNotIn("fs.read", eff(pol, CAUTIOUS).summary()["approval_actions"])

    def test_check_timeout(self):
        e = eff(build_policy(self.fh))
        self.assertEqual(e.check_timeout(None, 60, 3600), 60)
        self.assertEqual(e.check_timeout(10, 60, 3600), 10)
        err = self.assertRule(lambda: e.check_timeout(7200, 60, 3600), "bad_request")
        self.assertIn("max timeout on this node is 3600 s", err.hint)

    def test_limits_and_endpoints_from_config(self):
        pol = build_policy(self.fh, policy={"fs": {"max_read_bytes": 1234}, "limits": {"max_result_bytes": 99999}},
                           model_endpoints=[{"name": "ollama", "base_url": "http://127.0.0.1:11434/v1"}])
        self.assertEqual(pol.limits.max_read_bytes, 1234)
        self.assertEqual(pol.limits.max_result_bytes, 99999)
        self.assertEqual(pol.model_endpoints, [{"name": "ollama", "base_url": "http://127.0.0.1:11434/v1"}])
        self.assertEqual(eff(pol).model_endpoints, pol.model_endpoints)

    def test_refuses_without_home(self):
        with mock.patch.dict(os.environ, {"HOME": "relative/home"}):
            self.assertRule(lambda: build_policy(self.fh), "internal")


class TestPathGuard(Base):
    def test_basic_forms(self):
        e = eff(build_policy(self.fh))
        for bad in ("", "a\x00b", "x" * 5000):
            self.assertRule(lambda: e.resolve_path(bad), "bad_request")
        self.assertRule(lambda: e.resolve_path("~root/.ssh"), "bad_request")
        self.assertEqual(e.resolve_path("~"), self.fh.home)
        self.assertEqual(e.resolve_path("sub/x.txt"), self.fh.home / "sub" / "x.txt")   # relative -> workdir
        self.assertEqual(e.resolve_path("$HOME/a"), self.fh.home / "a")
        self.assertEqual(e.resolve_path("${HOME}/b"), self.fh.home / "b")

    def test_secret_env_var_never_expanded(self):
        e = eff(build_policy(self.fh), READONLY)
        with mock.patch.dict(os.environ, {"SOME_SECRET_ENV": "/etc"}):
            for form in ("$SOME_SECRET_ENV", "%SOME_SECRET_ENV%", "${SOME_SECRET_ENV}"):
                p = e.resolve_path(form)
                self.assertEqual(p.name, form)
                self.assertNotIn("/etc", str(p.parent) if p.parent != self.fh.home else "")
        err = self.assertRule(lambda: e.resolve_path("/etc/$SOME_SECRET_ENV"), "policy_denied", "fs_roots")
        self.assertNotIn("/etc/etc", err.message)

    def test_outside_roots_and_dotdot(self):
        e = eff(build_policy(self.fh))
        err = self.assertRule(lambda: e.resolve_path("/etc/hostname"), "policy_denied", "fs_roots")
        self.assertIn("Allowed roots", err.hint)
        self.assertRule(lambda: e.resolve_path("~/../../etc/passwd"), "policy_denied", "fs_roots")
        self.assertRule(lambda: e.resolve_path("~/missing/../../x"), "policy_denied", "fs_roots")

    def test_lexical_precheck_makes_no_fs_call(self):
        e = eff(build_policy(self.fh), READONLY)
        with mock.patch("os.path.realpath", side_effect=AssertionError("realpath called")), \
                mock.patch("os.stat", side_effect=AssertionError("stat called")), \
                mock.patch("os.path.lexists", side_effect=AssertionError("lexists called")):
            self.assertRule(lambda: e.resolve_path("//attacker/share/x"), "policy_denied", "fs_roots")
            self.assertRule(lambda: e.resolve_path("/srv/elsewhere"), "policy_denied", "fs_roots")

    def test_symlink_escape(self):
        outside = Path(tempfile.mkdtemp(prefix="hiveout-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        (outside / "secret.txt").write_text("s")
        os.symlink(outside, self.fh.p("link"))
        e = eff(build_policy(self.fh))
        self.assertRule(lambda: e.resolve_path("~/link/secret.txt"), "policy_denied", "fs_roots")
        self.assertRule(lambda: e.resolve_path("~/link/new.txt", write=True), "policy_denied", "fs_roots")

    def test_root_via_symlink_accepts_real_path(self):
        # macOS /tmp -> /private/tmp: a root given through a symlink also matches its realpath (lexical + real)
        real = self.fh.base / "realroot"
        real.mkdir()
        os.symlink(real, self.fh.base / "aliasroot")
        pol = build_policy(self.fh, policy={"fs": {"roots": [{"path": "~", "mode": "rw"},
                                                             {"path": str(self.fh.base / "aliasroot"),
                                                              "mode": "rw"}]}})
        self.assertEqual(eff(pol).resolve_path(str(real / "x")), real / "x")
        self.assertEqual(eff(pol).resolve_path(str(self.fh.base / "aliasroot" / "y")), real / "y")

    def test_ro_root_and_deny_wins(self):
        photos = self.fh.p("photos")
        photos.mkdir()
        pol = build_policy(self.fh, policy={"fs": {"roots": [{"path": "~", "mode": "rw"},
                                                             {"path": "~/photos", "mode": "ro"}],
                                                   "deny": ["<DEFAULT_SECRET_PATHS>", "~/private/**"]}})
        e = eff(pol)
        e.resolve_path("~/photos/a.jpg")
        self.assertRule(lambda: e.resolve_path("~/photos/a.jpg", write=True), "policy_denied", "fs_roots")
        self.assertRule(lambda: e.resolve_path("~/private/x"), "policy_denied", "fs_deny")
        self.assertRule(lambda: e.resolve_path("~/private"), "policy_denied", "fs_deny")

    def test_default_secret_paths_per_os(self):
        e = eff(build_policy(self.fh), OPERATOR)
        for p in ("~/.ssh/id_rsa", "~/Library/Keychains/login.keychain-db",
                  "~/AppData/Roaming/Microsoft/Credentials/x", "~/.aws/credentials", "~/.cache/huggingface/token"):
            with self.subTest(p=p):
                self.assertRule(lambda: e.resolve_path(p), "policy_denied", "fs_deny")
        self.assertTrue(policy_mod.DEFAULT_SECRET_PATHS and "~/.ssh/**" in policy_mod.DEFAULT_SECRET_PATHS)

    def test_hard_denies_under_full(self):
        conf_dir = self.fh.base / "conf"
        conf_dir.mkdir()
        key = conf_dir / "tls.key"
        key.write_text("k")
        pol = build_policy(self.fh, "full", protected=([key], []))
        e = eff(pol)
        for p in (str(self.fh.hive_home / "node" / "secrets.json"), str(self.fh.hive_home / "x"),
                  str(self.fh.p("proj/.hive/x")), str(self.fh.p("a/.HIVE/b")), str(key),
                  "/mnt/c/Users/u/.hive/node/config.json"):
            with self.subTest(p=p):
                self.assertRule(lambda: e.resolve_path(p), "policy_denied", "hive_internal")
        self.assertRule(lambda: e.resolve_path(f"/proc/{os.getpid()}/environ"), "policy_denied", "hive_internal")
        self.assertRule(lambda: e.resolve_path("/proc/self/environ"), "policy_denied", "hive_internal")
        e.resolve_path(str(self.fh.p("normal.txt")), write=True)

    def test_data_dir_outside_hive_home(self):
        data = self.fh.base / "elsewhere-data"
        data.mkdir()
        cfgpol = build_policy(self.fh, "full")
        pol = Policy(cfgpol.cfg, workdir=self.fh.home, data_dir=data, hive_home=self.fh.hive_home,
                     protected=([], []), network=__import__("hive.config", fromlist=["x"]).NetworkConfig(proxy="off"),
                     models=__import__("hive.config", fromlist=["x"]).ModelsConfig(),
                     secrets=SecretStore(data / "s.json", passthrough=(), env={}))
        self.assertRule(lambda: eff(pol).resolve_path(str(data / "audit" / "x.jsonl")), "policy_denied",
                        "hive_internal")

    def test_case_variants_on_casefold_platforms(self):
        with mock.patch.object(policy_mod, "IS_MAC", True):
            pol = build_policy(self.fh)
        e = eff(pol)
        self.assertRule(lambda: e.resolve_path("~/.SSH/id_rsa"), "policy_denied", "fs_deny")
        self.assertRule(lambda: e.resolve_path(str(self.fh.hive_home).upper() + "/node/config.json"),
                        "policy_denied")

    def test_windows_string_checks(self):
        for bad, rule in (("\\\\?\\C:\\x", "path_form"), ("\\\\.\\PhysicalDrive0", "path_form"),
                          ("\\\\attacker\\x", "unc"), ("//attacker/x", "unc"), ("C:\\a.txt:x", "ads"),
                          ("C:\\work\\con.txt", "device_name"), ("C:\\work\\NUL", "device_name"),
                          ("C:\\work\\x.", "trailing_dot"), ("C:\\work\\x ", "trailing_dot")):
            with self.subTest(bad=bad):
                self.assertRule(lambda: windows_string_checks(bad), "policy_denied", rule)
        windows_string_checks("C:\\work\\a.txt")
        windows_string_checks("D:/models/x.gguf")
        windows_string_checks("\\\\wsl.localhost\\Ubuntu\\x", unc_allowed=True)
        with mock.patch.dict(os.environ, {"TEMP": "\\\\?\\C:\\evil"}):
            from hive.policy import _VAR_RE
            s = _VAR_RE.sub(lambda m: os.environ.get(m.group(3) or m.group(2) or m.group(1), m.group(0)), "%TEMP%\\x")
            self.assertRule(lambda: windows_string_checks(s), "policy_denied", "path_form")

    def test_wsl_secret_remap(self):
        pol = build_policy(self.fh, is_wsl=True, policy={"fs": {"roots": [{"path": "~", "mode": "rw"},
                                                                          {"path": "/mnt", "mode": "rw"}]}})
        e = eff(pol)
        self.assertRule(lambda: e.resolve_path("/mnt/c/Users/alice/.ssh/id_rsa"), "policy_denied", "fs_deny")
        self.assertRule(lambda: e.resolve_path("/mnt/c/Users/Alice/.SSH/id_rsa"), "policy_denied", "fs_deny")
        self.assertRule(lambda: eff(pol, FILES).resolve_path("/mnt/d/Users/bob/.bashrc", write=True),
                        "policy_denied", "exec_path")


class TestSubtree(Base):
    def test_subtree_cheap_and_walk(self):
        for rel in (".ssh/id_rsa", ".config/gcloud/c.json", ".aws/credentials", "tmp/x/user/evil.service",
                    "tmp/y/hooks/pre-commit", "proj/.git/config", "repo/.git/hooks/pre-commit", "big/a"):
            self.fh.touch(rel)
        e = eff(build_policy(self.fh), FILES)
        for p in ("~/.ssh", "~/.config", "~/.aws", "~"):
            with self.subTest(p=p):
                self.assertRule(lambda: e.resolve_path(p, write=True, subtree=True), "policy_denied")
        self.assertRule(lambda: e.resolve_path("~/.ssh"), "policy_denied", "fs_deny")      # list ~/.ssh
        self.assertRule(lambda: e.resolve_path("~/repo", write=True, subtree=True), "policy_denied",
                        "contains_denied")                                                # leading ** pattern
        self.assertEqual(eff(build_policy(self.fh), OPERATOR).denied_under(self.fh.p("repo"), write=True), [])
        # ancestor of HIVE_HOME (a pod's /root): hard deny below it
        self.assertTrue(eff(build_policy(self.fh, "full")).denied_under(self.fh.base))

    def test_walk_cap(self):
        for i in range(30):
            self.fh.touch(f"many/f{i}")
        e = eff(build_policy(self.fh), FILES)
        with mock.patch.object(policy_mod, "MAX_WALK", 10):
            self.assertRule(lambda: e.denied_under(self.fh.p("many"), write=True), "too_large")

    def test_destination_checks(self):
        self.fh.touch("tmp/x/user/evil.service")
        self.fh.touch("tmp/y/hooks/pre-commit")
        self.fh.p("proj").mkdir()
        e = eff(build_policy(self.fh), FILES)
        self.assertTrue(e.denied_under(self.fh.p("tmp/x"), write=True, rebase=self.fh.p(".config/systemd")))
        self.assertTrue(e.denied_under(self.fh.p("tmp/y"), write=True, rebase=self.fh.p("proj/.git")))
        self.assertEqual(e.denied_under(self.fh.p("tmp/y"), write=True, rebase=self.fh.p("proj/other")), [])
        self.assertRule(lambda: e.resolve_path("~/proj/.git/config", write=True), "policy_denied", "exec_path")


class TestDenyWrite(Base):
    PATHS = ["~/.bashrc", "~/.bash_aliases", "~/.local/share/systemd/user/x.service", "~/.wslconfig",
             "~/.config/pip/pip.conf", "~/.venvs/vllm/lib/python3.12/site-packages/vllm/__init__.py",
             "~/OneDrive - X/Documents/PowerShell/profile.ps1", "~/.condarc", "~/Desktop/x.lnk", "~/.vimrc",
             "~/.config/nvim/init.lua", "~/proj/.envrc", "~/proj/.vscode/tasks.json",
             "~/.ipython/profile_default/startup/a.py", "~/AppData/Local/Programs/Ollama/ollama app.exe",
             "~/AppData/Roaming/x/y.dll", "~/Applications/X.app/Contents/MacOS/x", "~/.config/autostart/x.desktop",
             "~/x/lib.pth", "~/.gitconfig",
             # v8.1 external review (audit finding 4): every path the audit planted with a cautious operator token
             "~/.local/share/jupyter/kernels/python3/kernel.json", "~/.bashrc.d/x.sh", "~/.xsession",
             "~/.config/plasma-workspace/env/x.sh", "~/.kde/Autostart/x.sh", "~/.kde4/Autostart/x.sh",
             "~/.oh-my-zsh/custom/x.zsh", "~/.pam_environment", "~/.Rprofile", "~/.gdbinit", "~/.emacs.d/init.el",
             "~/.emacs", "~/.config/emacs/init.el", "~/.tmux.conf", "~/.wezterm.lua", "~/.config/wezterm/w.lua",
             "~/.config/i3/config", "~/.config/sway/config", "~/.config/hypr/hyprland.conf", "~/.hgrc",
             "~/proj/.yarnrc.yml", "~/.yarnrc.yml", "~/.config/direnv/direnvrc", "~/.config/systemd/user.conf",
             "~/.zshrc.d/a.zsh", "~/Library/Jupyter/kernels/k/kernel.json",
             "~/AppData/Roaming/jupyter/kernels/k/kernel.json", "~/proj/.hg/hgrc", "~/.vim/plugin/x.vim",
             # v8.1 external review, round 2 (exec-path denylist): PATH dirs, sourced files, terminal/WM/REPL configs
             "~/.cargo/bin/ls", "~/.cargo/env", "~/go/bin/x", "~/.bun/bin/x", "~/.deno/bin/x", "~/.npm-global/bin/x",
             "~/.pyenv/shims/python", "~/miniconda3/bin/python", "~/anaconda3/bin/x", "~/.nvm/nvm.sh",
             "~/.oh-my-zsh/oh-my-zsh.sh", "~/.oh-my-zsh/tools/x.sh", "~/.config/zsh/.zshrc", "~/.zsh/x.zsh",
             "~/.config/starship.toml", "~/.tcshrc", "~/.cshrc", "~/.kshrc", "~/.xonshrc", "~/.config/nushell/config.nu",
             "~/.config/alacritty/alacritty.toml", "~/.config/kitty/kitty.conf", "~/.config/foot/foot.ini",
             "~/.config/ghostty/config", "~/.local/share/konsole/a.profile", "~/.config/openbox/autostart",
             "~/.config/labwc/autostart", "~/.config/river/init", "~/.config/bspwm/bspwmrc",
             "~/.config/awesome/rc.lua", "~/.config/qtile/config.py", "~/.config/niri/config.kdl",
             "~/.config/xfce4/xinitrc", "~/.config/lxsession/LXDE/autostart", "~/.julia/config/startup.jl",
             "~/.octaverc", "~/.ghci", "~/.irbrc", "~/.psqlrc", "~/.sqliterc", "~/.lldbinit", "~/.mailcap",
             "~/.vscode/extensions/x/ext.js", "~/venvs/other/pyvenv.cfg"]

    def test_v81_exec_paths_not_overbroad(self):
        pol = build_policy(self.fh)
        for p in ("~/.config/systemd/notes.txt", "~/proj/yarnrc.yml", "~/.local/share/other/x", "~/kde/x.sh",
                  "~/.config/i3x/config", "~/proj/bin/run.sh", "~/go/src/app/main.go", "~/.config/zshx",
                  "~/proj/.vscode/settings.json", "~/proj/conda-notes.txt"):
            with self.subTest(p=p):
                eff(pol, FILES).resolve_path(p, write=True)

    def test_exec_paths_for_files_token(self):
        pol = build_policy(self.fh)
        for p in self.PATHS:
            with self.subTest(p=p):
                self.assertRule(lambda: eff(pol, FILES).resolve_path(p, write=True), "policy_denied", "exec_path")
                eff(pol, OPERATOR).resolve_path(p, write=True)        # operator on standard: allowed
                eff(pol, FILES).resolve_path(p)                       # reads are fine

    def test_gating_sources(self):
        pol = build_policy(self.fh)
        self.assertRule(lambda: eff(pol, CAUTIOUS).resolve_path("~/.bashrc", write=True), "policy_denied",
                        "exec_path")
        hub_gated = ident(name="hub", kind="hub", via="hub", exec_gated=True)
        self.assertRule(lambda: eff(pol, hub_gated).resolve_path("~/.bashrc", write=True), "policy_denied",
                        "exec_path")
        shell_denied = ident(name="d", deny_actions=("shell.run",))
        self.assertRule(lambda: eff(pol, shell_denied).resolve_path("~/.bashrc", write=True), "policy_denied",
                        "exec_path")
        self.assertFalse(eff(pol, OPERATOR).write_exec_paths_denied)
        self.assertFalse(eff(pol, dataclasses.replace(OPERATOR, kind="hub", via="hub")).write_exec_paths_denied)

    def test_exec_contained_nodes(self):
        rows = [({"exec": {"mode": "allowlist"}}, True), ({"exec": {"enabled": False}}, True),
                ({"deny_actions": ["shell.run"]}, True),
                ({"allow_actions": ["fs.*", "model.*", "system.*", "node.*"]}, True), ({"read_only": True}, True),
                ({}, False)]
        hub_op = ident(name="hub", kind="hub", via="hub")
        for pol_over, contained in rows:
            with self.subTest(policy=pol_over):
                pol = build_policy(self.fh, policy=pol_over)
                self.assertIs(pol.exec_contained, contained)
                self.assertIs(eff(pol, OPERATOR).write_exec_paths_denied, contained)
                if contained and not pol_over.get("read_only"):
                    self.assertRule(lambda: eff(pol, OPERATOR).resolve_path("~/.bashrc", write=True),
                                    "policy_denied", "exec_path")
                    self.assertRule(lambda: eff(pol, hub_op).resolve_path("~/.bashrc", write=True),
                                    "policy_denied", "exec_path")
                elif not contained:
                    eff(pol, OPERATOR).resolve_path("~/.bashrc", write=True)
        pol = build_policy(self.fh, policy={"read_only": True})
        self.assertRule(lambda: eff(pol, OPERATOR).check_action(SPECS["fs.write"]), "policy_denied", "read_only")

    def test_venv_rule(self):
        self.fh.touch("venv/pyvenv.cfg", "home = /usr/bin")
        self.fh.touch("venv/bin/activate")
        self.fh.touch("plain/bin/x")
        pol = build_policy(self.fh)
        self.assertRule(lambda: eff(pol, FILES).resolve_path("~/venv/bin/activate", write=True), "policy_denied",
                        "exec_path")
        self.assertRule(lambda: eff(pol, FILES).resolve_path("~/venv/bin/new", write=True), "policy_denied",
                        "exec_path")
        eff(pol, FILES).resolve_path("~/plain/bin/x", write=True)
        eff(pol, OPERATOR).resolve_path("~/venv/bin/activate", write=True)

    def test_full_preset_has_no_deny_write(self):
        pol = build_policy(self.fh, "full")
        eff(pol, FILES).resolve_path("~/.bashrc", write=True)

    def test_path_dirs_inside_rw_roots(self):
        bindir = self.fh.p("tools/bin")
        bindir.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + "/usr/bin" + os.pathsep + "rel"}):
            pol = build_policy(self.fh)
        self.assertRule(lambda: eff(pol, FILES).resolve_path(str(bindir / "pwsh.exe"), write=True),
                        "policy_denied", "exec_path")
        eff(pol, OPERATOR).resolve_path(str(bindir / "pwsh.exe"), write=True)
        self.assertNotIn(str(bindir), pol.trusted_path.split(os.pathsep))
        self.assertIn("/usr/bin", pol.trusted_path.split(os.pathsep))
        self.assertTrue(pol.node_path.endswith(str(bindir) + os.pathsep + "/usr/bin" + os.pathsep + "rel"))

    def test_services_code_dirs(self):
        venv = self.fh.p(".venvs/vllm")
        comfy = self.fh.p("ComfyUI")
        view = lambda: {"endpoints": [], "code_dirs": [str(venv), str(comfy), str(self.fh.home)]}  # noqa: E731
        pol = build_policy(self.fh)
        g = pol.effective(FILES, paused=False, services=view)
        self.assertRule(lambda: g.resolve_path("~/.venvs/vllm/bin/x", write=True), "policy_denied", "exec_path")
        self.assertRule(lambda: g.resolve_path("~/ComfyUI/custom_nodes/a.py", write=True), "policy_denied",
                        "exec_path")
        g.resolve_path("~/other.txt", write=True)          # the home entry is skipped (never the whole root)
        pol.effective(OPERATOR, paused=False, services=view).resolve_path("~/ComfyUI/custom_nodes/a.py", write=True)


class TestInterpreterAndSysPath(Base):
    def test_hive_package_and_interpreter(self):
        pol = build_policy(self.fh, "full", policy={"fs": {"roots": [{"path": "*", "mode": "rw"}]}})
        pkg = Path(policy_mod.__file__).resolve().parent
        for who in (OPERATOR, FILES):
            self.assertRule(lambda: eff(pol, who).resolve_path(str(pkg / "util.py"), write=True), "policy_denied",
                            "hive_internal")
            self.assertRule(lambda: eff(pol, who).resolve_path(sys.executable, write=True), "policy_denied",
                            "hive_internal")
        eff(pol, FILES).resolve_path(str(pkg / "util.py"))      # reading is allowed

    def test_base_prefix_inside_root(self):
        base = self.fh.p("pythons/cpython")
        (base / "Lib").mkdir(parents=True)
        with mock.patch.object(sys, "base_prefix", str(base)):
            pol = build_policy(self.fh)
            pol_full = build_policy(self.fh, "full")
        for p in (pol, pol_full):
            for who in (CAUTIOUS, OPERATOR):
                self.assertRule(lambda: eff(p, who).resolve_path(str(base / "Lib" / "site.py"), write=True),
                                "policy_denied", "hive_internal")
        with mock.patch.object(sys, "base_prefix", str(self.fh.home)):
            pol2 = build_policy(self.fh)
        self.assertTrue(any("home, a root or an ancestor" in w for w in pol2.warnings))
        eff(pol2, OPERATOR).resolve_path("~/ok.txt", write=True)

    def test_sys_path_dirs(self):
        d = self.fh.p("checkout")
        d.mkdir()
        with mock.patch.object(sys, "path", list(sys.path) + [str(d)]):
            pol = build_policy(self.fh)
        for who in (OPERATOR, FILES):
            for rel in ("json.py", "hive/__init__.py"):
                self.assertRule(lambda: eff(pol, who).resolve_path(str(d / rel), write=True), "policy_denied",
                                "hive_internal")
        with mock.patch.object(sys, "path", list(sys.path) + [str(self.fh.home)]):
            err = self.assertRule(lambda: build_policy(self.fh), "internal")
        self.assertIn("import path", err.hint)

    def test_hub_code_paths(self):
        code = self.fh.p("hubcode")
        code.mkdir()
        pol = build_policy(self.fh, protected=([], [code]))
        self.assertRule(lambda: eff(pol, OPERATOR).resolve_path(str(code / "x.py"), write=True), "policy_denied",
                        "hive_internal")


class TestHolds(Base):
    def test_approved_input(self):
        from hive.approvals import FileHolds, file_fingerprint
        f = self.fh.touch("run.sh", "echo hi")
        holds = FileHolds()
        pol = build_policy(self.fh)
        e = pol.effective(FILES, paused=False, holds=holds)
        hid = holds.acquire({str(f): file_fingerprint(str(f))}, policy=e)
        self.assertRule(lambda: e.resolve_path(str(f), write=True), "conflict", "approved_input")
        pol.effective(OPERATOR, paused=False, holds=holds).resolve_path(str(f), write=True)
        holds.release(hid)
        e.resolve_path(str(f), write=True)


class TestCommandGuard(Base):
    def test_default_denies(self):
        e = eff(build_policy(self.fh))
        for cmd in ("sudo reboot", "ls && shutdown -h now", "systemctl poweroff", "rm -rf /", "rm -rf ~",
                    "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda", ":(){ :|:& };:", "cat ~/.hive/x",
                    "docker run --privileged x", "docker run -v /:/host x", "Stop-Computer",
                    "format c:", "reg delete HKLM\\Software\\x"):
            with self.subTest(cmd=cmd):
                self.assertRule(lambda: e.check_command(cmd, "bash"), "policy_denied", "command_deny")
        for cmd in ("rm -rf ./build", "git reset", "grep -i shutdown vllm.log", 'python -c "ex.shutdown()"',
                    "journalctl | grep reboot", "ray stop", "ls -la", "docker run --rm -v /data:/data img"):
            with self.subTest(cmd=cmd):
                e.check_command(cmd, "bash")
        self.assertRule(lambda: e.check_command(["rm", "-rf", "/"], "auto"), "policy_denied", "command_deny")
        eff(build_policy(self.fh, "full")).check_command("sudo reboot", "bash")
        self.assertRule(lambda: e.check_shell("fish"), "policy_denied", "shell")
        e.check_shell("bash")

    def test_allowlist(self):
        tooldir = self.fh.base / "sysbin"
        tooldir.mkdir()
        tool = tooldir / "nvidia-smi"
        tool.write_text("#!/bin/sh\necho ok\n")
        tool.chmod(0o755)
        planted = self.fh.p("bin2")
        planted.mkdir()
        (planted / "nvidia-smi").write_text("#!/bin/sh\necho evil\n")
        (planted / "nvidia-smi").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(tooldir)}):
            pol = build_policy(self.fh, policy={"exec": {"mode": "allowlist", "command_allow": [r"nvidia-smi( .*)?"],
                                                         "env_allow": ["CUDA_VISIBLE_DEVICES"]}})
        e = eff(pol)
        e.check_command(["nvidia-smi", "-L"], "auto")
        self.assertRule(lambda: e.check_command("nvidia-smi", "bash"), "policy_denied", "allowlist")
        self.assertRule(lambda: e.check_command(["ls"], "auto"), "not_found")
        self.assertRule(lambda: e.check_command([str(planted / "nvidia-smi")], "auto"), "policy_denied",
                        "exec_in_writable_root")
        # an agent env PATH has no effect: resolution uses node_path
        with mock.patch.dict(os.environ, {"PATH": str(planted)}):
            e.check_command(["nvidia-smi"], "auto")
        self.assertEqual(e.node_path, pol.node_path)
        self.assertRule(lambda: e.check_env({"LD_PRELOAD": "/x.so"}), "bad_request", "env_not_allowed")
        e.check_env({"CUDA_VISIBLE_DEVICES": "0"})
        eff(build_policy(self.fh)).check_env({"LD_PRELOAD": "x"})     # open mode: no env check

    def test_env_allow_forbidden_names_are_config_errors(self):
        from tests.wp1util import node_config
        with self.assertRaises(HiveError):
            node_config(self.fh, policy={"exec": {"mode": "allowlist", "env_allow": ["PATH"]}})
        with self.assertRaises(HiveError):
            node_config(self.fh, policy={"exec": {"env_allow": ["LD_PRELOAD"]}})

    def test_check_cwd(self):
        e = eff(build_policy(self.fh))
        self.assertEqual(e.check_cwd(None), self.fh.home)
        self.fh.p("w").mkdir()
        self.assertEqual(e.check_cwd("~/w"), self.fh.p("w"))
        self.assertRule(lambda: e.check_cwd("~/nope"), "not_found")
        self.assertRule(lambda: e.check_cwd("/etc"), "policy_denied", "fs_roots")


class TestChildEnv(Base):
    def test_order(self):
        st = SecretStore(self.fh.data / "secrets.json", passthrough=(), env={})
        st.set("HF_TOKEN", "hf_abcdefghijklmnopqrstuvwxyz")
        st.set("NOEXEC_TOKEN", "abcdefghijklmn", exec_ok=False)
        pol = build_policy(self.fh, secrets=st, models={"hf_endpoint": "https://hf-mirror.com"},
                           policy={"exec": {"path_prepend": ["/opt/x/bin"]}})
        fake_proxy = {"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": "localhost"}
        with mock.patch.dict(os.environ, {"HIVE_SECRET_X": "s", "HIVE_THING": "t", "KEEP": "k",
                                          "HTTPS_PROXY": "http://env:1"}), \
                mock.patch("hive.netproxy.child_proxy_env", return_value=fake_proxy):
            for k in ("NO_PROXY", "no_proxy"):
                os.environ.pop(k, None)
            env = eff(pol).child_env({"NO_COLOR": "0", "EXTRA": "1"}, unset=["KEEP"], gpus="0,1",
                                     secret_env=["HF_TOKEN"])
        self.assertNotIn("HIVE_THING", env)
        self.assertNotIn("KEEP", env)
        self.assertEqual(env["HTTPS_PROXY"], "http://env:1")      # node env defines it: not overwritten
        self.assertEqual(env["NO_PROXY"], "localhost")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["NO_COLOR"], "0")                     # explicit env wins
        self.assertEqual(env["HF_ENDPOINT"], "https://hf-mirror.com")
        self.assertTrue(env["PATH"].startswith("/opt/x/bin" + os.pathsep))
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0,1")
        self.assertEqual(env["HF_TOKEN"], "hf_abcdefghijklmnopqrstuvwxyz")
        self.assertRule(lambda: eff(pol).child_env({}, secret_env=["NOEXEC_TOKEN"]), "policy_denied", "secret_env")
        self.assertRule(lambda: eff(pol).child_env({}, secret_env=["AWS_SECRET_ACCESS_KEY"]), "not_found")
        env2 = eff(pol).child_env({}, gpus="none")
        self.assertEqual(env2["CUDA_VISIBLE_DEVICES"], "")


class _ProxyGuard(helpers.FakeNetGuard):
    """FakeNetGuard that routes public targets through a proxy (and can pretend a name is unresolvable/fake-ip)."""

    def check_url(self, url, *, purpose, method="GET", redirect=False):
        t = super().check_url(url, purpose=purpose, method=method, redirect=redirect)
        if t.host == "unresolvable.example":
            return dataclasses.replace(t, route="proxy", proxy="http://127.0.0.1:7890", ip=None)
        if t.category in ("public", "fakeip"):
            return dataclasses.replace(t, route="proxy", proxy="http://127.0.0.1:7890")
        return t


class TestNetRules(Base):
    def test_check_url_passthrough_and_kwargs(self):
        pol = build_policy(self.fh, policy={"net": {"http_deny": ["127.0.0.1:9090"]}})
        e = eff(pol)
        e.check_url("http://example.com/x", purpose="http", method="POST", redirect=True)
        self.assertEqual(pol.net_guard.calls[-1], ("http://example.com/x", "http", "POST", True))
        self.assertEqual(pol.net_guard.kwargs["http_deny"], ["127.0.0.1:9090"])
        self.assertIn("hf-mirror.com", build_policy(self.fh, models={"hf_endpoint": "https://hf-mirror.com"})
                      .net_guard.kwargs["download_allow"])

    def test_loopback_write(self):
        endpoints = [{"name": "lm", "base_url": "http://127.0.0.1:1234/v1"}]
        view = lambda: {"endpoints": ["http://127.0.0.1:8000/v1"], "code_dirs": []}  # noqa: E731
        pol = build_policy(self.fh, model_endpoints=endpoints)
        g = pol.effective(CAUTIOUS, paused=False, services=view)
        self.assertRule(lambda: g.check_url("http://127.0.0.1:2375/containers/create", purpose="http",
                                            method="POST"), "policy_denied", "loopback_write")
        g.check_url("http://127.0.0.1:2375/containers/json", purpose="http", method="GET")
        g.check_url("http://127.0.0.1:8000/v1/chat/completions", purpose="http", method="POST")
        g.check_url("http://127.0.0.1:8000/v1/chat/completions/", purpose="model", method="POST")
        g.check_url("http://127.0.0.1:1234/v1/embeddings", purpose="http", method="POST")
        self.assertRule(lambda: g.check_url("http://127.0.0.1:8000/prompt", purpose="http", method="POST"),
                        "policy_denied", "loopback_write")
        self.assertRule(lambda: g.check_url("http://127.0.0.1:2375/v1/chat/completions", purpose="model",
                                            method="POST"), "policy_denied", "loopback_write")
        pol.effective(OPERATOR, paused=False).check_url("http://127.0.0.1:2375/x", purpose="http", method="POST")
        allow = build_policy(self.fh, policy={"exec": {"mode": "allowlist"}})
        self.assertRule(lambda: eff(allow, OPERATOR).check_url("http://127.0.0.1:2375/x", purpose="http",
                                                               method="POST"), "policy_denied", "loopback_write")

    def test_pinned_writes_rule_5c(self):
        pol = build_policy(self.fh, guard=_ProxyGuard)
        g = eff(pol, FILES)
        t = g.check_url("https://api.example.com/x", purpose="http", method="POST")
        self.assertTrue(t.pin_ip)
        self.assertEqual(t.ip, "93.184.216.34")
        self.assertFalse(g.check_url("https://api.example.com/x", purpose="http").pin_ip)
        self.assertFalse(eff(pol, OPERATOR).check_url("https://api.example.com/x", purpose="http",
                                                      method="POST").pin_ip)
        self.assertRule(lambda: g.check_url("https://unresolvable.example/x", purpose="http", method="POST"),
                        "policy_denied", "loopback_write")
        g.check_url("https://unresolvable.example/x", purpose="http", method="GET")
        pol2 = build_policy(self.fh, guard=lambda **kw: helpers.FakeNetGuard(resolve={"tun.example": ["198.18.0.5"]},
                                                                             **kw))
        self.assertRule(lambda: eff(pol2, FILES).check_url("https://tun.example/", purpose="http", method="PUT"),
                        "policy_denied", "loopback_write")

    def test_secret_write(self):
        st = SecretStore(self.fh.data / "secrets.json", passthrough=(), env={})
        st.set("GITHUB_TOKEN", "ghp_abcdefghijklmnopqrstuvwxyz0123")
        pol = build_policy(self.fh, secrets=st)
        self.assertRule(lambda: eff(pol, READNET).auth_spec("GITHUB_TOKEN", method="POST"), "policy_denied",
                        "secret_write")
        a = eff(pol, READNET).auth_spec("GITHUB_TOKEN")
        self.assertEqual((a.header, a.value), ("Authorization", "Bearer ghp_abcdefghijklmnopqrstuvwxyz0123"))
        self.assertIn("github.com", a.hosts)
        eff(pol, OPERATOR).auth_spec("GITHUB_TOKEN", method="POST")


class TestKillGuard(Base):
    def test_kill_guard(self):
        e = eff(build_policy(self.fh))
        prot = {os.getpid(): "hive node", 4242: "job:j_x"}
        self.assertRule(lambda: e.check_kill(4242, {"name": "python"}, protected=prot), "policy_denied",
                        "protected_process")
        self.assertRule(lambda: e.check_kill(1, {"name": "init"}, protected={}), "policy_denied", "kill_protect")
        self.assertRule(lambda: e.check_kill(999, {"name": "SSHD"}, protected={}), "policy_denied", "kill_protect")
        self.assertRule(lambda: e.check_kill(999, {"name": "svchost"}, protected={}), "policy_denied", "kill_protect")
        self.assertRule(lambda: e.check_kill(999, {"name": "python", "cmdline": ["python", "-m", "hive", "node",
                                                                                   "run"]}, protected={}),
                        "policy_denied", "kill_protect")
        self.assertRule(lambda: e.check_kill(999, {"name": "python", "cmdline": ["python3", "hive.pyz", "_shim"]},
                                             protected={}), "policy_denied", "kill_protect")
        if hasattr(os, "getuid"):
            self.assertRule(lambda: e.check_kill(999, {"name": "x", "uid": os.getuid() + 1}, protected={}),
                            "policy_denied", "other_user")
        e.check_kill(999, {"name": "python", "cmdline": ["python", "train.py"], "uid": os.getuid()}, protected={})
        run = self.fh.data / "run"
        run.mkdir(exist_ok=True)
        (run / "watchdog.pid").write_text("777")
        self.assertRule(lambda: e.check_kill(777, {"name": "sh"}, protected={}), "policy_denied", "kill_protect")


class TestPermissive(unittest.TestCase):
    def test_permissive(self):
        with tempfile.TemporaryDirectory() as d:
            pol = Policy.permissive(Path(d))
            e = pol.effective(OPERATOR, paused=False)
            e.resolve_path("/etc/hostname")
            e.check_command("sudo reboot", "bash")
            self.assertEqual(pol.roots(), [{"path": "*", "mode": "rw"}])
            self.assertIn("private", pol.http_allow)
            self.assertRaises(HiveError, e.resolve_path, str(Path(d) / "data" / "x"))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.name == "nt", "Windows-only PathGuard/CommandGuard paths")
class TestWindowsOnly(Base):  # pragma: no cover - runs on Windows CI
    def test_short_names_mixed_drives_and_batch(self):
        pol = build_policy(self.fh, policy={"fs": {"roots": [{"path": "~", "mode": "rw"},
                                                             {"path": "C:\\Program Files", "mode": "ro"}]}})
        e = eff(pol)
        p = e.resolve_path("C:\\PROGRA~1")
        self.assertNotIn("~1", str(p))
        other = "D:\\x" if not str(self.fh.home).upper().startswith("D:") else "E:\\x"
        self.assertRule(lambda: e.resolve_path(other), "policy_denied", "fs_roots")
        self.assertRule(lambda: e.resolve_path("~\\a.txt:x"), "policy_denied", "ads")
        self.assertRule(lambda: e.resolve_path("~\\con.txt"), "policy_denied", "device_name")
        self.assertRule(lambda: e.resolve_path("\\\\attacker\\x"), "policy_denied", "unc")
        tooldir = self.fh.base / "sysbin"
        tooldir.mkdir()
        (tooldir / "tool.cmd").write_text("@echo ok\r\n")
        with mock.patch.dict(os.environ, {"PATH": str(tooldir)}):
            allow = build_policy(self.fh, policy={"exec": {"mode": "allowlist", "command_allow": ["tool.*"]}})
        self.assertRule(lambda: eff(allow).check_command(["tool"], "auto"), "policy_denied", "batch_target")
