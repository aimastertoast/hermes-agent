"""FORK: an incremental post-update ACL repair.

Upstream's ``windows_home_acl`` step walks EVERY entry under the Hermes home and grants the
user an ACE on each one, because the pywin32 write does not propagate to the entries that
already exist. Measured on this install: 1,020,881 entries (117,733 directories) and 1,336
seconds -- 22 minutes of every update, of which ~99.9% re-grants an ACE that is already
there (4,000 sampled entries: 8 without the user's ACE, all hard links inside an extracted
tool tree, which upstream refuses to grant through on purpose).

This module gives the same guarantee for a fraction of the cost:

* A directory the repair granted carries an INHERITABLE user ACE (OI|CI), so every file
  created inside it afterwards inherits it. Only a file that PREDATES its directory's grant
  can miss the ACE, and such a file lives inside a directory this repair has already fixed.
* So the tree is repaired once, the time is stamped, and every later repair looks only at
  the directories created since the stamp. A new directory is the only way the tree can
  break again: an elevated ``os.mkdir(path, 0o700)`` applies a protected DACL of
  SYSTEM/Administrators/OWNER RIGHTS (CPython 3.13+), and everything created inside it
  inherits that broken DACL -- which the directory's own repair fixes along with it.
* The later repair still WALKS every directory (117,733 of them, 18 s): a directory created
  before the stamp can receive a new one after it, and only the new one is broken. What it
  does not do is re-grant the million entries that are already healthy.

The stamp lives in ``<home>/.acl-repair-stamp.json`` and holds the time the repair STARTED,
so an entry created while a repair runs is newer than the stamp and the next run still sees
it. Delete the stamp to force a full repair (after restoring a home tree from a backup, say).

ponytail: the stamp is the whole incremental guarantee. A tree broken by something OUTSIDE
an elevated Hermes process (a manual ``icacls /reset`` elsewhere, a restored backup) is not
noticed by the incremental pass -- delete the stamp to force the full repair.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger("hermes_cli.update_cmd")

#: The file in the Hermes home that records when the tree was last fully repaired.
STAMP_NAME = ".acl-repair-stamp.json"

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def stamp_path(home: str | Path) -> Path:
    """Where the repair stamp lives for *home*."""
    return Path(home) / STAMP_NAME


def read_stamp(home: str | Path) -> float | None:
    """When the tree under *home* was last fully repaired (epoch seconds), or None."""
    try:
        data = json.loads(stamp_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    when = data.get("repaired_at") if isinstance(data, dict) else None
    return float(when) if isinstance(when, (int, float)) else None


def write_stamp(home: str | Path, when: float) -> None:
    """Record *when* (epoch seconds) as the last full repair of the tree under *home*."""
    stamp_path(home).write_text(json.dumps({"repaired_at": when}), encoding="utf-8")


def _iter_new_dirs(root: Path, since: float) -> Iterator[Path]:
    """Yield every directory under *root* created after *since* (epoch seconds).

    The walk visits EVERY directory, not only the new ones: a directory that was created
    before the stamp can still receive a new one afterwards (the update creates
    ``<home>/profiles/<id>/sessions/<day>``), and the new one is the broken one -- so
    pruning the walk at the old directories would miss it. A reparse point is never
    entered: an elevated grant must not follow a link out of the home.
    """
    stack = [Path(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            child = Path(entry.path)
            if info.st_ctime > since:
                yield child
            stack.append(child)


def _full_repair(home: Path) -> None:
    """Upstream's repair, unchanged in behaviour: grant every entry under the home.

    This is the one-off cost of the incremental repair -- it runs when there is no stamp.
    """
    from hermes_dir_acl import grant_tree_user_access

    granted, skipped, failed = grant_tree_user_access(home, pin_root=True, elevated=True)
    if failed and not granted:
        path, reason = failed[0]
        raise RuntimeError(f"could not restore user access to {home}: {path}: {reason}")
    if not granted:
        why = f"{skipped[0]} is a junction, symlink or hard link" if skipped else "nothing was walkable"
        raise RuntimeError(f"could not restore user access to {home}: nothing was repaired ({why})")
    if failed:
        log.warning("Restoring user access under %s failed for %d entr%s, e.g. %s: %s", home, len(failed),
                   "y" if len(failed) == 1 else "ies", failed[0][0], failed[0][1])


def _incremental_repair(home: Path, since: float) -> None:
    """Grant the directories created since the last repair, and their subtrees with them."""
    from hermes_dir_acl import grant_user_access

    checked = 0
    for directory in _iter_new_dirs(home, since):
        checked += 1
        if not grant_user_access(directory, elevated=True, repair_children=True):
            log.warning("Could not restore user access to %s while elevated", directory)
    log.info("Elevated ACL repair checked %d director%s under %s created since the last repair",
             checked, "y" if checked == 1 else "ies", home)


def repair_home_acl(*, elevated: bool | None = None) -> bool:
    """Restore the user's access to the Hermes home tree. True when the repair ran here.

    False means "nothing to do": the update was not elevated, so it left no
    Administrators-only entry behind and there is nothing to repair.

    *elevated* is the host fact (default: probed; False on every host but an elevated
    Windows one).
    """
    from hermes_dir_acl import process_is_elevated
    from hermes_constants import get_hermes_home

    if not (process_is_elevated() if elevated is None else elevated):
        # Only an elevated update leaves Administrators-only entries, and only an elevated
        # token can rewrite them.
        return False
    home = Path(get_hermes_home())
    # Captured BEFORE the repair, written AFTER it: an entry created while the repair runs
    # is newer than the stamp, so the next run still looks at it.
    started = time.time()
    stamp = read_stamp(home)
    if stamp is None:
        _full_repair(home)
    else:
        _incremental_repair(home, stamp)
    write_stamp(home, started)
    return True
