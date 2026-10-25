"""The desktop's update-settings route — FORK-OWNED.

The renderer's "Update behavior" toggle (``$updateAllowLocalAhead``) used to
live ONLY in the renderer's local storage, so the Python side that actually
gates an update (``hermes_cli.update_settings.load_update_settings``) never
saw it: the writer existed with ZERO callers and the toggle was dead UI.

These tests pin the bridge that closes that gap: the desktop POSTs the toggle,
the backend writes it to ``HERMES_HOME/update_settings.json`` — the same file
the orchestrator reads on every invoke — and a partial or mistyped payload
never lands there.

The second toggle the panel used to carry (``$updateSafeModeAuto``) was
removed: nothing ever read the flag it persisted, so it could only ever look
like a broken updater. These tests pin that it stays gone — the route ignores
a legacy payload that still carries the key, and never writes it back.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.update_settings import load_update_settings, settings_path


@pytest.fixture()
def client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """The HERMES_HOME the route writes: a temp dir, never the live install."""
    import hermes_constants

    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: root)
    return root


def test_route_writes_the_toggle_the_orchestrator_reads(client, home):
    response = client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": True},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    settings = load_update_settings(home)
    assert settings is not None
    assert settings.allow_local_ahead is True


def test_route_writes_no_legacy_toggle_key(client, home):
    """The removed auto-apply toggle must not reappear in the file."""
    client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": True},
    )

    payload = json.loads(settings_path(home).read_text(encoding="utf-8"))
    assert "safe_mode_auto" not in payload["toggles"]


def test_route_ignores_a_legacy_auto_apply_key(client, home):
    """A renderer build older than the removal still posts the key. It must
    not fail the write, and it must not persist."""
    response = client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": True, "safe_mode_auto": True},
    )

    assert response.status_code == 200

    settings = load_update_settings(home)
    assert settings is not None
    assert settings.allow_local_ahead is True
    assert not hasattr(settings, "safe_mode_auto")


def test_route_overwrites_the_previous_state(client, home):
    client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": True},
    )
    client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": False},
    )

    settings = load_update_settings(home)
    assert settings is not None
    assert settings.allow_local_ahead is False


def test_route_rejects_a_non_boolean_toggle(client, home):
    """A typo'd string must not arm the local-ahead bypass."""
    response = client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": "true"},
    )

    assert response.status_code == 422
    assert load_update_settings(home) is None


def test_route_rejects_a_partial_payload(client, home):
    """The toggle is written whole, so a body that leaves it out is refused
    rather than silently resetting it."""
    response = client.post(
        "/api/hermes/update/settings",
        json={},
    )

    assert response.status_code == 422
    assert load_update_settings(home) is None
