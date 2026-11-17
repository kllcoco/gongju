"""``python -m hive`` / ``python hive.pyz``: dispatches ``_shim``/``_download``/``_update`` before importing the
argparse-heavy CLI; pythonw stdio guard (design §3, §13.1, WP6).

D56 (§4.2 ``prune_cwd_path``): the cwd is pruned from ``sys.path`` here, BEFORE any ``hive.*`` import, using only
``os`` and ``sys`` (both already loaded by the interpreter). ``hive.cli``/``hive._shim`` import ``hive.util``, whose
module-level stdlib imports (ipaddress, subprocess, json, …) would otherwise load from a writable cwd that ``python -m``
puts on ``sys.path`` (3.9/3.10 always; 3.11+ without ``-P``). This module and ``hive/__init__.py`` therefore import
nothing else at module level (not even ``__future__``, which is a real runtime import). ``util.prune_cwd_path()`` stays
in ``cli.main``, ``_shim.main`` and ``_download.main`` for the console-script path.
"""
import os
import sys


def _prune_cwd_path():
    """Remove ``""``, ``"."`` and the realpath of the cwd from sys.path unless the hive package lives directly in the
    cwd (source checkout run from its own folder). Same rule as ``util.prune_cwd_path``, stdlib-import free."""
    try:
        cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        return
    try:
        pkg_parent = os.path.normcase(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    except (OSError, ValueError, NameError):
        pkg_parent = None
    if pkg_parent == cwd:
        return
    keep = []
    for entry in sys.path:
        if entry in ("", "."):
            continue
        try:
            if os.path.normcase(os.path.realpath(entry)) == cwd:
                continue
        except (OSError, ValueError, TypeError):
            pass
        keep.append(entry)
    sys.path[:] = keep


def _main():
    _prune_cwd_path()   # D56: first statement, before any `from hive import …`
    if sys.stdout is None or sys.stderr is None:  # pythonw guard (§13.1)
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "_shim":
        from hive import _shim
        return _shim.main(sys.argv[2:])
    if cmd == "_download":
        from hive import _download
        return _download.main(sys.argv[2:])
    if cmd == "_update":
        from hive import dist
        dist.self_update(None)   # WP5 wires `hive _update` (fetches only when relay.auto_update)
        return 0
    from hive import cli
    return cli.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(_main())
