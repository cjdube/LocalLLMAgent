"""The four-layer resolver, and the all-or-nothing save.

Every test here points WREN_SETTINGS_FILE and WREN_ENV_FILE at tmp_path and
calls config.reload(), so nothing reads the developer's real config/.env or
writes the real config/settings.json.
"""

import json
import logging
import os
import stat

import pytest

from agent import config, schema


# The suite's own logs redirect rides on WREN_LOGS_DIR, which is also a schema
# key. Clearing it here would send this module's task-log guards back at the
# real logs/.
_KEEP_IN_ENV = frozenset({"WREN_LOGS_DIR"})


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A settings document and an empty .env, both under tmp_path, and a
    process environment with no schema key set in it.

    The environment has to be cleared explicitly. Half the repo calls
    load_dotenv on the real config/.env at import, long before any fixture
    runs, so the developer's own OLLAMA_MODEL is already in os.environ and
    wins layer 1 — which is the whole point of layer 1, and exactly why these
    tests, which are about the three layers underneath it, start from empty.
    The one test that cares about layer 1 sets its own value.
    """
    settings = tmp_path / "settings.json"
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setenv("WREN_SETTINGS_FILE", str(settings))
    monkeypatch.setenv("WREN_ENV_FILE", str(env))
    for key in schema.keys():
        if key not in _KEEP_IN_ENV:
            monkeypatch.delenv(key, raising=False)
    config.reload()
    yield settings
    config.reload()


def write(store, values=None, preferences=None):
    store.write_text(json.dumps(
        {"values": values or {}, "preferences": preferences or {}}), encoding="utf-8")
    config.reload()


# --------------------------------------------------------------------------- #
# The four layers
# --------------------------------------------------------------------------- #

def test_the_environment_outranks_the_settings_file(store, monkeypatch):
    # 251 monkeypatch.setenv calls in this suite, and every WREN_X=... one-off
    # run, depend on this order. It is why the .env migration is mandatory.
    write(store, {"OLLAMA_MODEL": "from-file"})
    monkeypatch.setenv("OLLAMA_MODEL", "from-env")
    assert config.getenv("OLLAMA_MODEL") == "from-env"


def test_the_settings_file_outranks_the_schema_default(store):
    write(store, {"OLLAMA_MODEL": "from-file"})
    assert config.getenv("OLLAMA_MODEL") == "from-file"


def test_the_schema_default_outranks_the_callers_default(store):
    assert schema.default_for("OLLAMA_MODEL") == "gemma4"
    assert config.getenv("OLLAMA_MODEL", "from-caller") == "gemma4"


def test_the_callers_default_answers_when_the_row_has_none(store):
    # WREN_SKILLS_DIR's real default is a path computed from this machine, so
    # the table deliberately carries no default for it.
    assert schema.default_for("WREN_SKILLS_DIR") == ""
    assert config.getenv("WREN_SKILLS_DIR", "/tmp/skills") == "/tmp/skills"


def test_an_unknown_key_falls_all_the_way_through(store):
    assert config.getenv("NOT_A_WREN_SETTING") is None
    assert config.getenv("NOT_A_WREN_SETTING", "fallback") == "fallback"


def test_an_empty_string_does_not_count_as_set(store, monkeypatch):
    # Exporting X="" is how a shell unsets nothing in particular. Treating it
    # as a value would pin the empty string past every remaining layer.
    monkeypatch.setenv("OLLAMA_MODEL", "")
    write(store, {"OLLAMA_MODEL": ""})
    assert config.getenv("OLLAMA_MODEL") == "gemma4"


def test_source_of_names_the_layer_that_answered(store, monkeypatch):
    assert config.source_of("OLLAMA_MODEL") == "default"
    write(store, {"OLLAMA_MODEL": "from-file"})
    assert config.source_of("OLLAMA_MODEL") == "file"
    monkeypatch.setenv("OLLAMA_MODEL", "from-env")
    assert config.source_of("OLLAMA_MODEL") == "env"
    assert config.source_of("NTFY_TOKEN") == "unset"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def test_a_missing_file_reads_as_empty(store):
    assert not store.exists()
    assert config.CONFIG == {"values": {}, "preferences": {}}


def test_an_unparseable_file_is_read_as_empty_and_left_alone(store, caplog):
    # NOT quarantined. store.load_json moves a corrupt file aside, which is
    # right for a machine store and wrong for one a person may hand-edit.
    store.write_text("{ not json", encoding="utf-8")
    config.reload()
    assert config.CONFIG == {"values": {}, "preferences": {}}
    assert store.exists(), "a hand-editable file must not be moved aside"
    assert store.read_text(encoding="utf-8") == "{ not json"
    assert not list(store.parent.glob("*.corrupt-*"))


def test_a_file_that_is_not_an_object_reads_as_empty(store):
    store.write_text("[1, 2, 3]", encoding="utf-8")
    config.reload()
    assert config.CONFIG == {"values": {}, "preferences": {}}


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

def test_apply_writes_and_reports_what_changed(store):
    changed = config.apply({"OLLAMA_MODEL": "gemma5"}, {})
    assert changed == ["OLLAMA_MODEL"]
    assert config.getenv("OLLAMA_MODEL") == "gemma5"
    assert json.loads(store.read_text())["values"]["OLLAMA_MODEL"] == "gemma5"


def test_re_saving_an_unchanged_form_changes_nothing(store):
    config.apply({"OLLAMA_MODEL": "gemma5"}, {})
    before = store.read_bytes()
    assert config.apply({"OLLAMA_MODEL": "gemma5"}, {}) == []
    assert store.read_bytes() == before, "a no-op save must not rewrite the file"


def test_a_rejected_batch_leaves_the_file_byte_identical(store):
    config.apply({"OLLAMA_MODEL": "gemma5"}, {})
    before = store.read_bytes()
    with pytest.raises(config.ConfigError):
        # The first value is fine; the second is out of range. All-or-nothing
        # means neither lands — a half-applied config is how a 4:30 AM task
        # dies unattended.
        config.apply({"OLLAMA_MODEL": "gemma6", "OLLAMA_NUM_CTX": "1"}, {})
    assert store.read_bytes() == before
    assert config.getenv("OLLAMA_MODEL") == "gemma5"


def test_an_empty_value_clears_the_key_back_to_its_default(store):
    config.apply({"OLLAMA_MODEL": "gemma5"}, {})
    config.apply({"OLLAMA_MODEL": ""}, {})
    assert "OLLAMA_MODEL" not in json.loads(store.read_text())["values"]
    assert config.getenv("OLLAMA_MODEL") == "gemma4"


@pytest.mark.parametrize("values, fragment", [
    ({"NOT_A_SETTING": "x"}, "not a known setting"),
    ({"NTFY_TOKEN": "x"}, "secret"),
    ({"WREN_CHAT_PORT": "9999"}, "not editable"),
    ({"OLLAMA_NUM_CTX": "banana"}, "must be a number"),
    ({"OLLAMA_NUM_CTX": "1"}, "at least"),
    ({"OLLAMA_NUM_CTX": "999999999"}, "at most"),
    ({"WREN_LLM_BACKEND": "llamafile"}, "must be one of"),
    ({"WREN_CHAT_BUSY_PROBE": "yes"}, "must be 0 or 1"),
])
def test_the_schema_refuses_what_it_should(store, values, fragment):
    with pytest.raises(config.ConfigError) as excinfo:
        config.apply(values, {})
    assert fragment in str(excinfo.value)
    assert not store.exists(), "a rejected save must write nothing"


def test_the_store_is_locked_and_owner_only(store):
    config.apply({"OLLAMA_MODEL": "gemma5"}, {})
    mode = stat.S_IMODE(store.stat().st_mode)
    assert mode == 0o600, f"settings.json is {oct(mode)}, not owner-only"
    # locked() drops its sidecar beside the store; .gitignore's config/*.lock
    # rule is what keeps it out of the repo.
    assert (store.parent / f"{store.name}.lock").exists()


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #

def test_preferences_start_at_the_shipped_defaults(store):
    assert config.preferences()["persona"]["user_name"] == \
        schema.STRUCTURED_DEFAULTS["persona"]["user_name"]


def test_a_saved_section_replaces_the_shipped_one_whole(store):
    # Never merged: a half-merged list of calendar categories is a worse
    # answer than either version alone.
    config.apply({}, {"persona": {"user_name": "Robin"}})
    persona = config.preferences()["persona"]
    assert persona == {"user_name": "Robin"}
    assert "positioning" not in persona
    # Other sections are untouched.
    assert config.preferences()["job_search"]["states"]


def test_the_pre_settings_file_sits_between_the_two(store, tmp_path, monkeypatch):
    # TRANSITIONAL layer — config/preferences.json, until agent/migrate_settings.py
    # folds it in and renames it away. It has to outrank the shipped example (or a
    # persona edit made before the page existed silently reverts to "Alex") and
    # lose to a saved section (or the page would show a field it cannot change).
    legacy = tmp_path / "preferences.json"
    legacy.write_text(json.dumps({"persona": {"user_name": "Legacy"}}), encoding="utf-8")
    monkeypatch.setenv("WREN_PREFERENCES_FILE", str(legacy))
    assert config.preferences()["persona"] == {"user_name": "Legacy"}
    config.apply({}, {"persona": {"user_name": "Saved"}})
    assert config.preferences()["persona"] == {"user_name": "Saved"}


def test_the_pre_settings_file_degrades_without_being_quarantined(store, tmp_path,
                                                                  monkeypatch, caplog):
    # Same promise as the settings document, for the same reason: a person may
    # hand-edit this file, so a parse failure is logged and read as empty —
    # store.load_json's rename-aside would lose their only copy.
    bad = tmp_path / "preferences.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("WREN_PREFERENCES_FILE", str(bad))
    with caplog.at_level(logging.ERROR):
        assert config.preferences()["persona"] == schema.STRUCTURED_DEFAULTS["persona"]
    assert bad.read_text(encoding="utf-8") == "{not json"
    assert "could not load preferences" in caplog.text


def test_the_pre_settings_file_ignores_what_is_not_a_section(store, tmp_path, monkeypatch):
    # A stray top-level key (the old "location" string, a "_comment") is not a
    # section and must not become one.
    legacy = tmp_path / "preferences.json"
    legacy.write_text(json.dumps({"location": "Portland,OR,US", "_comment": "hi",
                                  "sports": "not an object"}), encoding="utf-8")
    monkeypatch.setenv("WREN_PREFERENCES_FILE", str(legacy))
    merged = config.preferences()
    assert "location" not in merged and "_comment" not in merged
    assert merged["sports"] == schema.STRUCTURED_DEFAULTS["sports"]


def test_a_missing_pre_settings_file_leaves_the_shipped_defaults(store, tmp_path,
                                                                 monkeypatch):
    monkeypatch.setenv("WREN_PREFERENCES_FILE", str(tmp_path / "nope.json"))
    assert config.preferences()["persona"] == schema.STRUCTURED_DEFAULTS["persona"]


def test_an_unknown_section_is_refused(store):
    with pytest.raises(config.ConfigError):
        config.apply({}, {"astrology": {"sign": "leo"}})


# --------------------------------------------------------------------------- #
# The startup warning
# --------------------------------------------------------------------------- #

def test_the_warning_fires_when_env_still_sets_a_key_the_page_owns(store, tmp_path):
    (tmp_path / ".env").write_text("OLLAMA_MODEL=gemma4\n", encoding="utf-8")
    write(store, {"OLLAMA_MODEL": "gemma5"})
    assert len(config.STARTUP_WARNINGS) == 1
    assert "OLLAMA_MODEL" in config.STARTUP_WARNINGS[0]


def test_the_warning_stays_quiet_when_only_secrets_remain_in_env(store, tmp_path):
    # Secrets stay in config/.env on purpose, so "both files exist" would fire
    # forever and train Craig to ignore it.
    (tmp_path / ".env").write_text(
        "NTFY_TOKEN=tk_x\nWREN_CHAT_TOKEN=abc\nSTRAVA_CLIENT_ID=1\n",
        encoding="utf-8")
    write(store, {"OLLAMA_MODEL": "gemma5"})
    assert config.STARTUP_WARNINGS == []


def test_the_warning_stays_quiet_before_anything_is_migrated(store, tmp_path):
    # No settings document yet means .env is still the only source, which is
    # the pre-migration state, not a conflict.
    (tmp_path / ".env").write_text("OLLAMA_MODEL=gemma4\n", encoding="utf-8")
    config.reload()
    assert config.STARTUP_WARNINGS == []


def test_env_keys_are_read_by_name_only(store, tmp_path):
    # The parser must never need the value: this file holds live credentials.
    (tmp_path / ".env").write_text(
        "# a comment\n\nexport OLLAMA_MODEL=gemma4\nOLLAMA_HOST=http://x:1\n",
        encoding="utf-8")
    assert config._env_keys(tmp_path / ".env") == ["OLLAMA_MODEL", "OLLAMA_HOST"]


# --------------------------------------------------------------------------- #
# Path resolution
# --------------------------------------------------------------------------- #

def test_the_paths_resolve_per_call_from_the_environment(tmp_path, monkeypatch):
    # Pinned to a module constant instead, a child interpreter spawned by a
    # test would inherit nothing and write the real config/settings.json.
    monkeypatch.setenv("WREN_SETTINGS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("WREN_ENV_FILE", str(tmp_path / "e"))
    assert config.settings_path() == tmp_path / "s.json"
    assert config.env_path() == tmp_path / "e"
    monkeypatch.delenv("WREN_SETTINGS_FILE")
    monkeypatch.delenv("WREN_ENV_FILE")
    assert config.settings_path().name == "settings.json"
    assert config.settings_path().parent.name == "config"
    assert config.env_path().name == ".env"


def test_the_settings_store_is_redirected_for_the_whole_suite():
    # The conftest backstop, asserted here rather than only in test_conftest.py
    # so a broken redirect fails next to the module it protects.
    assert "WREN_SETTINGS_FILE" in os.environ
    assert config.settings_path().parent != \
        config._ROOT / "config", "the suite is pointed at the real config/"
