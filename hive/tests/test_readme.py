"""Phase 2 release: every ``hive …`` command shown in README.md exists in the CLI (subcommand path and flags parse
with the real argparse parsers; nothing is executed), and the README links point at files that exist."""
from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
import unittest
from pathlib import Path
from typing import List, Tuple

from hive import cli, mcp

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

# a command starts with `hive`, `~/.venvs/hive/bin/hive`, `/workspace/.hive/hive` or `python3 hive.pyz`, optionally
# after `VAR=value ` environment assignments (`HIVE_HOME=/workspace/.hive python3 hive.pyz join --install`)
_START = re.compile(r"(?:[A-Z_]+=\S+\s+)*(?:~/\.venvs/hive/bin/hive|/workspace/\.hive/hive|python3? hive\.pyz|hive)"
                    r"(?=\s|$)")


def _commands() -> List[Tuple[int, str]]:
    """(line number, command text after the program name) for code-block lines and inline code spans."""
    out: List[Tuple[int, str]] = []
    in_block = False
    for no, line in enumerate(README.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            # each shell segment of a code line (`a && b`, `a; b`) that starts with the program
            for seg in re.split(r"&&|;|\|\|", line.split(" #", 1)[0]):
                m = _START.match(seg.strip())
                if m:
                    out.append((no, seg.strip()[m.end():]))
        else:
            for span in re.findall(r"`([^`]+)`", line):
                m = _START.match(span)
                if m:
                    out.append((no, span[m.end():]))
    return out


def _argv(text: str) -> List[str]:
    # Markdown tables escape pipes as \|; a quoted pipe (`hive sh … -- 'ls | head'`) stays inside its word
    words = shlex.split(text.replace("\\|", "|"), comments=False)
    argv: List[str] = []
    for w in words:
        if w in ("&&", "||", "|", ";") or w.startswith("#"):
            break
        argv.append(w.replace("[", "").replace("]", ""))
    return [w for w in argv if w]


def _parse(argv: List[str]) -> argparse.Namespace:
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] == "--connection" else 1
    rest = argv[i:]
    if argv[:1] in (["--version"], ["-V"]):
        return argparse.Namespace(version=True)   # cli.main answers it before the parser
    if rest and rest[0] == "mcp":
        return mcp.build_parser().parse_args(rest[1:])
    if rest and rest[0] == "sh":
        if len(rest) < 2:
            raise ValueError("hive sh needs a node")
        head = rest[1:rest.index("--")] if "--" in rest else rest[1:]
        ap = argparse.ArgumentParser(prog="hive sh")
        ap.add_argument("node")
        ap.add_argument("--shell")
        ap.add_argument("--cwd")
        ap.add_argument("--timeout", type=float)
        ap.add_argument("--connection")
        ap.add_argument("--approval")
        return ap.parse_args(head)
    ns = cli.build_parser().parse_args(rest)
    if getattr(ns, "fn", None) is None and not getattr(ns, "version", False):
        raise ValueError("incomplete command (a group without its subcommand)")
    return ns


class TestReadmeCommands(unittest.TestCase):
    def test_readme_exists_and_is_chinese(self):
        text = README.read_text(encoding="utf-8")
        for needle in ("蜂巢 Hive", "Tailscale", "Claude Desktop", "Claude Code", "Cursor", "Cherry Studio",
                       "Open WebUI", "hive doctor", "hive node panic", "--cautious", "docs/DESIGN.md", "WSL2",
                       "RunPod", "hive.pyz"):
            self.assertIn(needle, text)

    def test_every_command_parses(self):
        cmds = _commands()
        self.assertGreater(len(cmds), 80)
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        top = set(sub.choices) | {"sh", "mcp"}
        bad = []
        for no, text in cmds:
            argv = _argv(text)
            if not argv:
                continue
            if len(argv) == 1 and argv[0] in top:
                continue   # prose naming a command (`hive sh`, `hive call`), not an invocation
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    _parse(argv)
            except SystemExit as e:
                if e.code == 0 and "--help" in argv:
                    continue
                bad.append(f"README.md:{no}: hive {' '.join(argv)}: {err.getvalue().strip().splitlines()[-1:]}")
            except ValueError as e:
                bad.append(f"README.md:{no}: hive {' '.join(argv)}: {e}")
        self.assertEqual(bad, [])

    def test_unknown_flag_is_caught(self):
        # the checker itself must reject a command that does not exist
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _parse(["node", "token", "add", "x", "--no-such-flag"])
            with self.assertRaises(SystemExit):
                _parse(["frobnicate"])
        with self.assertRaises(ValueError):
            _parse(["hub"])

    def test_links_exist(self):
        text = README.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#]+)\)", text):
            if target.startswith(("http://", "https://")):
                continue
            self.assertTrue((ROOT / target).exists(), target)

    def test_anchor_links_exist(self):
        text = README.read_text(encoding="utf-8")
        prose = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)   # `# comments` in code blocks are no headings
        anchors = set()
        for h in re.findall(r"^#{1,6} (.+)$", prose, flags=re.M):
            a = h.strip().lower()
            a = re.sub(r"[^\w\- 一-鿿]", "", a)
            anchors.add(a.replace(" ", "-"))
        for target in re.findall(r"\]\(#([^)]+)\)", text):
            self.assertIn(target, anchors)


if __name__ == "__main__":
    unittest.main()
