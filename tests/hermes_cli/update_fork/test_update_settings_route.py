"""The desktop's update-settings route — FORK-OWNED.

The renderer's "Update behavior" toggles (``$updateAllowLocalAhead`` and
``$updateSafeModeAuto``) used to live ONLY in the renderer's local storage, so
the Python side that actually gates an update
(``hermes_cli.update_settings.load_update_settings``) never saw them: the
writer existed with ZERO callers and both toggles were dead UI.

These tests pin the bridge that closes that gap: the desktop POSTs the
toggles, the backend writes them to ``HERMES_HOME/update_settings.json`` — the
same file the orchestrator reads on every invoke — and a partial or mistyped
payload never lands there.
"""

from __future__ import annotations

import pytest

from hermes_cli.update_settings import load_update_settings


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


def test_route_writes_the_toggles_the_orchestrator_reads(client, home):
    response = client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": True, "safe_mode_auto": True},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    settings = load_update_settings(home)
    assert settings is not None
    assert settings.allow_local_ahead is True
    assert settings.safe_mode_auto is True


def test_route_overwrites_the_previous_state(client, home):
    client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": True, "safe_mode_auto": True},
    )
    client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": False, "safe_mode_auto": False},
    )

    settings = load_update_settings(home)
    assert settings is not None
    assert settings.allow_local_ahead is False
    assert settings.safe_mode_auto is False


def test_route_rejects_a_non_boolean_toggle(client, home):
    """A typo'd string must not arm the local-ahead bypass."""
    response = client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": "true", "safe_mode_auto": False},
    )

    assert response.status_code == 422
    assert load_update_settings(home) is None


def test_route_rejects_a_partial_payload(client, home):
    """Both toggles are written together, so a partial body is refused rather
    than silently resetting the toggle it left out."""
    response = client.post(
        "/api/hermes/update/settings",
        json={"allow_local_ahead": True},
    )

    assert response.status_code == 422
    assert load_update_settings(home) is None
