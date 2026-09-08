"""The /settings JSON API — read the configuration, and change it.

The only route in this repo that writes configuration, so it is also the only
one that has to be careful about three things at once:

  * A secret's value never leaves this process. A secret row is sent with no
    `value` key at all — absent, not redacted, not an empty string — and only
    `is_set`. There is no shape of this response that carries a token, so no
    future edit to the page can accidentally render one.
  * All-or-nothing. Everything is validated before anything is written, and a
    rejected save leaves config/settings.json byte-identical. A half-applied
    config is how a 4:30 AM task dies unattended.
  * Key names only in the log. A save is worth an audit line; the values are
    the thing we are protecting.

`source` is what makes the page honest. The environment outranks the settings
document (agent/config.py explains why), so a row still set in config/.env
would render a field, accept an edit, save it, and change nothing. Such a row
comes back as source "env" and the page locks it, and config.STARTUP_WARNINGS
names the file to run agent/migrate_settings.py against.
"""

import logging

from flask import Blueprint, jsonify, request

from agent import config, prefs, schema
from chat.auth import _authenticated

logger = logging.getLogger("wren")

settings_bp = Blueprint("settings", __name__)


def _row(setting: schema.Setting) -> dict:
    """One field, as the page needs it.

    Everything the page renders comes from here rather than from a copy of the
    table in JavaScript: chat/static/ is served with no auth check, so a schema
    shipped to the browser as a literal would be readable by anyone who can
    reach the server.
    """
    row = {
        "key": setting.key,
        "group": setting.group,
        "label": setting.label,
        "help": setting.help,
        "type": setting.type,
        "applies": setting.applies,
        "editable": setting.editable,
        "reason": setting.reason,
        "choices": list(setting.choices),
        "minimum": setting.minimum,
        "maximum": setting.maximum,
        "default": setting.default,
        "secret": setting.secret,
        "source": config.source_of(setting.key),
        "is_set": config.is_set(setting.key),
    }
    if not setting.secret:
        row["value"] = config.getenv(setting.key) or ""
    return row


@settings_bp.route("/api/settings", methods=["GET"])
def api_settings():
    """Every field, grouped in the order the page renders them."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({
        "groups": [{"name": name, "rows": [_row(s) for s in rows]}
                   for name, rows in schema.grouped()],
        "preferences": config.preferences(),
        "sections": list(schema.PREFERENCE_SECTIONS),
        "warnings": list(config.STARTUP_WARNINGS),
        "restart_command": schema.RESTART_COMMAND,
    })


def _validate(values: dict, sections: dict) -> dict:
    """Every problem at once, keyed by field, or {} when the save is good.

    config.apply raises on the first bad key, which is right for a script and
    wrong for a form: a page that reports one error per round trip makes the
    user save six times to find six mistakes. This runs the same checks against
    a throwaway staged document — never the live one — so nothing here can
    write.
    """
    errors: dict[str, str] = {}
    staged = config._staged()
    for key, value in values.items():
        try:
            config.set_value(staged, key, value)
        except config.ConfigError as e:
            errors[key] = str(e)
    for name, value in sections.items():
        if name not in schema.PREFERENCE_SECTIONS:
            errors[name] = f"{name} is not a known preference section"
            continue
        problems = prefs.validate_section(name, value)
        if problems:
            errors[name] = "; ".join(problems)
    return errors


@settings_bp.route("/api/settings", methods=["POST"])
def api_save_settings():
    """Validate everything, then write once.

    The restart and next-run lists are computed from the keys that ACTUALLY
    changed, not from the keys the form submitted — re-saving an untouched form
    must not raise a banner telling the user to restart a server for nothing. A
    banner nobody needs is a banner that trains you to ignore the ones you do.
    """
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a JSON object"}), 400
    values = payload.get("values") or {}
    sections = payload.get("preferences") or {}
    if not isinstance(values, dict) or not isinstance(sections, dict):
        return jsonify({"error": "values and preferences must both be objects"}), 400

    errors = _validate(values, sections)
    if errors:
        # Nothing was written: _validate stages into a copy. The page shows each
        # message against its own field.
        logger.info(f"settings save rejected: {sorted(errors)}")
        return jsonify({"error": "some fields were not accepted",
                        "field_errors": errors}), 400

    try:
        changed = config.apply(values, sections)
    except config.ConfigError as e:
        # Unreachable through _validate, but apply() is the authority on what
        # lands and a 500 here would be a lie about which one refused.
        logger.warning(f"settings save refused at write time: {e}")
        return jsonify({"error": str(e)}), 400

    prefs.reload()
    # Key names only. This is the audit line for a configuration change; the
    # values are the thing the rest of this module exists to protect.
    logger.info(f"settings saved: {changed}" if changed else "settings saved: no change")

    return jsonify({
        "changed": changed,
        "restart_required": [k for k in changed
                             if (row := schema.by_key(k)) and row.applies == "restart"],
        "next_run_only": [k for k in changed
                          if (row := schema.by_key(k)) and row.applies == "next_run"],
        "restart_command": schema.RESTART_COMMAND,
        "warnings": list(config.STARTUP_WARNINGS),
    })
