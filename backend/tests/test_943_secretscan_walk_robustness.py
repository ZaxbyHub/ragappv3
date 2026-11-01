"""Deterministic vanish-tolerance guardrail for check_secretscan's walks (#943).

Issue #943's race: a directory disappears after a repo-wide walk's parent
listing has recorded it, so the walk's RECURSION into that directory raises
``FileNotFoundError`` from inside the traversal and the whole scan crashes
(the production traceback lives in issue #943's CI log). The fix walks with
``os.scandir`` and skips vanished entries at both guard points — the
per-entry ``except OSError`` around the stat calls and the ``except
OSError: continue`` around each recursion-time listing.

This test choreographs that window deterministically per walker — no
threads, no timing race. The fake ``os.scandir`` is context-manager
capable (the stdlib contract) and, when asked to LIST the vanished
directory itself, removes it from disk and raises the production
``FileNotFoundError`` — exactly what happens when the directory vanishes
between the parent's listing and the recursion. Each parametrized arm
builds its own module, tree, and wrapper, and both walkers must skip the
vanished subtree and complete. A surviving nested directory pins that the
walkers actually RECURSE (a root-only walker fails the arms), and a
further test drives ``main()`` — the production crash locus — through the
same choreography. The root-vanished boundary is covered separately.

The pre-fix ``Path.rglob("*")`` shape fails the arms with the production
signature under this fake (pathlib's selector does not tolerate the
recursion-time FileNotFoundError), so the guardrail bites the original
defect, not a harness artifact.
"""

import importlib.util
import os as _os
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "check_secretscan.py"
)


def _load_secretscan(monkeypatch, root: Path):
    spec = importlib.util.spec_from_file_location(
        "check_secretscan_under_test", _SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", root)
    return module


def _make_tree(root: Path) -> None:
    (root / "keep").mkdir()
    (root / "keep" / "sub").mkdir()
    (root / "keep" / "sub" / "nested.txt").write_text("x", encoding="utf-8")
    (root / "keep" / "a.txt").write_text("x", encoding="utf-8")
    (root / "vanish" / "sub").mkdir(parents=True)
    (root / "vanish" / "sub" / "deep.txt").write_text("x", encoding="utf-8")
    (root / "plain").mkdir()


def _rmtree_raw(path: Path) -> None:
    # os.listdir-based delete: shutil.rmtree/os.walk would re-enter the
    # patched os.scandir, whose replacement does not implement the full
    # context-manager resource semantics rmtree relies on.
    for name in _os.listdir(path):
        child = path / name
        if child.is_dir() and not child.is_symlink():
            _rmtree_raw(child)
        else:
            _os.remove(child)
    _os.rmdir(path)


class _Listing:
    """Context-manager-capable iterator over a snapshot of a listing."""

    def __init__(self, entries):
        self._entries = list(entries)

    def __iter__(self):
        return iter(self._entries)

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc):
        return False


def _install_vanishing_scandir(monkeypatch, module, vanish: Path) -> None:
    real_scandir = module.os.scandir

    def _scandir_vanish_at_recursion(path="."):
        if Path(path) == vanish:
            # The race window: the parent listing already recorded this
            # directory, but it is gone by recursion time. Remove it for
            # real and raise the production error from the listing call.
            _rmtree_raw(vanish)
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return _Listing(list(real_scandir(path)))

    monkeypatch.setattr(module.os, "scandir", _scandir_vanish_at_recursion)


@pytest.mark.parametrize("walker", ["files", "dirs"])
def test_walker_tolerates_vanish_at_recursion(
    monkeypatch, tmp_path, walker
):
    root = tmp_path / ("repo-" + walker)
    root.mkdir()
    _make_tree(root)
    module = _load_secretscan(monkeypatch, root)
    _install_vanishing_scandir(monkeypatch, module, root / "vanish")

    if walker == "files":
        result = module.on_disk_files()
        assert not any(p.startswith("vanish/") for p in result), result
        assert "keep/a.txt" in result, result
        assert "keep/sub/nested.txt" in result, result
    else:
        result = module.on_disk_dirs()
        # The walk COMPLETES and the vanished subtree's children are absent.
        # The vanished directory's own name may remain — it was a directory
        # at listing time, and both walkers use listing-time semantics for
        # their own entries (same as on_disk_files since #890's fix);
        # re-statting every entry would move the race, not close it.
        assert "plain" in result, result
        assert "keep/sub" in result, result  # recursion actually happens
        assert not any(p.startswith("vanish/") for p in result), result


def test_main_completes_through_the_vanish_choreography(
    monkeypatch, tmp_path, capsys
):
    # The #943 crash locus was main()'s walk call sites, not the helpers in
    # isolation — drive the whole scan through the same choreography.
    root = tmp_path / "repo-main"
    root.mkdir()
    _make_tree(root)
    (root / ".secretscanignore").write_text("keep/**\n", encoding="utf-8")
    module = _load_secretscan(monkeypatch, root)
    _install_vanishing_scandir(monkeypatch, module, root / "vanish")

    assert module.main() == 0


def test_walkers_tolerate_root_vanishing_entirely(monkeypatch, tmp_path):
    # Boundary: the walk root itself can vanish before the first scandir —
    # the OSError arm must skip the whole walk rather than crash (#943
    # class, boundary case). On Windows the pre-fix rglob shape also
    # returned [] here, so this arm pins the fix's boundary behavior but is
    # not a mutant discriminator.
    root = tmp_path / "gone"
    root.mkdir()
    module = _load_secretscan(monkeypatch, root)
    root.rmdir()
    assert module.on_disk_files() == []
    assert module.on_disk_dirs() == []
