"""The fork's desktop release channel: the consumer side of a published bundle.

The fork publishes a prebuilt desktop bundle per commit as a GitHub release
(``desktop-bootstrap-<sha>``). The update pipeline installs that bundle instead
of rebuilding the app, so these tests pin the three things that make it safe:
the release is found by the commit it was cut from, the bytes are the published
ones (the digest is the contract), and the archive cannot write outside the tree
it is extracted into.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from hermes_cli import desktop_release as dr

COMMIT = "a9e1e7be494b0de9e8b488394351cc9e4effc29b"


class _FakeResponse(io.BytesIO):
    """The shape ``download_asset`` reads: a context manager over bytes."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _release(tag: str, *, draft: bool = False, assets: list[dict] | None = None) -> dict:
    return {"tag_name": tag, "draft": draft, "assets": assets or []}


def test_find_release_matches_the_short_sha_the_tag_carries(monkeypatch):
    """The published tags carry different lengths of the sha, so the match is a prefix."""
    monkeypatch.setattr(dr, "fetch_releases", lambda repository, **kw: [
        _release("desktop-bootstrap-0db1c6f190d"),
        _release("desktop-bootstrap-a9e1e7be494", draft=True),
        _release("v1.2.3"),
        _release("desktop-bootstrap-a9e1e7be494"),
    ])
    found = dr.find_release("aimastertoast/hermes-agent", COMMIT)
    assert found is not None
    assert found["tag_name"] == "desktop-bootstrap-a9e1e7be494"


def test_find_release_ignores_a_draft_of_the_same_tag(monkeypatch):
    """A draft's assets are not downloadable, so it is not the published bundle."""
    monkeypatch.setattr(dr, "fetch_releases", lambda repository, **kw: [
        _release("desktop-bootstrap-a9e1e7be494", draft=True),
    ])
    assert dr.find_release("aimastertoast/hermes-agent", COMMIT) is None


def test_find_release_without_a_match_is_none(monkeypatch):
    monkeypatch.setattr(dr, "fetch_releases", lambda repository, **kw: [
        _release("desktop-bootstrap-0db1c6f190d"),
    ])
    assert dr.find_release("aimastertoast/hermes-agent", COMMIT) is None


def test_select_asset_prefers_the_bundle_named_for_the_commit():
    release = _release("desktop-bootstrap-a9e1e7be494", assets=[
        {"name": "Hermes-0db1c6f190d-win-x64.zip"},
        {"name": "Hermes-a9e1e7be494-win-x64.zip"},
    ])
    asset = dr.select_asset(release, COMMIT)
    assert asset is not None
    assert asset["name"] == "Hermes-a9e1e7be494-win-x64.zip"


def test_select_asset_without_a_windows_bundle_is_none():
    release = _release("desktop-bootstrap-a9e1e7be494", assets=[{"name": "notes.txt"}])
    assert dr.select_asset(release, COMMIT) is None


def test_download_asset_rejects_bytes_that_are_not_the_published_ones(monkeypatch, tmp_path):
    """The published digest is the contract: other bytes are never installed."""
    payload = b"not the bundle"

    class _Opener:
        def __call__(self, request, timeout=None):
            return _FakeResponse(payload)

    monkeypatch.setattr(dr.urllib.request, "urlopen", _Opener())
    asset = {"browser_download_url": "https://example.invalid/bundle.zip",
             "digest": "sha256:" + "0" * 64}
    with pytest.raises(ValueError, match="hashes to"):
        dr.download_asset(asset, tmp_path / "bundle.zip")


def test_download_asset_accepts_the_published_bytes(monkeypatch, tmp_path):
    import hashlib

    payload = b"the bundle"
    digest = hashlib.sha256(payload).hexdigest()

    class _Opener:
        def __call__(self, request, timeout=None):
            return _FakeResponse(payload)

    monkeypatch.setattr(dr.urllib.request, "urlopen", _Opener())
    asset = {"browser_download_url": "https://example.invalid/bundle.zip",
             "digest": f"sha256:{digest}"}
    destination = tmp_path / "bundle.zip"
    assert dr.download_asset(asset, destination) == digest
    assert destination.read_bytes() == payload


def test_extract_bundle_writes_the_flat_tree(tmp_path):
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Hermes.exe", "exe")
        bundle.writestr("resources/install-stamp.json", "{}")
    target = tmp_path / "win-unpacked"
    dr.extract_bundle(archive, target)
    assert (target / "Hermes.exe").read_text() == "exe"
    assert (target / "resources" / "install-stamp.json").is_file()


def test_extract_bundle_refuses_to_write_outside_the_target(tmp_path):
    """A path that climbs out of the target is refused before anything is written."""
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escaped.txt", "no")
    with pytest.raises(ValueError, match="unsafe path"):
        dr.extract_bundle(archive, tmp_path / "win-unpacked")
    assert not (tmp_path / "escaped.txt").exists()
