"""Tests for the daily Gmail watch renewal.

Gmail drops a watch after 7 days without saying so, and a stopped watcher looks
exactly like a quiet inbox. These tests pin the three things that make that
failure audible: the renewal happens, the expiry is stored, and a near-expiry
still pushes an alert even though the call succeeded.

Every Gmail call and every push is stubbed; nothing reaches the network.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent.tools import mail_state
from tasks import mail_watch_renew


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_state, "_STORE_PATH", tmp_path / "mail_state.json")


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("MAIL_PUBSUB_PROJECT", "wren-123")
    monkeypatch.setenv("MAIL_PUBSUB_TOPIC", "wren-mail")


@pytest.fixture
def alerts(monkeypatch):
    """Capture notify_failure() instead of pushing to the phone."""
    sent = []
    monkeypatch.setattr(mail_watch_renew, "notify_failure",
                        lambda task, detail, logger=None: sent.append(str(detail)))
    return sent


def _in_days(days: float) -> str:
    """An expiration in the shape Gmail returns it: epoch MILLISECONDS."""
    moment = datetime.now(timezone.utc) + timedelta(days=days)
    return str(int(moment.timestamp() * 1000))


@pytest.fixture
def gmail(monkeypatch):
    state = {
        "label": {"label_id": "Label_7", "name": "Wren/Watch"},
        "watch": {"history_id": "1234", "expiration": _in_days(7)},
        "watch_calls": [],
    }

    def fake_register_watch(topic_name, label_id=None):
        state["watch_calls"].append((topic_name, label_id))
        return state["watch"]

    monkeypatch.setattr(mail_watch_renew.gmail_read, "label_id", lambda *a, **k: state["label"])
    monkeypatch.setattr(mail_watch_renew.gmail_read, "register_watch", fake_register_watch)
    return state


def test_topic_name_is_assembled_from_config(monkeypatch):
    monkeypatch.setenv("MAIL_PUBSUB_PROJECT", "wren-123")
    monkeypatch.setenv("MAIL_PUBSUB_TOPIC", "wren-mail")

    assert mail_watch_renew.topic_name() == "projects/wren-123/topics/wren-mail"


def test_renewal_registers_the_watch_for_the_label(gmail, alerts):
    assert mail_watch_renew.main() == 0
    assert gmail["watch_calls"] == [("projects/wren-123/topics/wren-mail", "Label_7")]


def test_renewal_stores_the_expiry_and_the_history_id(gmail, alerts):
    mail_watch_renew.main()

    state = mail_state.load_state()
    assert state["watch_expiration"] == gmail["watch"]["expiration"]
    assert state["history_id"] == "1234"
    assert 160 < mail_state.watch_expires_in_hours() < 169


def test_a_healthy_renewal_pushes_nothing(gmail, alerts):
    mail_watch_renew.main()

    assert alerts == []


def test_a_near_expiry_alerts_even_though_the_call_succeeded(gmail, alerts):
    """The one case the failure path would never catch: users.watch() returns
    fine, but the expiry is not moving out — so the daily runs are not landing
    and mail is about to stop arriving silently."""
    gmail["watch"] = {"history_id": "1234", "expiration": _in_days(1)}

    assert mail_watch_renew.main() == 0
    assert len(alerts) == 1
    assert "silently" in alerts[0]


def test_a_missing_project_fails_loudly(gmail, alerts, monkeypatch):
    monkeypatch.delenv("MAIL_PUBSUB_PROJECT", raising=False)

    assert mail_watch_renew.main() == 1
    assert alerts


def test_a_missing_label_fails_loudly(gmail, alerts):
    gmail["label"] = {"error": 'no Gmail label named "Wren/Watch"'}

    assert mail_watch_renew.main() == 1
    assert alerts


def test_a_watch_failure_names_the_publisher_grant(gmail, alerts):
    """A 403 here is nearly always gmail-api-push@system.gserviceaccount.com
    losing Pub/Sub Publisher on the topic, and the API error does not say so."""
    gmail["watch"] = {"error": "403 the topic is not accessible"}

    assert mail_watch_renew.main() == 1
    assert "gserviceaccount" in alerts[0]


def test_the_watch_is_not_recorded_when_the_call_failed(gmail, alerts):
    gmail["watch"] = {"error": "403 the topic is not accessible"}

    mail_watch_renew.main()

    assert mail_state.load_state()["watch_expiration"] is None
