"""argparse CLI ``hive`` (design §19, WP6).

Phase 0 stub: ``main`` prunes the cwd from sys.path (D56) and reports that the CLI is not available yet.
This module is one of the two places allowed to print and to use ``util.iso()`` (§22.3 rules 5 and 11).
"""
from __future__ import annotations

import sys
from typing import List, Optional

from . import __version__, util


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (``hive = hive.cli:main``)."""
    util.prune_cwd_path()
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["--version"], ["-V"]):
        print(f"hive {__version__}")
        return 0
    print("hive: the command line is not available yet in this build (Phase 0)", file=sys.stderr)
    return 2


__all__ = ["main"]
