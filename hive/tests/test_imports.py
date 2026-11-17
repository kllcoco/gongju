"""Layout and review gates (§3, §22.3): 54 importable modules with § docstrings, stdlib only, import rules, greps."""
from __future__ import annotations

import ast
import importlib
import pkgutil
import subprocess
import sys
import sysconfig
import unittest
from pathlib import Path

import hive

PKG = Path(hive.__file__).resolve().parent
ROOT = PKG.parent

LAYOUT = [
    "hive", "hive.__main__", "hive.errors", "hive.util", "hive.catalog", "hive.schema", "hive.actions",
    "hive.actions.node", "hive.actions.system", "hive.actions.shell", "hive.actions.jobs", "hive.actions.services",
    "hive.actions.files", "hive.actions.net", "hive.actions.model", "hive.config", "hive.auth", "hive.policy",
    "hive.limits", "hive.approvals", "hive.audit", "hive.secretstore", "hive.httpserver", "hive.node",
    "hive.proctree", "hive.winapi", "hive._shim", "hive.textlog", "hive.jobs", "hive.services", "hive.presets",
    "hive.fsops", "hive.netutil", "hive.netproxy", "hive.hfhub", "hive._download", "hive.sysinfo", "hive.gpu",
    "hive.procinfo", "hive.wsl", "hive.install", "hive.doctor", "hive.transport", "hive.relay", "hive.hub",
    "hive.gateway", "hive.client", "hive.transfer", "hive.dist", "hive.tools", "hive.render", "hive.guides",
    "hive.mcp", "hive.cli",
]
WP1 = ("config", "auth", "policy", "limits", "approvals", "audit", "secretstore", "httpserver", "node")


def py_files():
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def module_name(p: Path) -> str:
    rel = p.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _stdlib_names():
    names = set(sys.builtin_module_names)
    if hasattr(sys, "stdlib_module_names"):
        names |= set(sys.stdlib_module_names)
        return names
    std = Path(sysconfig.get_paths()["stdlib"])
    for p in std.iterdir():
        if p.suffix == ".py":
            names.add(p.stem)
        elif p.is_dir() and (p / "__init__.py").exists():
            names.add(p.name)
    dyn = std / "lib-dynload"
    if dyn.is_dir():
        for p in dyn.iterdir():
            names.add(p.name.split(".", 1)[0])
    names |= {"msvcrt", "winreg", "_winapi", "nt", "fcntl", "termios", "pwd", "grp", "resource"}
    return names


class TestLayout(unittest.TestCase):
    def test_54_modules(self):
        found = [module_name(p) for p in py_files()]
        self.assertEqual(len(found), 54)
        self.assertEqual(sorted(found), sorted(LAYOUT))

    def test_every_module_imports_with_docstring(self):
        for name in LAYOUT:
            with self.subTest(module=name):
                mod = importlib.import_module(name)
                doc = mod.__doc__ or ""
                self.assertTrue(doc.strip(), name)
                if name != "hive":
                    self.assertRegex(doc, r"§\d+", name)
        names = [m.name for m in pkgutil.walk_packages(hive.__path__, "hive.")]
        self.assertEqual(len(names) + 1, 54)

    def test_package_metadata(self):
        self.assertEqual(hive.__version__, "1.0.0")
        self.assertEqual(hive.API_VERSION, 1)
        from hive import client
        self.assertIs(hive.connect, client.connect)
        with self.assertRaises(AttributeError):
            getattr(hive, "nope")

    def test_main_module(self):
        out = subprocess.run([sys.executable, "-m", "hive", "--version"], cwd=str(ROOT), capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "hive 1.0.0")


class TestGates(unittest.TestCase):
    def test_stdlib_only(self):
        std = _stdlib_names()
        for p in py_files():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                roots = []
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                for r in roots:
                    with self.subTest(file=p.name, module=r):
                        self.assertTrue(r in std or r in ("hive", "__future__", "psutil"), r)
        # psutil only inside try/except ImportError
        for p in py_files():
            src = p.read_text(encoding="utf-8")
            if "import psutil" in src:
                self.assertRegex(src, r"try:\s*\n\s*import psutil", p.name)

    def test_future_annotations_everywhere(self):
        # hive/__init__.py and hive/__main__.py import nothing before the D56 sys.path prune (a planted __future__.py
        # in the cwd would run), so they are exempt
        for p in py_files():
            if p.parent.name == "hive" and p.name in ("__init__.py", "__main__.py"):
                continue
            with self.subTest(file=p.name):
                self.assertIn("from __future__ import annotations", p.read_text(encoding="utf-8"))

    def test_no_py310_syntax(self):
        for p in py_files():
            src = p.read_text(encoding="utf-8")
            with self.subTest(file=p.name):
                tree = ast.parse(src)
                self.assertFalse(any(type(n).__name__ == "Match" for n in ast.walk(tree)))
                self.assertNotIn("kw_only=", src)
                self.assertNotIn("slots=True", src)

    def test_grep_gates(self):
        for p in py_files():
            src = p.read_text(encoding="utf-8")
            with self.subTest(file=p.name):
                self.assertNotRegex(src, r"shell\s*=\s*True")
                self.assertNotIn("CERT_NONE", src)
                if p.name not in ("render.py", "cli.py", "util.py"):
                    self.assertNotRegex(src, r"\butil\.iso\(")
                if p.name not in ("cli.py", "__main__.py"):
                    self.assertNotRegex(src, r"^\s*print\(", )

    def test_shim_download_no_function_level_hive_imports(self):
        for name in ("_shim.py", "_download.py"):
            tree = ast.parse((PKG / name).read_text(encoding="utf-8"))
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(fn):
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse(node.level > 0 or (node.module or "").startswith("hive"), name)
                    if isinstance(node, ast.Import):
                        self.assertFalse(any(a.name.startswith("hive") for a in node.names), name)

    def test_download_side_imports_no_wp1(self):
        code = ("import sys; import hive._download, hive.netutil, hive.netproxy, hive.hfhub; "
                "print(','.join(sorted(m for m in sys.modules if m.startswith('hive.'))))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        loaded = set(out.stdout.strip().split(","))
        self.assertFalse(loaded & {f"hive.{m}" for m in WP1}, loaded)
        allowed = {"hive.util", "hive.errors", "hive.netproxy", "hive.netutil", "hive.hfhub", "hive._download"}
        self.assertLessEqual(loaded, allowed)

    def test_shim_imports(self):
        code = ("import sys; import hive._shim, hive.proctree; "
                "print(','.join(sorted(m for m in sys.modules if m.startswith('hive.'))))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        loaded = set(out.stdout.strip().split(","))
        self.assertLessEqual(loaded, {"hive.util", "hive.errors", "hive.proctree", "hive.textlog", "hive.winapi",
                                      "hive._shim"})

    def test_util_imports_no_wp_module(self):
        code = "import sys; import hive.util; print(','.join(sorted(m for m in sys.modules if m.startswith('hive.'))))"
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(set(out.stdout.strip().split(",")), {"hive.util", "hive.errors"})

    def test_public_functions_documented(self):
        for p in py_files():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
                    with self.subTest(file=p.name, name=node.name):
                        self.assertTrue(ast.get_docstring(node), f"{p.name}:{node.name}")


if __name__ == "__main__":
    unittest.main()
