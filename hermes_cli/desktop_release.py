"""FORK: the fork's desktop release channel -- the CONSUMER side of a published bundle.

The fork publishes a prebuilt Windows desktop bundle as a GitHub release per
commit (tag ``desktop-bootstrap-<sha12>``, one asset ``Hermes-<sha12>-win-x64.zip``).
This module is the other half of that contract: the update pipeline asks it for the
bundle that matches the commit it just landed on, downloads it, verifies it against
the digest the release published, and swaps it over the packaged app. The update
then finds the app already current and skips the multi-minute rebuild.

Upstream has no such path: its Windows mechanisms are the signed MSIX feed and the
source hand-off, and the source hand-off always rebuilds the app from the checkout.
The bundle is the fork's own artifact, so this lives in a fork-owned module instead
of as a delta on an upstream file (a delta there is a merge magnet).

Every failure here is a fallback, never a failed update: no release, no asset, a
download error or a refused swap all leave the source build to do its job.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

#: The tag convention the fork publishes with.
RELEASE_TAG_PREFIX = "desktop-bootstrap-"
#: The asset name the fork publishes with (the 12-char short sha).
_ASSET_SUFFIX = "-win-x64.zip"
_CHUNK = 1 << 20
_API = "https://api.github.com"


def _api_headers() -> dict[str, str]:
    """Headers for an api.github.com request. The token is optional: the fork is public."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hermes-agent-desktop-release",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        from hermes_cli.github_api import github_token

        token = github_token()
    except Exception:  # a missing credential must never fail the lookup
        token = None
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def release_repository(root: Path, git: str = "git") -> str:
    """``owner/repo`` of the remote that owns the releases -- the fork's own origin."""
    from hermes_cli.source_releases import source_repository
    from hermes_cli.source_check import _installed_git

    try:
        return source_repository(git_cmd=[_installed_git(git)], cwd=root)
    except Exception:
        return source_repository()


def fetch_releases(repository: str, *, timeout: float = 30.0) -> list[dict]:
    """The repository's releases, newest first (the fork's own release stream)."""
    url = f"{_API}/repos/{repository}/releases?per_page=100"
    request = urllib.request.Request(url, headers=_api_headers())
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [release for release in payload if isinstance(release, dict)] if isinstance(payload, list) else []


def find_release(repository: str, commit: str, *, timeout: float = 30.0) -> Optional[dict]:
    """The published release for ``commit``, matched by the short sha in its tag.

    The tag carries a PREFIX of the commit, and the published ones do not all use the
    same length, so the match is "the commit starts with the tag's suffix" -- the only
    relation that holds for every release the fork has cut.
    """
    try:
        releases = fetch_releases(repository, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    for release in releases:
        if release.get("draft"):
            continue
        tag = str(release.get("tag_name") or "")
        if not tag.startswith(RELEASE_TAG_PREFIX):
            continue
        suffix = tag[len(RELEASE_TAG_PREFIX):]
        if suffix and commit.startswith(suffix):
            return release
    return None


def select_asset(release: dict, commit: str) -> Optional[dict]:
    """The Windows x64 bundle of ``release``: the asset named for ``commit`` if it is there."""
    assets = [asset for asset in (release.get("assets") or []) if isinstance(asset, dict)]
    for asset in assets:
        name = str(asset.get("name") or "")
        if not name.endswith(_ASSET_SUFFIX):
            continue
        stem = name[len("Hermes-"):-len(_ASSET_SUFFIX)]
        if stem and commit.startswith(stem):
            return asset
    for asset in assets:
        if str(asset.get("name") or "").endswith(_ASSET_SUFFIX):
            return asset
    return None


def download_asset(asset: dict, destination: Path, *, timeout: float = 60.0,
                   emit: Optional[Callable[[str], None]] = None) -> str:
    """Download the asset to ``destination`` and return its sha256 hex.

    The digest the release published is the contract: a bundle that does not hash to
    it is never installed.
    """
    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise ValueError("the release asset has no download URL")
    # No Authorization header here: the download redirects to the CDN host, which
    # rejects a token aimed at the API. The fork's releases are public.
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-agent-desktop-release"})
    digest = hashlib.sha256()
    size = int(asset.get("size") or 0)
    copied = 0
    with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as handle:
        while True:
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            handle.write(chunk)
            copied += len(chunk)
            if emit and size and copied % (32 * _CHUNK) < _CHUNK:
                emit(f"  → downloaded {copied // (1 << 20)} MB of {size // (1 << 20)} MB")
    actual = digest.hexdigest()
    expected = str(asset.get("digest") or "").removeprefix("sha256:").strip().lower()
    if expected and expected != actual:
        raise ValueError(f"the downloaded bundle hashes to {actual}, not the published {expected}")
    return actual


def _file_digest(path: Path) -> str:
    """The sha256 of a file, as hex."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def extract_bundle(archive: Path, target: Path) -> None:
    """Extract the flat bundle zip into ``target`` (the unpacked app root)."""
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            parts = Path(member.filename).parts
            if member.filename.startswith("/") or ".." in parts:
                raise ValueError(f"the bundle contains an unsafe path: {member.filename}")
        bundle.extractall(target)


def head_commit(root: Path, git: str = "git") -> Optional[str]:
    """The commit the checkout is on, or None when git cannot answer."""
    from hermes_cli.source_check import _installed_git

    try:
        result = subprocess.run(
            [_installed_git(git), "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip() if result.returncode == 0 else ""
    return sha if len(sha) == 40 else None


def install_release(commit: str, *, root: Path, desktop_dir: Path, git: str = "git",
                    emit: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Install the published bundle for ``commit`` over the packaged app.

    Returns the tag that was installed, or None when the fork published no bundle for
    this commit -- then the caller builds the app from source as usual.
    """
    say = emit or (lambda message: print(message, flush=True))
    repository = release_repository(root, git=git)
    say(f"→ Looking for the published desktop release for {commit[:12]} in {repository}")
    try:
        release = find_release(repository, commit)
    except Exception as exc:
        say(f"  ⚠ Could not read the releases of {repository}: {exc}")
        return None
    if not release:
        say(f"  → No published desktop release for {commit[:12]}; the app will be built from source.")
        return None
    tag = str(release.get("tag_name") or RELEASE_TAG_PREFIX + commit[:12])
    asset = select_asset(release, commit)
    if not asset:
        say(f"  ⚠ The release {tag} has no Windows bundle; the app will be built from source.")
        return None

    from hermes_cli.main_desktop import _desktop_staging_dir, _swap_staged_desktop_app

    staging = _desktop_staging_dir(desktop_dir)
    archive = staging.parent / f"{staging.name}.zip"
    try:
        expected = str(asset.get("digest") or "").removeprefix("sha256:").strip().lower()
        # A bundle published from this machine is still in the release dir. Its digest is
        # the contract, so it IS the published artifact and the download can be skipped.
        local = desktop_dir / "release" / str(asset.get("name") or "")
        if expected and local.is_file() and _file_digest(local) == expected:
            say(f"  → Using the published bundle already on disk ({local.name})")
            source_zip = local
        else:
            say(f"  → Downloading {asset.get('name')} ({int(asset.get('size') or 0) // (1 << 20)} MB)")
            download_asset(asset, archive, emit=say)
            source_zip = archive
        target = staging / "win-unpacked"
        extract_bundle(source_zip, target)
    except Exception as exc:
        say(f"  ⚠ Could not fetch the published bundle: {exc}")
        return None
    finally:
        archive.unlink(missing_ok=True)

    exe = _swap_staged_desktop_app(desktop_dir, staging)
    if exe is None:
        say("  ⚠ Could not promote the downloaded bundle over the installed app; keeping the current build.")
        return None
    stamp = _installed_stamp(desktop_dir)
    if stamp and stamp.get("commit") != commit:
        say(f"  ⚠ The installed bundle reports commit {str(stamp.get('commit'))[:12]}, not {commit[:12]}.")
        return None
    say(f"  ✓ Installed the published desktop bundle {tag}.")
    return tag


def _installed_stamp(desktop_dir: Path) -> Optional[dict]:
    """The packaged app's install stamp, or None when it cannot be read."""
    stamp_path = desktop_dir / "release" / "win-unpacked" / "resources" / "install-stamp.json"
    try:
        payload = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def install_release_for_head(root: Path, *, git: str = "git",
                             emit: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Install the published bundle matching the checkout's HEAD, if there is one."""
    commit = head_commit(root, git=git)
    if not commit:
        return None
    desktop_dir = root / "apps" / "desktop"
    if not desktop_dir.is_dir():
        return None
    return install_release(commit, root=root, desktop_dir=desktop_dir, git=git, emit=emit)
