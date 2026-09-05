"""A visit interview must survive the Gemini session being recreated.

The interview's state lives in Python (app/visit_intake.py) and survives any
reconnect; the MODEL's memory of being mid-interview does not. Two
mechanisms cover that, and both are tested here:

* the proactive session swap is DEFERRED while an interview is running
  (app/session_hub.py's _safe_to_refresh, wired into
  GeminiLiveAdapter.refresh_guard), and
* any reconnect we didn't choose — Gemini closing a long or idle session,
  which was observed happening every couple of minutes — re-explains the
  situation to the fresh session (_resume_intake_after_reconnect).
"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.gemini_live import ConnectionStateChanged
from app.scenarios import INTAKE_RESUME_MARKER
from app.session_hub import SessionHub


def _g1_hub() -> SessionHub:
    settings = Settings(robot_profile="g1")
    return SessionHub(settings)


def test_refresh_is_allowed_when_no_interview_is_running() -> None:
    hub = _g1_hub()
    assert hub._safe_to_refresh() is True


def test_refresh_is_deferred_while_an_interview_is_running() -> None:
    hub = _g1_hub()
    intake = hub.tools.get("visit_intake")
    assert intake is not None, "g1 profile must register visit_intake"

    intake._intake.start()  # visitor asked to register
    assert intake.engaged is True
    assert hub._safe_to_refresh() is False, "swapped the session mid-interview"

    # ...and allowed again once the interview ends
    intake._intake.consume("отмена")
    assert intake.engaged is False
    assert hub._safe_to_refresh() is True


def test_r1_profile_has_no_intake_and_never_defers() -> None:
    """R1 has no visit_intake tool at all — the guard must not assume one."""
    hub = SessionHub(Settings(robot_profile="r1"))
    assert hub.tools.get("visit_intake") is None
    assert hub._safe_to_refresh() is True


async def test_reconnect_puts_the_model_back_on_script() -> None:
    hub = _g1_hub()
    intake = hub.tools.get("visit_intake")
    assert intake is not None

    intake._intake.start()
    intake._intake.consume("Иванов Иван Иванович")
    pending = intake.current_question
    assert "организации" in pending, pending

    sent: list[str] = []

    class CapturingAdapter:
        async def send_text(self, text: str) -> None:
            sent.append(text)

    hub.adapter = CapturingAdapter()  # type: ignore[assignment]

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    hub._broadcast_json = noop  # type: ignore[method-assign]

    # First connection of the process: nothing to resume.
    await hub._handle_event(ConnectionStateChanged(state="connected"))
    assert sent == []

    # A later one is a RE-connect — the fresh session must be told where the
    # interview left off, and with the exact pending question.
    await hub._handle_event(ConnectionStateChanged(state="connected"))
    assert len(sent) == 1, sent
    assert sent[0].startswith(INTAKE_RESUME_MARKER)
    assert pending in sent[0]


async def test_reconnect_says_nothing_when_no_interview_is_running() -> None:
    hub = _g1_hub()
    sent: list[str] = []

    class CapturingAdapter:
        async def send_text(self, text: str) -> None:
            sent.append(text)

    hub.adapter = CapturingAdapter()  # type: ignore[assignment]

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    hub._broadcast_json = noop  # type: ignore[method-assign]

    await hub._handle_event(ConnectionStateChanged(state="connected"))
    await hub._handle_event(ConnectionStateChanged(state="connected"))
    assert sent == []


def test_current_question_tracks_the_interview_position() -> None:
    """What gets replayed to the model has to be the question actually
    pending — including the confirmation read-back at the end."""
    from app.visit_intake import VisitIntake

    vi = VisitIntake()
    assert vi.current_question == ""  # idle

    vi.start()
    assert "фамилию" in vi.current_question  # first question now -- no language step

    for answer in ("Иванов Иван", "ООО Ромашка", "встреча", "к Петрову", "да", "да"):
        vi.consume(answer)
    assert "Верно" in vi.current_question  # confirmation read-back


async def test_undeliverable_tool_response_is_logged_not_swallowed(caplog) -> None:
    """The model blocks its turn waiting for a tool result, so a dropped one
    is the robot going mute mid-answer — it must at least be diagnosable."""
    from app.gemini_live import GeminiLiveAdapter
    from app.tools import ToolRegistry

    adapter = GeminiLiveAdapter(settings=Settings(), tools=ToolRegistry())
    assert adapter._ws is None  # no connection

    with caplog.at_level("WARNING", logger="gemini_live"):
        await adapter._send_tool_response("call-1", "visit_intake", "next question")

    assert any("tool_response_undeliverable" in r.getMessage() for r in caplog.records), (
        caplog.records
    )


async def test_tool_response_send_failure_is_logged(caplog) -> None:
    from app.gemini_live import GeminiLiveAdapter
    from app.tools import ToolRegistry

    class DeadWS:
        async def send(self, payload: str) -> None:
            raise ConnectionResetError("socket went away mid-send")

    adapter = GeminiLiveAdapter(settings=Settings(), tools=ToolRegistry())
    adapter._ws = DeadWS()  # type: ignore[assignment]

    with caplog.at_level("WARNING", logger="gemini_live"):
        await adapter._send_tool_response("call-2", "search_guard_kb", "room 305")

    assert any("tool_response_undeliverable" in r.getMessage() for r in caplog.records)
    await asyncio.sleep(0)
