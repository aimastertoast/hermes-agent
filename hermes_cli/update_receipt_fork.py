"""Fork-owned pipeline receipt storage for the update pipeline.

Split out of ``hermes_cli.update_receipt`` so that upstream-owned module keeps
a ZERO fork delta. Upstream rewrites that file constantly; every line the fork
adds there is a line the next merge can conflict on, and a conflict aborts the
update outright. A file upstream has never heard of cannot conflict.

Two receipt systems live side by side:

* ``hermes_cli.update_receipt`` — upstream's legacy ``UpdateReceipt`` class and
  its ``logs/update_receipts/`` store (fleet / refusal receipts).
* this module — the fork's typed ``UpdateReceiptRecord`` and its
  ``<home>/update_receipts/`` store, written by the update orchestrator and
  read by the dashboard's receipt endpoints.

The only dependency on the upstream module is ``record_fact``, imported lazily
inside the function that needs it, so neither module can half-initialize the
other.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

# Receipt ids are produced by ``secrets.token_hex(8)`` — 16 lowercase hex
# chars, no path separators. A reader that takes an attacker-controlled
# receipt_id and constructs ``rdir / f"{receipt_id}.json"`` MUST reject
# anything that isn't a token_hex-shaped string: a literal ``../`` would
# let a caller escape the receipts directory, and a stray ``.json.tmp``
# would clobber the atomic-write temp file the writer depends on. The
# readers below share this guard so neither path traversal nor a colliding
# filename can reach the filesystem.
_RECEIPT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def record_verification(checks: list[dict[str, Any]], *, rolled_back: bool = False,
                        failed_check: str = "") -> None:
    """Record the post-update verification section on the active receipt.

    Uses upstream's generic ``record_fact`` (a top-level field on the receipt
    dict) rather than a receipt method of our own: the section lands under the
    same ``data["verification"]`` key either way, and reusing upstream's setter
    keeps this module's footprint on the upstream file at zero — no method has
    to be added to ``UpdateReceipt``.

    No-op when no receipt is active (same contract as ``record_step``).
    """
    from hermes_cli.update_receipt import record_fact

    record_fact("verification", {
        "checks": [dict(check) for check in checks],
        "rolled_back": bool(rolled_back),
        "failed_check": failed_check,
    })


def read_latest_pipeline_receipt_dict(home: Path) -> Optional[dict[str, Any]]:
    """Pipeline-path receipt serialized to a plain dict (for JSON transport).

    Profile-aware counterpart to the legacy ``read_latest_receipt()`` in
    ``hermes_cli.update_receipt`` — the API layer (which lives behind
    ``_config_profile_scope`` and must serialize to JSON) calls this instead
    of ``read_latest_pipeline_receipt`` directly so the dataclass → dict
    conversion is centralized and tested.

    The desktop overlay reads this from ``/api/hermes/update/receipt``,
    which sends ``?profile=X`` to land on the target profile's home. The
    orchestrator's 7-step pipeline writes receipts to ``<home>/update_receipts/``;
    reading from any other path silently returns ``None`` (the desktop then
    shows no overlay — the "no receipt" branch is the same UX as a fresh
    install).
    """
    record = read_latest_pipeline_receipt(home)
    if record is None:
        return None
    return asdict(record)


def latest_pipeline_receipt_summary(home: Path) -> Optional[dict[str, Any]]:
    """Compact summary derived from the pipeline receipt.

    The orchestrator's :class:`UpdateReceiptRecord` doesn't track
    started_at / finished_at / git SHAs (the snapshot captures state.db
    and config.yaml hashes, not commit refs), so the corresponding fields
    serialize as ``None`` — the desktop overlay treats them as "not
    available" rather than "failed". Outcome is populated; the
    fleet-equivalent list is empty because the orchestrator is single-host.
    """
    record = read_latest_pipeline_receipt(home)
    if record is None:
        return None
    try:
        return {
            "outcome": record.outcome,
            "started_at": None,
            "finished_at": None,
            "pre_sha": None,
            "post_sha": None,
            "post_version": None,
            "fleet_states": [],
        }
    except Exception:
        return None


# Pipeline receipt contract (Task 3 of the update-permanent-fix plan)
#
# Distinct from the legacy ``UpdateReceipt`` class in
# ``hermes_cli.update_receipt``: this is a typed, dataclass-based record the
# orchestrator (Task 6) and the dashboard/acknowledgement flows use to
# communicate the outcome of one update run.
# Lives at ``<home>/update_receipts/<receipt_id>.json`` (NOT ``logs/`` —
# profile-scoped, not log-scoped, so an update can never read receipts
# from a sibling profile or refuse to write because the log dir is full).
#
# Naming note: ``UpdateReceipt`` is the legacy class upstream; this dataclass
# is ``UpdateReceiptRecord`` to avoid breaking the dozens of imports that
# already bind the legacy name. Likewise the legacy ``read_latest_receipt()``
# stays signature-free for backwards compatibility — the home-aware readers
# here live under the ``_pipeline`` suffix.
# ---------------------------------------------------------------------------


_PIPELINE_RECEIPT_DIRNAME = "update_receipts"


@dataclass
class UpdateReceiptRecord:
    """Durable, typed record of one ``hermes update`` run.

    The fields here are the contract every later task in the plan depends
    on (orchestrator, classifier, dashboard, acknowledgement). The dataclass
    form (vs the legacy ``UpdateReceipt.data`` dict) is deliberate: callers
    can introspect fields, IDEs can autocomplete them, and JSON round-trip
    is ``UpdateReceiptRecord(**json.loads(...))``.
    """

    receipt_id: str
    outcome: Literal["success", "failed", "conflict", "aborted", "partial", "catastrophic"]
    error: Optional[str]
    rolled_back: bool
    acknowledged: bool = False
    strategy: str = "merge"
    applied_via: str = "user-click"
    safe_classification: dict = field(
        default_factory=lambda: {"auto_apply_safe": False, "reasons": []}
    )
    steps: list = field(default_factory=list)
    ahead_disregarded: int = 0
    pre_state: dict = field(default_factory=dict)
    post_state: dict = field(default_factory=dict)
    # ``backup_branch`` ref (e.g. ``backup-1700000000``) when the merge step
    # actually created a backup; empty when the backup was skipped because
    # local was at origin. The orchestrator (Task 6) writes this so the
    # dashboard / receipt reader can locate the rollback branch without
    # re-scanning the git tree.
    backup_ref: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    @classmethod
    def new(cls, outcome, error, rolled_back, **kwargs) -> UpdateReceiptRecord:
        """Construct a fresh record with a random 16-hex receipt id."""
        return cls(
            receipt_id=secrets.token_hex(8),
            outcome=outcome,
            error=error,
            rolled_back=rolled_back,
            **kwargs,
        )


def _pipeline_receipt_dir(home: Path) -> Path:
    """Return (and create) ``<home>/update_receipts/``."""
    d = home / _PIPELINE_RECEIPT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_pipeline_receipt(home: Path, record: UpdateReceiptRecord) -> Path:
    """Atomically write ``<home>/update_receipts/<id>.json`` + the ``latest.json`` pointer.

    Atomicity is non-negotiable: a power loss mid-write must never leave a
    torn receipt that looks complete to the next read. We write the full
    payload to ``.<id>.json.tmp`` first (same directory, so the rename is
    a single atomic inode swap on every supported FS), then ``rename`` it
    over the target, then overwrite the pointer. The pointer write is also
    a temp+rename so a concurrent reader never sees ``latest.json`` pointing
    at a receipt id whose file doesn't exist yet.
    """
    rdir = _pipeline_receipt_dir(home)
    target = rdir / f"{record.receipt_id}.json"
    tmp = rdir / f".{record.receipt_id}.json.tmp"
    payload = json.dumps(asdict(record), indent=2, default=str)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
    pointer_tmp = rdir / ".latest.json.tmp"
    pointer_tmp.write_text(
        json.dumps({"receipt_id": record.receipt_id}), encoding="utf-8"
    )
    pointer_tmp.replace(rdir / "latest.json")
    return target


def read_latest_pipeline_receipt(
    home: Path,
) -> Optional[UpdateReceiptRecord]:
    """Follow the ``latest.json`` pointer and reconstruct the record. ``None`` if absent/torn.

    Returns ``None`` (does not raise) on any of: missing pointer, missing
    target file, malformed JSON, schema mismatch (extra/missing fields),
    or a ``receipt_id`` that fails the path-traversal guard (e.g. one
    written into ``latest.json`` by a non-orchestrator process). Callers
    should treat ``None`` as "no acknowledged-up-to-date receipt exists" —
    the legacy ``read_latest_receipt()`` has the same never-raise contract for
    its callers.
    """
    rdir = _pipeline_receipt_dir(home)
    pointer = rdir / "latest.json"
    if not pointer.exists():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        receipt_id = data["receipt_id"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None
    # Receipt ids come from ``secrets.token_hex(8)`` (16 lowercase hex chars).
    # Anything else — a path separator, a stray extension, a sibling-
    # directory leak — must not be allowed to construct a filesystem path
    # under the receipts directory.
    if not isinstance(receipt_id, str) or not _RECEIPT_ID_RE.fullmatch(receipt_id):
        return None
    target = rdir / f"{receipt_id}.json"
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        return UpdateReceiptRecord(**payload)
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def acknowledge_pipeline_receipt(home: Path, receipt_id: str) -> bool:
    """Flip ``acknowledged=True`` on the on-disk receipt. Idempotent.

    Returns ``True`` iff the receipt existed and was (re)written. Idempotent:
    calling twice on the same id leaves the file byte-identical after the
    first call (acknowledged was already ``True``). Never raises.

    The ``receipt_id`` argument is validated against ``_RECEIPT_ID_RE``
    (16 lowercase hex chars, matching ``secrets.token_hex(8)`` output).
    A non-conforming id — whether a path traversal payload from a caller
    that shouldn't have access to the receipts directory, or a stray
    ``.json.tmp`` that would clobber the writer's atomic temp file — is
    rejected up front: a malicious caller can never cause a write to a
    path outside the receipts directory, and the same call returns
    ``False`` so the renderer surfaces the same "no such receipt" outcome
    it does for a typo.

    Note: this rewrites the whole receipt file rather than mutating it in
    place. The same atomic write contract as ``write_pipeline_receipt``
    applies — a power loss mid-acknowledge leaves either the previous
    acknowledged=False state or the new acknowledged=True state, never a
    torn file.
    """
    if not isinstance(receipt_id, str) or not _RECEIPT_ID_RE.fullmatch(receipt_id):
        return False
    rdir = _pipeline_receipt_dir(home)
    target = rdir / f"{receipt_id}.json"
    if not target.exists():
        return False
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        data["acknowledged"] = True
    except (json.JSONDecodeError, OSError):
        return False
    tmp = rdir / f".{receipt_id}.json.tmp"
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(target)
    return True
