"""Regression test for the API facade's chat_set_conversation_roster.

Pins the kwargs the facade is expected to forward to the inner ChatAPI.
A previous version of the facade dropped ``team_id`` from its signature,
so every roster pick from the UI (solo, multi-agent, or saved-team)
raised ``TypeError`` at the seam between ``routes/chat.py`` and the
domain API. The route always passes ``team_id=`` (None or a value), so
this test pins that the facade forwards it unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.api import API


def _bare_api() -> API:
    """API instance without running the heavy __init__.

    The facade method under test only reaches into ``self._chat_api``, so
    we bypass __init__ and stub that single attribute.
    """
    api = object.__new__(API)
    api._chat_api = MagicMock()
    api._chat_api.chat_set_conversation_roster.return_value = {
        "ok": True, "agent_id": None, "team_id": None,
    }
    return api


def test_forwards_team_id_when_present():
    api = _bare_api()

    api.chat_set_conversation_roster("c_1", ["a", "b"], team_id="t_42")

    api._chat_api.chat_set_conversation_roster.assert_called_once_with(
        "c_1", ["a", "b"], team_id="t_42",
    )


def test_forwards_none_team_id_when_omitted():
    api = _bare_api()

    api.chat_set_conversation_roster("c_1", ["a"])

    api._chat_api.chat_set_conversation_roster.assert_called_once_with(
        "c_1", ["a"], team_id=None,
    )


def test_route_passes_keyword_team_id():
    """Pins the call shape used by routes/chat.py.

    The route calls the facade with ``team_id=body.team_id`` as a keyword
    arg. If the facade signature ever drops ``team_id`` again, this
    invocation pattern would raise ``TypeError`` — which is exactly the
    UI symptom (every roster pick failed, pill stayed "No agent").
    """
    api = _bare_api()

    api.chat_set_conversation_roster(
        "c_1", ["a", "b", "c"], team_id=None,
    )
    api.chat_set_conversation_roster(
        "c_1", [], team_id="t_saved",
    )

    assert api._chat_api.chat_set_conversation_roster.call_count == 2
