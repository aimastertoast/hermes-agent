"""The fork's merge policy for upstream-owned files — FORK-OWNED.

``.gitattributes`` marks a set of upstream-owned files ``merge=union`` so a routine
upstream rewrite cannot abort ``hermes update`` (the GUI's "Update failed (exit 1)"
banner). The union driver resolves such a conflict as "upstream's file plus the
lines the fork added", which is only correct while the fork's delta on that file
is a PURE INSERTION.

The moment the fork CHANGES or REMOVES a line in a listed file, union keeps
upstream's line and the fork's added block beside it: a duplicate declaration, or
a fork change silently dropped. This test fails when that invariant breaks, so
the file leaves the list in ``.gitattributes`` instead of corrupting a merge.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GITATTRIBUTES = REPO / ".gitattributes"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    # timeout: a treeless partial clone can make a plain diff hang on a lazy-fetch, and a
    # hanging policy test is worse than a failing one.
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False,
        timeout=60,
    )


def _union_paths() -> list[str]:
    """Paths listed with ``merge=union`` in the repo's .gitattributes."""
    if not GITATTRIBUTES.exists():
        return []
    paths = []
    for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "merge=union" not in line:
            continue
        paths.append(line.split()[0].removeprefix("/"))
    return paths


@lru_cache(maxsize=1)
def _fork_delta() -> dict[str, tuple[int, int]] | None:
    """``{path: (added, deleted)}`` for the fork's delta against the NEXT merge's base.

    The base is the common ancestor of HEAD and the upstream ref, not the ref itself:
    measuring against the ref would count upstream's own newer commits as if the fork
    had removed those lines. None when there is no upstream ref to measure against.

    One unfiltered ``git diff`` for the whole list: passing a path like
    ``gateway/run.py`` in argv trips tests/conftest.py's live-system guard, which
    cannot tell a read-only diff from a gateway spawn.
    """
    ref = next(
        (r for r in ("origin/main",) if _git("rev-parse", "--verify", "--quiet", r).returncode == 0),
        None,
    )
    if ref is None:
        return None
    base = _git("merge-base", "HEAD", ref).stdout.strip()
    if not base:
        return None
    result = _git("diff", "--numstat", base, "HEAD")
    assert result.returncode == 0, result.stderr
    delta: dict[str, tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue  # a binary file renders as "-\t-\t<path>"
        added, deleted, path = parts
        if added.isdigit() and deleted.isdigit():
            delta[path] = (int(added), int(deleted))
    return delta


@pytest.mark.parametrize("path", _union_paths())
def test_union_listed_file_has_a_pure_insertion_fork_delta(path: str) -> None:
    """The fork only ADDS lines in a union-listed file (no line changed, none removed)."""
    delta = _fork_delta()
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    if path not in delta:
        pytest.skip(f"{path} no longer differs from upstream: remove it from .gitattributes")
    added, deleted = delta[path]
    assert added > 0, f"{path} has no fork delta left: remove it from .gitattributes"
    assert deleted == 0, (
        f"{path} is union-listed in .gitattributes but the fork's delta on it removes "
        f"{deleted} line(s). Union would keep upstream's line and the fork's added block "
        f"side by side. Move the fork's change into a fork-owned module, or drop the file "
        f"from the union list so the conflict stays loud."
    )


def test_union_list_covers_only_existing_files() -> None:
    """A listed path that does not exist is a typo that silently disables the policy."""
    for path in _union_paths():
        assert (REPO / path).is_file(), f"{path} is listed in .gitattributes but does not exist"


# Upstream rewrites these in (almost) every commit -- the per-locale catalogs
# grow a key per feature -- so a fork line in them is a conflict waiting to
# happen, and a conflict aborts ``hermes update`` (the GUI's "Update failed
# (exit 1)" banner). The fork's update strings therefore live in
# ``apps/desktop/src/i18n/fork.ts`` (fork-owned), and the catalogs upstream
# ships are byte-identical again.
MIGRATED_TO_FORK_OWNED = (
    "apps/desktop/src/i18n/en.ts",
    "apps/desktop/src/i18n/types.ts",
    "apps/desktop/src/i18n/fr.ts",
    "apps/desktop/src/i18n/es.ts",
    "apps/desktop/src/i18n/de.ts",
    "apps/desktop/src/i18n/overlay-gaps.json",
)


@pytest.mark.parametrize("path", MIGRATED_TO_FORK_OWNED)
def test_migrated_file_carries_no_fork_delta(path: str) -> None:
    """A migrated file must be byte-identical to upstream."""
    delta = _fork_delta()
    if delta is None:
        pytest.skip("no upstream ref to measure the fork's delta against")
    assert path not in delta, (
        f"{path} carries a fork delta ({delta[path][0]} added, {delta[path][1]} removed). "
        "That content belongs in a fork-owned module: upstream rewrites this file in "
        "almost every commit, and the conflict aborts the GUI update."
    )
