"""The one-time move out of config/.env and config/preferences.json.

Every test points WREN_ENV_FILE, WREN_PREFERENCES_FILE and WREN_SETTINGS_FILE
at tmp_path, so nothing here can read or rewrite the developer's real
credential file. The `migrate` fixture is what does it; a test without that
fixture is a test that would.
"""

import json

import pytest

from agent import config, migrate_settings, schema


@pytest.fixture
def migrate(tmp_path, monkeypatch):
    """A .env, a preferences.json and a settings.json, all under tmp_path.

    Returns the three paths. The environment is cleared of every schema key for
    the same reason tests/test_config.py clears it: half the repo has already
    called load_dotenv on the real config/.env by the time a fixture runs, and
    layer 1 would answer with the developer's own values.
    """
    env = tmp_path / ".env"
    prefs_file = tmp_path / "preferences.json"
    settings = tmp_path / "settings.json"
    monkeypatch.setenv("WREN_ENV_FILE", str(env))
    monkeypatch.setenv("WREN_PREFERENCES_FILE", str(prefs_file))
    monkeypatch.setenv("WREN_SETTINGS_FILE", str(settings))
    for key in schema.keys():
        monkeypatch.delenv(key, raising=False)
    env.write_text("", encoding="utf-8")
    config.reload()
    yield env, prefs_file, settings
    config.reload()


def run(apply=False):
    plan, staged = migrate_settings.build_plan()
    if apply:
        migrate_settings.apply(plan, staged)
    return plan


def written(settings):
    return json.loads(settings.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# What moves, and what does not
# --------------------------------------------------------------------------- #

def test_a_non_secret_key_with_a_row_moves(migrate):
    env, _, settings = migrate
    env.write_text("OLLAMA_MODEL=gemma5\n", encoding="utf-8")
    plan = run(apply=True)
    assert plan.values == {"OLLAMA_MODEL": "gemma5"}
    assert written(settings)["values"]["OLLAMA_MODEL"] == "gemma5"
    assert "OLLAMA_MODEL" not in env.read_text(encoding="utf-8")


def test_a_secret_stays_in_the_env_file(migrate):
    env, _, settings = migrate
    env.write_text("NTFY_TOKEN=tk_secret\nOLLAMA_MODEL=gemma5\n", encoding="utf-8")
    plan = run(apply=True)
    assert plan.stay_secret == ["NTFY_TOKEN"]
    # The page is only ever told set/not-set, so moving a secret would put a
    # credential in a second file and buy nothing.
    assert "NTFY_TOKEN=tk_secret" in env.read_text(encoding="utf-8")
    assert "NTFY_TOKEN" not in json.dumps(written(settings))


def test_a_key_with_no_row_stays_and_is_reported(migrate):
    env, _, settings = migrate
    # Real bucket: STRAVA_*, ANTHROPIC_API_KEY and the SCRIBEJAY_* keys have no
    # reader in this repo. Dropping a credential because the schema does not
    # know it would be the worst outcome this script could produce.
    env.write_text("STRAVA_REFRESH_TOKEN=rt_1\nOLLAMA_MODEL=gemma5\n", encoding="utf-8")
    plan = run(apply=True)
    assert plan.stay_unrecognised == ["STRAVA_REFRESH_TOKEN"]
    assert "STRAVA_REFRESH_TOKEN=rt_1" in env.read_text(encoding="utf-8")


def test_a_locked_row_moves_but_stays_locked(migrate):
    env, _, settings = migrate
    # Seven keys in the real .env are non-secret and non-editable. Leaving them
    # behind would keep the environment outranking the document and the startup
    # warning firing forever.
    assert schema.by_key("WREN_CHAT_PORT").editable is False
    env.write_text("WREN_CHAT_PORT=8420\n", encoding="utf-8")
    run(apply=True)
    assert written(settings)["values"]["WREN_CHAT_PORT"] == "8420"
    # The page still refuses it.
    with pytest.raises(config.ConfigError, match="not editable"):
        config.apply({"WREN_CHAT_PORT": "9999"}, {})


def test_a_value_the_schema_refuses_stays_where_it_is(migrate):
    env, _, settings = migrate
    env.write_text("OLLAMA_NUM_CTX=1\nOLLAMA_MODEL=gemma5\n", encoding="utf-8")
    plan = run(apply=True)
    assert [k for k, _ in plan.problems] == ["OLLAMA_NUM_CTX"]
    assert "at least" in plan.problems[0][1]
    # One bad value must not cost the other 25 keys their migration.
    assert plan.values == {"OLLAMA_MODEL": "gemma5"}
    assert "OLLAMA_NUM_CTX=1" in env.read_text(encoding="utf-8")


def test_an_empty_assignment_is_left_alone(migrate):
    env, _, _ = migrate
    # X= is how a shell unsets nothing in particular; the resolver already skips
    # it, so there is no value here to move.
    env.write_text("OLLAMA_MODEL=\n", encoding="utf-8")
    plan = run()
    assert plan.values == {}
    assert not plan.moves_anything


# --------------------------------------------------------------------------- #
# The dry run writes nothing
# --------------------------------------------------------------------------- #

def test_the_dry_run_writes_nothing(migrate, capsys):
    env, prefs_file, settings = migrate
    env.write_text("OLLAMA_MODEL=gemma5\n", encoding="utf-8")
    prefs_file.write_text(json.dumps({"sports": {"teams": []}}), encoding="utf-8")
    before_env = env.read_bytes()
    before_prefs = prefs_file.read_bytes()

    assert migrate_settings.main([]) == 0

    assert env.read_bytes() == before_env
    assert prefs_file.read_bytes() == before_prefs
    assert not settings.exists()
    assert not list(env.parent.glob(".env.pre-settings-*"))
    assert "DRY RUN" in capsys.readouterr().out


def test_apply_needs_the_flag(migrate):
    env, _, settings = migrate
    env.write_text("OLLAMA_MODEL=gemma5\n", encoding="utf-8")
    assert migrate_settings.main(["--apply"]) == 0
    assert settings.exists()


# --------------------------------------------------------------------------- #
# Nothing is clobbered, and a second run does nothing
# --------------------------------------------------------------------------- #

def test_an_existing_value_is_kept_not_overwritten(migrate):
    env, _, settings = migrate
    config.apply({"OLLAMA_MODEL": "already-chosen"}, {})
    env.write_text("OLLAMA_MODEL=from-env\n", encoding="utf-8")
    plan = run(apply=True)
    assert plan.values == {}
    assert plan.skipped == [("OLLAMA_MODEL", "already in settings.json, left as it is")]
    assert written(settings)["values"]["OLLAMA_MODEL"] == "already-chosen"


def test_running_it_twice_is_safe(migrate):
    env, prefs_file, settings = migrate
    env.write_text("OLLAMA_MODEL=gemma5\nNTFY_TOKEN=tk_1\n", encoding="utf-8")
    prefs_file.write_text(json.dumps({"sports": {"teams": []}}), encoding="utf-8")
    run(apply=True)
    after_first_env = env.read_bytes()
    after_first_settings = settings.read_bytes()

    plan = run(apply=True)
    assert not plan.moves_anything
    assert env.read_bytes() == after_first_env
    assert settings.read_bytes() == after_first_settings
    # One backup, from the first run. A no-op run leaves no litter.
    assert len(list(env.parent.glob(".env.pre-settings-*"))) == 1


def test_the_env_file_is_backed_up_before_it_is_rewritten(migrate):
    env, _, _ = migrate
    original = "OLLAMA_MODEL=gemma5\nNTFY_TOKEN=tk_1\n"
    env.write_text(original, encoding="utf-8")
    run(apply=True)
    backups = list(env.parent.glob(".env.pre-settings-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------- #
# Preference sections
# --------------------------------------------------------------------------- #

def test_a_section_moves_whole_and_the_file_is_renamed(migrate):
    _, prefs_file, settings = migrate
    teams = {"teams": [{"league": "mlb", "id": "2", "name": "Red Sox"}]}
    prefs_file.write_text(json.dumps({"sports": teams}), encoding="utf-8")
    run(apply=True)
    assert written(settings)["preferences"]["sports"] == teams
    # Renamed, never deleted: it is the only copy of what a person wrote by
    # hand. The new name is what stops a stale loader from finding it.
    assert not prefs_file.exists()
    retired = prefs_file.with_name(prefs_file.name + ".migrated")
    assert json.loads(retired.read_text(encoding="utf-8"))["sports"] == teams


def test_a_section_the_validator_refuses_is_left_behind(migrate):
    _, prefs_file, settings = migrate
    prefs_file.write_text(json.dumps({
        "sports": {"teams": [{"league": "mlb", "id": "2", "name": "Red Sox"}]},
        "persona": {"user_name": "A"},          # missing two required fields
    }), encoding="utf-8")
    plan = run(apply=True)
    assert list(plan.sections) == ["sports"]
    assert [name for name, _ in plan.problems] == ["persona"]
    assert "persona.positioning" in plan.problems[0][1]
    assert prefs_file.exists(), "the file still holds a section, so it stays put"


def test_a_non_section_key_is_skipped_not_moved(migrate):
    _, prefs_file, settings = migrate
    prefs_file.write_text(json.dumps({
        "_comment": "hand-written note",
        "sports": {"teams": []},
    }), encoding="utf-8")
    plan = run(apply=True)
    assert ("_comment", "not a preference section, not moved") in plan.skipped
    assert "_comment" not in written(settings)["preferences"]


def test_an_unparseable_preferences_file_is_reported_not_dropped(migrate):
    env, prefs_file, _ = migrate
    prefs_file.write_text("{ not json", encoding="utf-8")
    env.write_text("OLLAMA_MODEL=gemma5\n", encoding="utf-8")
    plan = run(apply=True)
    assert any("could not be read" in why for _, why in plan.problems)
    assert prefs_file.read_text(encoding="utf-8") == "{ not json"


# --------------------------------------------------------------------------- #
# location -> DEFAULT_LOCATION
# --------------------------------------------------------------------------- #

def test_location_becomes_a_schema_row(migrate):
    _, prefs_file, settings = migrate
    prefs_file.write_text(json.dumps({"location": "Newfields,NH,US"}), encoding="utf-8")
    plan = run(apply=True)
    assert plan.values == {"DEFAULT_LOCATION": "Newfields,NH,US"}
    assert written(settings)["values"]["DEFAULT_LOCATION"] == "Newfields,NH,US"


def test_the_env_files_location_wins_over_the_preferences_one(migrate):
    env, prefs_file, settings = migrate
    # Both files are read in the same run, and the environment outranks the
    # document — so writing the preferences value would produce a field that
    # shows one place and resolves to another.
    env.write_text("DEFAULT_LOCATION=Portsmouth,NH,US\n", encoding="utf-8")
    prefs_file.write_text(json.dumps({"location": "Newfields,NH,US"}), encoding="utf-8")
    plan = run(apply=True)
    assert plan.values["DEFAULT_LOCATION"] == "Portsmouth,NH,US"
    assert ("location", "config/.env already sets DEFAULT_LOCATION, kept that") \
        in plan.skipped


# --------------------------------------------------------------------------- #
# Rewriting config/.env
# --------------------------------------------------------------------------- #

def test_rewrite_drops_the_key_and_the_comment_glued_above_it():
    text = (
        "# Ollama\n"
        "# Context window per chat call.\n"
        "OLLAMA_MODEL=gemma5\n"
        "\n"
        "# A section header, followed by a blank line.\n"
        "\n"
        "NTFY_TOKEN=tk_1\n"
    )
    out = migrate_settings.rewrite_env(text, {"OLLAMA_MODEL"})
    assert "OLLAMA_MODEL" not in out
    assert "Context window" not in out, "a glued comment documents its own key"
    assert "# Ollama" not in out
    assert "# A section header" in out, "a comment before a blank line heads a section"
    assert "NTFY_TOKEN=tk_1" in out


def test_rewrite_leaves_every_key_it_was_not_asked_about():
    text = "A=1\nOLLAMA_MODEL=gemma5\nB=2\n"
    out = migrate_settings.rewrite_env(text, {"OLLAMA_MODEL"})
    assert out == "A=1\nB=2\n"


def test_rewrite_handles_the_export_prefix():
    out = migrate_settings.rewrite_env("export OLLAMA_MODEL=gemma5\nA=1\n",
                                       {"OLLAMA_MODEL"})
    assert out == "A=1\n"


def test_rewrite_collapses_the_gaps_it_leaves():
    # Cosmetic, but a file left with six blank lines in a row reads as damaged,
    # and this is the file a person hand-edits.
    text = "A=1\n\n\nOLLAMA_MODEL=gemma5\n\n\nB=2\n"
    assert migrate_settings.rewrite_env(text, {"OLLAMA_MODEL"}) == "A=1\n\nB=2\n"


def test_rewrite_of_nothing_changes_nothing():
    text = "A=1\n# note\nB=2\n"
    assert migrate_settings.rewrite_env(text, set()) == text


# --------------------------------------------------------------------------- #
# The real files
# --------------------------------------------------------------------------- #

def test_every_key_in_the_shipped_example_env_is_accounted_for(migrate):
    env, _, _ = migrate
    # config/.env.example is the documented shape of the real file. Every key in
    # it must land in exactly one bucket — a key in none of them is a key this
    # script would silently pass over.
    example = config._ROOT / "config" / ".env.example"
    env.write_text(
        "\n".join(f"{key}=placeholder" for key in config._env_keys(example)),
        encoding="utf-8")
    config.reload()
    plan = run()
    seen = (set(plan.values) | set(plan.stay_secret) | set(plan.stay_unrecognised)
            | {name for name, _ in plan.skipped} | {name for name, _ in plan.problems})
    assert seen == set(config._env_keys(example))


def test_the_preferences_file_is_renamed_only_when_it_is_empty_of_work(migrate):
    # The rename is what stops a stale loader finding the file. Doing it while a
    # refused section is still inside would hide that section behind a name
    # nothing loads — the user's own persona gone, with no message.
    _, prefs_file, _ = migrate
    prefs_file.write_text(json.dumps({
        "sports": {"teams": []},
        "persona": {"user_name": "A"},
    }), encoding="utf-8")
    plan = run(apply=True)
    assert plan.preferences_blocked
    assert prefs_file.exists()
    assert not prefs_file.with_name(prefs_file.name + ".migrated").exists()
