"""Tests for agent/tools/google_auth.py's service construction — transport
wiring only. The OAuth flow and live API calls stay untested per the project's
live-API precedent (see tests/test_google_tasks.py); what's asserted here is
the one property a hung googleapis.com connection would otherwise exploit:
every built service rides an httplib2 transport with a timeout."""

import pytest

from agent.tools import google_auth as ga


@pytest.fixture(autouse=True)
def _fresh_service_cache():
    ga._SERVICES.clear()
    yield
    ga._SERVICES.clear()


def test_build_service_uses_timeout_bearing_transport(monkeypatch):
    monkeypatch.setattr(ga, "get_credentials", lambda: object())
    captured = {}

    def fake_build(api, version, http=None):
        captured["http"] = http
        return f"service-{api}-{version}"

    monkeypatch.setattr(ga, "build", fake_build)

    service = ga.build_service("calendar", "v3")
    assert service == "service-calendar-v3"
    # An AuthorizedHttp wrapping an Http whose timeout is set — not build()'s
    # own default transport, which has none.
    assert captured["http"].http.timeout == ga.GOOGLE_HTTP_TIMEOUT_S


def test_build_service_caches_per_api_and_version(monkeypatch):
    monkeypatch.setattr(ga, "get_credentials", lambda: object())
    builds = []

    def fake_build(api, version, http=None):
        builds.append((api, version))
        return object()

    monkeypatch.setattr(ga, "build", fake_build)

    first = ga.build_service("gmail", "v1")
    assert ga.build_service("gmail", "v1") is first
    assert ga.build_service("tasks", "v1") is not first
    assert builds == [("gmail", "v1"), ("tasks", "v1")]


def test_reset_service_builds_a_new_client_on_a_new_transport(monkeypatch):
    """The cached client owns an httplib2 connection pool, and httplib2 keeps a
    dead socket in that pool after a broken pipe. So "reconnect" cannot mean
    reusing the client — it has to mean a new client on a new transport, or the
    retry fails exactly the way the first attempt did.

    Both halves are asserted: the old client is gone from the cache, AND the
    replacement was built with a transport of its own.
    """
    monkeypatch.setattr(ga, "get_credentials", lambda: object())
    transports = []

    def fake_build(api, version, http=None):
        transports.append(http)
        return object()

    monkeypatch.setattr(ga, "build", fake_build)

    first = ga.build_service("gmail", "v1")
    second = ga.reset_service("gmail", "v1")

    assert second is not first
    assert ga.build_service("gmail", "v1") is second
    assert len(transports) == 2
    assert transports[0] is not transports[1]


def test_reset_service_on_an_uncached_api_just_builds_one(monkeypatch):
    """Called before anything was cached — a plain build, not a KeyError."""
    monkeypatch.setattr(ga, "get_credentials", lambda: object())
    monkeypatch.setattr(ga, "build", lambda api, version, http=None: "fresh")

    assert ga.reset_service("gmail", "v1") == "fresh"
