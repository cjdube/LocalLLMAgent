"""Session-auth check shared by the chat server and its route blueprints.

Kept in its own tiny module so the blueprints (chat/routes_dashboard.py,
chat/routes_opportunities.py) and chat/server.py all import it one-way, without
importing each other."""

from flask import session


def _authenticated() -> bool:
    return bool(session.get("authenticated"))
