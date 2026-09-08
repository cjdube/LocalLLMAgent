"""Tests for chat/routes_settings.py — the only route in this repo that writes
configuration.

The resolver and the all-or-nothing save are covered by tests/test_config.py,
and the section validators by tests/test_prefs.py. What is tested here is what
the route adds: the auth gate, that no secret value appears anywhere in a
response, that a rejected save reports every bad field at once and writes
nothing, and that the restart banner is computed from what actually changed.

The store fixture points WREN_SETTINGS_FILE and WREN_ENV_FILE at tmp_path, so
nothing here reads the developer's config/.env or writes the real
config/settings.json.
"""

import json
import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from agent import config, prefs, schema
from chat import server as srv


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An empty settings document and an empty .env, both under tmp_path, and a
    process environment with no schema key set in it.

    Cleared explicitly for the reason tests/test_config.py gives: half the repo
    has called load_dotenv on the real config/.env by the time a fixture runs,
    so layer 1 would answer with the developer's own values and every `source`
    assertion below would read "env".
    """
    settings = tmp_path / "settings.json"
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("WREN_SETTINGS_FILE", str(settings))
    monkeypatch.setenv("WREN_ENV_FILE", str(env))
    for key in schema.keys():
        if key != "WREN_LOGS_DIR":          # the suite's own log redirect rides on it
            monkeypatch.delenv(key, raising=False)
    config.reload()
    yield settings
    config.reload()


@pytest.fixture
def client(store):
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["sid"] = "test-sid"
        yield c


def rows_by_key(payload):
    return {row["key"]: row
            for group in payload["groups"] for row in group["rows"]}


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_reading_without_a_session_is_401(store):
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        resp = c.get("/api/settings")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "not authenticated"}


def test_saving_without_a_session_is_401_and_writes_nothing(store):
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        resp = c.post("/api/settings", json={"values": {"OLLAMA_MODEL": "gemma5"}})
    assert resp.status_code == 401
    assert not store.exists()


# --------------------------------------------------------------------------- #
# No secret value leaves the process
# --------------------------------------------------------------------------- #

def test_a_secret_row_has_no_value_key_at_all(client, monkeypatch):
    monkeypatch.setenv("NTFY_TOKEN", "tk_live_do_not_leak")
    payload = client.get("/api/settings").get_json()
    row = rows_by_key(payload)["NTFY_TOKEN"]
    # Absent, not redacted and not an empty string: a key that is present is a
    # key a future edit to the page can render.
    assert "value" not in row
    assert row["secret"] is True
    assert row["is_set"] is True


def test_no_secret_value_appears_anywhere_in_the_response(client, monkeypatch):
    # The whole body, not just the row — a secret leaking through `preferences`
    # or a warning string would pass a per-row assertion.
    for key in schema.secret_keys():
        monkeypatch.setenv(key, f"leak-{key}")
    body = client.get("/api/settings").get_data(as_text=True)
    for key in schema.secret_keys():
        assert f"leak-{key}" not in body


def test_an_unset_secret_says_so_without_a_value(client, monkeypatch):
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    row = rows_by_key(client.get("/api/settings").get_json())["NTFY_TOKEN"]
    assert row["is_set"] is False
    assert "value" not in row


def test_saving_a_secret_is_refused(client, store):
    resp = client.post("/api/settings", json={"values": {"NTFY_TOKEN": "tk_new"}})
    assert resp.status_code == 400
    assert "secret" in resp.get_json()["field_errors"]["NTFY_TOKEN"]
    assert not store.exists(), "a rejected save must write nothing"


# --------------------------------------------------------------------------- #
# What the page is told
# --------------------------------------------------------------------------- #

def test_every_row_carries_what_the_page_renders(client):
    row = rows_by_key(client.get("/api/settings").get_json())["OLLAMA_MODEL"]
    for field in ("key", "group", "label", "help", "type", "applies", "editable",
                  "reason", "choices", "minimum", "maximum", "default", "secret",
                  "source", "is_set", "value"):
        assert field in row, f"the page renders {field} and the API does not send it"


def test_source_says_which_layer_answered(client, store, monkeypatch):
    assert rows_by_key(client.get("/api/settings").get_json())["OLLAMA_MODEL"]["source"] \
        == "default"
    config.apply({"OLLAMA_MODEL": "gemma5"}, {})
    assert rows_by_key(client.get("/api/settings").get_json())["OLLAMA_MODEL"]["source"] \
        == "file"
    # The row the page has to lock: the environment wins, so a save here would
    # succeed and change nothing.
    monkeypatch.setenv("OLLAMA_MODEL", "from-env")
    assert rows_by_key(client.get("/api/settings").get_json())["OLLAMA_MODEL"]["source"] \
        == "env"


def test_a_locked_row_is_sent_with_its_reason(client):
    row = rows_by_key(client.get("/api/settings").get_json())["WREN_CHAT_PORT"]
    assert row["editable"] is False
    assert row["reason"], "a locked field with no reason reads as a bug"


def test_the_groups_come_back_in_table_order(client):
    payload = client.get("/api/settings").get_json()
    assert [g["name"] for g in payload["groups"]] == list(schema.GROUPS)


def test_the_preference_sections_come_back_whole(client):
    payload = client.get("/api/settings").get_json()
    assert payload["sections"] == list(schema.PREFERENCE_SECTIONS)
    assert payload["preferences"]["persona"] == config.preferences()["persona"]


def test_the_startup_warning_reaches_the_page(client, store, tmp_path, monkeypatch):
    env = tmp_path / "overlap.env"
    env.write_text("OLLAMA_MODEL=from-env\n", encoding="utf-8")
    monkeypatch.setenv("WREN_ENV_FILE", str(env))
    config.apply({"WREN_CHAT_SUMMARY_CHARS": "1200"}, {})
    warnings = client.get("/api/settings").get_json()["warnings"]
    assert warnings and "OLLAMA_MODEL" in warnings[0]
    assert "migrate_settings" in warnings[0], "the banner has to say what to run"


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #

def test_a_good_save_lands_and_reports_what_changed(client, store):
    resp = client.post("/api/settings", json={"values": {"OLLAMA_MODEL": "gemma5"}})
    assert resp.status_code == 200
    assert resp.get_json()["changed"] == ["OLLAMA_MODEL"]
    assert json.loads(store.read_text())["values"]["OLLAMA_MODEL"] == "gemma5"


def test_every_bad_field_is_reported_at_once(client, store):
    # One error per round trip would make a person save six times to find six
    # mistakes. config.apply raises on the first, so the route validates before
    # it calls apply.
    resp = client.post("/api/settings", json={"values": {
        "OLLAMA_NUM_CTX": "banana",
        "WREN_LLM_BACKEND": "llamafile",
        "NOT_A_SETTING": "x",
    }})
    assert resp.status_code == 400
    errors = resp.get_json()["field_errors"]
    assert set(errors) == {"OLLAMA_NUM_CTX", "WREN_LLM_BACKEND", "NOT_A_SETTING"}
    assert "must be a number" in errors["OLLAMA_NUM_CTX"]
    assert not store.exists()


def test_a_rejected_save_leaves_the_file_byte_identical(client, store):
    client.post("/api/settings", json={"values": {"OLLAMA_MODEL": "gemma5"}})
    before = store.read_bytes()
    resp = client.post("/api/settings", json={"values": {
        "OLLAMA_MODEL": "gemma6", "OLLAMA_NUM_CTX": "1"}})
    assert resp.status_code == 400
    # All-or-nothing: the good value in the same batch must not land either.
    assert store.read_bytes() == before
    assert config.getenv("OLLAMA_MODEL") == "gemma5"


def test_a_no_op_re_save_raises_no_banner(client, store):
    client.post("/api/settings", json={"values": {"WREN_CHAT_SUMMARY_CHARS": "1200"}})
    resp = client.post("/api/settings", json={"values": {"WREN_CHAT_SUMMARY_CHARS": "1200"}})
    body = resp.get_json()
    assert body["changed"] == []
    # A banner nobody needs is a banner that trains you to ignore the ones you do.
    assert body["restart_required"] == []


def test_the_restart_list_is_computed_from_what_changed(client, store):
    body = client.post("/api/settings", json={"values": {
        "WREN_CHAT_SUMMARY_CHARS": "1200",       # applies=restart
        "OLLAMA_KEEP_ALIVE": "30m",              # applies=live
    }}).get_json()
    assert schema.by_key("WREN_CHAT_SUMMARY_CHARS").applies == "restart"
    assert schema.by_key("OLLAMA_KEEP_ALIVE").applies == "live"
    assert body["restart_required"] == ["WREN_CHAT_SUMMARY_CHARS"]
    assert "OLLAMA_KEEP_ALIVE" in body["changed"]
    assert body["restart_command"] == schema.RESTART_COMMAND


def test_saving_a_section_reloads_it_into_this_process(client, store):
    # What makes a preference edit land without a restart. prefs.PREFS is bound
    # at import; the route calls prefs.reload() so the running server sees it.
    teams = {"teams": [{"league": "mlb", "id": "2", "name": "Red Sox"}]}
    assert client.post("/api/settings", json={"preferences": {"sports": teams}}) \
        .status_code == 200
    assert [t["name"] for t in prefs.followed_teams()] == ["Red Sox"]


def test_a_bad_section_is_refused_with_the_validators_words(client, store):
    resp = client.post("/api/settings", json={
        "preferences": {"persona": {"user_name": "A"}}})
    assert resp.status_code == 400
    message = resp.get_json()["field_errors"]["persona"]
    # The same sentence tests/test_prefs.py asserts, so the page and the suite
    # can never disagree about what a good section is.
    assert "persona.positioning is missing or empty" in message
    assert not store.exists()


def test_an_unknown_section_is_refused(client, store):
    resp = client.post("/api/settings", json={"preferences": {"astrology": {"sign": "leo"}}})
    assert resp.status_code == 400
    assert "not a known preference section" in resp.get_json()["field_errors"]["astrology"]


@pytest.mark.parametrize("payload", [
    "not an object",
    ["values"],
    {"values": "not an object"},
    {"preferences": ["sports"]},
])
def test_a_malformed_body_is_a_400_not_a_500(client, store, payload):
    assert client.post("/api/settings", json=payload).status_code == 400
    assert not store.exists()


def test_an_empty_save_is_accepted_and_changes_nothing(client, store):
    resp = client.post("/api/settings", json={"values": {}, "preferences": {}})
    assert resp.status_code == 200
    assert resp.get_json()["changed"] == []
    assert not store.exists()


# --------------------------------------------------------------------------- #
# The log
# --------------------------------------------------------------------------- #

def test_the_route_logs_key_names_and_never_values(client, store, caplog):
    with caplog.at_level("INFO", logger="wren"):
        client.post("/api/settings", json={"values": {"OLLAMA_MODEL": "gemma5-secretish"}})
    assert "OLLAMA_MODEL" in caplog.text
    assert "gemma5-secretish" not in caplog.text


def test_a_rejected_save_logs_the_field_names_not_the_values(client, store, caplog):
    with caplog.at_level("INFO", logger="wren"):
        client.post("/api/settings", json={"values": {"OLLAMA_NUM_CTX": "banana-42"}})
    assert "OLLAMA_NUM_CTX" in caplog.text
    assert "banana-42" not in caplog.text
