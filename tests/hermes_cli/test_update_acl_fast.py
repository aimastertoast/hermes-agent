"""The fork's incremental ACL repair must repair what can break -- and only that.

Upstream's repair re-grants the user's ACE on every entry under the Hermes home: 1,020,881
entries and 22 minutes on this install, of which ~99.9% re-grants an ACE that is already
there. The fork's repair does that work once, stamps the time, and afterwards repairs only
the directories created since -- a directory is the only thing that can break the tree
again, and the files inside it are fixed along with it.

These tests pin the guarantee the incremental pass rests on: a directory created before the
stamp is skipped, a directory created after it is repaired -- also when it sits inside an
old directory, which is where the update actually creates them -- and a repair that did
not finish leaves no stamp behind.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import update_acl_fast
from hermes_cli.update_acl_fast import (
    _iter_new_dirs,
    read_stamp,
    repair_home_acl,
    stamp_path,
    write_stamp,
)


def _boom(*args, **kwargs):
    raise AssertionError("the full tree repair must not run")


def _use_home(monkeypatch, home: Path) -> None:
    """Point the repair at *home* instead of the real Hermes home."""
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: str(home))


def test_the_stamp_round_trips(tmp_path: Path) -> None:
    write_stamp(tmp_path, 1234.5)

    assert read_stamp(tmp_path) == 1234.5
    assert stamp_path(tmp_path).is_file()


def test_a_tree_that_was_never_repaired_has_no_stamp(tmp_path: Path) -> None:
    assert read_stamp(tmp_path) is None


def test_a_directory_created_before_the_stamp_is_not_visited(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "old").mkdir(parents=True)
    (home / "old" / "deeper").mkdir()

    visited = set(_iter_new_dirs(home, time.time() + 1))

    assert visited == set()


def test_a_new_directory_inside_an_old_one_is_visited(tmp_path: Path) -> None:
    """The update creates directories inside old ones (profiles/<id>/sessions/<day>).
    The old parent was repaired, but the new child inside it is the broken one."""
    home = tmp_path / "home"
    (home / "old").mkdir(parents=True)
    since = time.time()
    time.sleep(0.05)
    (home / "old" / "new").mkdir()

    visited = set(_iter_new_dirs(home, since))

    assert visited == {home / "old" / "new"}


def test_a_directory_created_after_the_stamp_is_visited(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "new" / "deeper").mkdir(parents=True)
    (home / "new" / "deeper" / "file.txt").write_text("x", encoding="utf-8")

    visited = set(_iter_new_dirs(home, 0))

    assert visited == {home / "new", home / "new" / "deeper"}


@pytest.mark.skipif(os.name != "nt", reason="a Windows junction")
def test_a_junction_is_never_entered(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / "target"
    target.mkdir(parents=True)
    junction = home / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                   check=True, capture_output=True)

    visited = set(_iter_new_dirs(home, 0))

    assert junction not in visited
    assert target in visited


def test_the_incremental_repair_grants_only_the_directories_created_since_the_stamp(
    tmp_path: Path, monkeypatch,
) -> None:
    import hermes_dir_acl

    home = tmp_path / "home"
    (home / "old").mkdir(parents=True)
    (home / "new").mkdir()
    write_stamp(home, time.time() + 1)  # everything below was created before the stamp
    granted: list[Path] = []
    monkeypatch.setattr(hermes_dir_acl, "grant_user_access",
                        lambda path, **kwargs: granted.append(Path(path)) or True)
    monkeypatch.setattr(hermes_dir_acl, "grant_tree_user_access", _boom)
    _use_home(monkeypatch, home)

    assert repair_home_acl(elevated=True) is True

    assert granted == [], "the repair touched a directory the last repair had already fixed"


def test_the_incremental_repair_repairs_a_directory_created_after_the_stamp(
    tmp_path: Path, monkeypatch,
) -> None:
    import hermes_dir_acl

    home = tmp_path / "home"
    (home / "old").mkdir(parents=True)
    since = time.time()
    time.sleep(0.05)
    (home / "old" / "new").mkdir()
    write_stamp(home, since)
    granted: list[Path] = []
    monkeypatch.setattr(hermes_dir_acl, "grant_user_access",
                        lambda path, **kwargs: granted.append(Path(path)) or True)
    monkeypatch.setattr(hermes_dir_acl, "grant_tree_user_access", _boom)
    _use_home(monkeypatch, home)

    assert repair_home_acl(elevated=True) is True

    assert granted == [home / "old" / "new"], "the new directory inside the old one was missed"


def test_a_tree_that_was_never_repaired_gets_the_full_repair(tmp_path: Path, monkeypatch) -> None:
    import hermes_dir_acl

    home = tmp_path / "home"
    home.mkdir()
    repaired: list[Path] = []
    monkeypatch.setattr(hermes_dir_acl, "grant_tree_user_access",
                        lambda root, **kwargs: repaired.append(Path(root)) or (1, [], []))
    _use_home(monkeypatch, home)

    assert repair_home_acl(elevated=True) is True

    assert repaired == [home]
    assert read_stamp(home) is not None, "a successful repair must leave a stamp"


def test_a_failed_full_repair_leaves_no_stamp(tmp_path: Path, monkeypatch) -> None:
    import hermes_dir_acl

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(hermes_dir_acl, "grant_tree_user_access", lambda root, **kwargs: (0, [], []))
    _use_home(monkeypatch, home)

    with pytest.raises(RuntimeError):
        repair_home_acl(elevated=True)

    assert read_stamp(home) is None, "a failed repair must not claim the tree is healthy"


def test_a_not_elevated_update_repairs_nothing(tmp_path: Path, monkeypatch) -> None:
    import hermes_dir_acl

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(hermes_dir_acl, "grant_tree_user_access", _boom)
    monkeypatch.setattr(hermes_dir_acl, "grant_user_access", _boom)
    _use_home(monkeypatch, home)

    assert repair_home_acl(elevated=False) is False

    assert read_stamp(home) is None


def test_the_windows_acl_step_uses_the_incremental_repair(monkeypatch) -> None:
    """The upstream-owned step must delegate to the fork's repair, not to the full walk."""
    import hermes_cli.update_windows_acl as step
    import hermes_dir_acl

    monkeypatch.setattr(update_acl_fast, "repair_home_acl", lambda **kwargs: True)
    monkeypatch.setattr(hermes_dir_acl, "grant_tree_user_access", _boom)

    step.repair_windows_home_acl(elevated=True)
