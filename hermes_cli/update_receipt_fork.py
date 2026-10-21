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
from datetime import datetime
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


# ---------------------------------------------------------------------------
# The receipt the DESKTOP reads
#
# TWO stores, TWO writers — and the Desktop's Update button uses the one
# this module used to ignore:
#
#   * the PIPELINE store (``<home>/update_receipts/``) — written by the
#     update orchestrator (``--use-orchestrator``). Typed record, carries its
#     own ``acknowledged`` flag.
#   * upstream's LEGACY store (``<root home>/logs/update_receipts/``) — written
#     by the DEFAULT ``hermes update`` path, which is what the Desktop's
#     Update button actually runs. Its ``latest.json`` pointer is overwritten
#     by unrelated writers (plugin checks, fleet syncs), so the last UPDATE
#     receipt has to be found by scanning the ``update_*.json`` archive.
#
# Reading only the pipeline store (what the fork's route used to do) is the
# same as reading nothing: the default path never writes there, so the
# Desktop's receipt pane could never show a receipt at all.
# ---------------------------------------------------------------------------

# ``<root home>/logs/update_receipts`` — upstream's store layout.
_LEGACY_RECEIPT_SUBDIR = ("logs", "update_receipts")

# ``update_<YYYYMMDD>_<HHMMSS>_<pid>_<update_id>.json`` — the run's start
# stamp is in the file name, so the archive can be ordered without parsing
# every payload. ``update_id`` is 32 hex chars (upstream's uuid4().hex).
_LEGACY_UPDATE_FILE_RE = re.compile(
    r"^update_(\d{8}_\d{6})_\d+_([0-9a-f]{32})\.json$"
)
_LEGACY_UPDATE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# ``pm`` receipts (plugin checks, fleet syncs) share the store and can end up
# in ``latest.json``. They are NOT update runs: their ``outcome`` is about
# PLUGINS ("updates-available"), and the Desktop's pane reads any non-success
# outcome as "the last update failed" — so serving one would put a plugin
# check in front of the user as an update failure.
_PM_RECEIPT_KINDS = frozenset({"plugin-check", "sync"})


def _legacy_root_dir(home: Path) -> Path:
    """The ROOT store of ``home`` — the store upstream's writers publish to.

    Upstream's own ``_receipt_dir()`` is the seam its writers and its tests
    point at a fixture directory. Recomputing the path here bypassed that
    seam, and the route then read a store nobody had written to (upstream's
    own endpoint test went RED: 404 where a receipt existed). Use upstream's
    seam when the ambient home IS this home — the only case where the two
    can differ is a caller reading a home that is not the current one, and
    there the root is derived from that home instead.
    """
    try:
        from hermes_cli import update_receipt as upstream_receipt
        from hermes_constants import get_hermes_home

        if Path(get_hermes_home()).resolve() == Path(home).resolve():
            return Path(upstream_receipt._receipt_dir())
    except Exception:
        pass

    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root(home=home).joinpath(*_LEGACY_RECEIPT_SUBDIR)


def _legacy_receipt_dirs(home: Path) -> list[Path]:
    """The root store, then the profile's own when they differ.

    Mirrors upstream's ``_receipt_dirs()``: an update mutates the checkout
    every profile shares, so the receipts the Desktop must read live in the
    ROOT home's store. The root is resolved FROM ``home`` (not from the
    process env) so a profile-scoped request reads that profile's root.
    """
    root = _legacy_root_dir(home)
    profile = home.joinpath(*_LEGACY_RECEIPT_SUBDIR)
    try:
        same = root.resolve() == profile.resolve()
    except OSError:
        same = root == profile
    return [root] if same else [root, profile]


def _legacy_update_receipts(home: Path) -> list[tuple[str, Path, dict[str, Any]]]:
    """Every legacy UPDATE receipt as ``(start stamp, path, payload)``.

    Plugin-check / fleet-sync receipts (``pm_*.json``) are NOT update runs and
    are skipped — they share the store and would otherwise be mistaken for an
    update's outcome.
    """
    found: list[tuple[str, Path, dict[str, Any]]] = []
    for directory in _legacy_receipt_dirs(home):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("update_*.json")):
            match = _LEGACY_UPDATE_FILE_RE.match(path.name)
            if not match:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                found.append((match.group(1), path, payload))
    return found


def _is_update_receipt(payload: dict[str, Any]) -> bool:
    """Whether a receipt payload is an UPDATE run (not a plugin check / sync).

    Upstream's store is shared: ``pm`` writes plugin-check and fleet-sync
    receipts into it, and its ``latest.json`` pointer is overwritten by
    whichever writer ran last — in practice a plugin check. The Desktop's
    pane reads any non-success outcome as "the last update failed", so a
    plugin check served here would be shown to the user as an update
    failure. An update receipt always names its run (``update_id``) or
    carries the update snapshot (``post_update``); a plugin check does
    neither.
    """
    if str(payload.get("kind") or "") in _PM_RECEIPT_KINDS:
        return False
    if "plugin_checks" in payload:
        return False
    return bool(
        payload.get("update_id")
        or payload.get("action_id")
        or "post_update" in payload
        or "pre_update" in payload
    )


def _legacy_pointer_receipts(home: Path) -> list[tuple[Optional[float], dict[str, Any]]]:
    """The ``latest.json`` pointer of each legacy store, when it is an UPDATE.

    The pointer is upstream's own "latest receipt" contract — the route has
    always served it, and upstream's endpoint test pins it. It is only a
    candidate when it is an update receipt: the pointer is overwritten by
    unrelated writers (plugin checks, fleet syncs), and those must not be
    served as the last update's outcome.
    """
    found: list[tuple[Optional[float], dict[str, Any]]] = []
    for directory in _legacy_receipt_dirs(home):
        pointer = directory / "latest.json"
        if not pointer.is_file():
            continue
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not _is_update_receipt(payload):
            continue
        found.append((_completed_at(payload), _legacy_receipt_for_desktop(payload, pointer)))
    return found


def _completed_at(payload: dict[str, Any]) -> Optional[float]:
    """Epoch seconds of a COMPLETED run, or ``None`` when it never finished.

    A receipt without ``finished_at`` belongs to a run that is still going (or
    was killed mid-flight): it has no outcome to report, so the Desktop must
    not present it as the last update's result. The overlay's own live apply
    state covers the in-flight case.
    """
    raw = payload.get("finished_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


def _legacy_receipt_for_desktop(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    """The legacy receipt plus the three fields the Desktop's pane needs.

    Upstream's legacy receipt already carries ``outcome`` and ``steps`` in the
    shape the Desktop reads. It lacks the receipt id (it calls it
    ``update_id``), the acknowledged flag, and an error string — so those are
    added here. The original fields are kept untouched: the dashboard reads
    the same receipt and expects upstream's own keys.
    """
    out = dict(payload)
    match = _LEGACY_UPDATE_FILE_RE.match(path.name)
    out.setdefault(
        "receipt_id",
        match.group(2) if match else str(payload.get("update_id") or payload.get("action_id") or ""),
    )
    out.setdefault("acknowledged", False)
    if not out.get("error"):
        stop = payload.get("stop_class") or payload.get("stop_reason")
        if stop:
            out["error"] = str(stop)
    return out


def read_latest_update_receipt_dict(home: Path) -> Optional[dict[str, Any]]:
    """The latest COMPLETED update receipt, in the shape the Desktop expects.

    Newest across BOTH stores by finish time. ``None`` when no update run has
    completed yet — the Desktop then shows its generic idle view, exactly as
    it does on a fresh install.
    """
    candidates: list[tuple[float, dict[str, Any]]] = []

    record = read_latest_pipeline_receipt(home)
    if record is not None and record.finished_at is not None:
        candidates.append((float(record.finished_at), asdict(record)))

    for _stamp, path, payload in _legacy_update_receipts(home):
        finished = _completed_at(payload)
        if finished is None:
            continue
        candidates.append((finished, _legacy_receipt_for_desktop(payload, path)))

    for finished, payload in _legacy_pointer_receipts(home):
        if finished is None:
            continue
        candidates.append((finished, payload))

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _acknowledge_legacy_receipt(home: Path, receipt_id: str) -> bool:
    """Flip ``acknowledged`` on the legacy receipt file with this id.

    Same contract as :func:`acknowledge_pipeline_receipt`: idempotent, atomic
    (temp file + rename), never raises, and the id is validated against a
    strict hex pattern so a caller can never construct a path outside the
    receipts directory.
    """
    if not _LEGACY_UPDATE_ID_RE.fullmatch(receipt_id):
        return False
    for _stamp, path, payload in _legacy_update_receipts(home):
        if payload.get("update_id") != receipt_id:
            continue
        payload["acknowledged"] = True
        tmp = path.with_name(f".{path.name}.acktmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return False
        return True
    return False


def acknowledge_update_receipt(home: Path, receipt_id: str) -> bool:
    """Acknowledge a receipt by id, in whichever store holds it.

    The ack lands ON THE RECEIPT (never on a global flag): acknowledging one
    run must not hide a different one.
    """
    if not isinstance(receipt_id, str) or not receipt_id:
        return False
    if acknowledge_pipeline_receipt(home, receipt_id):
        return True
    return _acknowledge_legacy_receipt(home, receipt_id)
