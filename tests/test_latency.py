"""Guards against the event pump ever being blocked again.

Response time is the metric that matters most for this robot, and every
regression found in the latency audit had the same shape: something slow
was awaited *inside* the loop that reads Gemini's socket, so audio,
transcripts and — worst — barge-ins all queued behind it. These tests
assert the pump keeps moving while slow things happen.
"""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.gemini_live import ToolCall, Transcript
from app.robot_audio import HalfDuplexGate
from app.session_hub import SessionHub
from app.tools import ToolResult


def make_hub(**overrides: object) -> SessionHub:
    return SessionHub(Settings(**overrides))  # type: ignore[arg-type]


class _SlowTools:
    """A tool that hangs, like a wall agent whose PC is switched off."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.started = asyncio.Event()

    def names(self) -> list[str]:
        return ["slow_tool"]

    def get(self, name: str) -> None:
        return None

    async def dispatch(self, name: str, args: dict) -> ToolResult:
        self.started.set()
        await asyncio.sleep(self.delay)
        return ToolResult(ok=True, text="done")


class _Call:
    """Stands in for gemini_live.ToolCall (frozen slots dataclass)."""

    def __init__(self) -> None:
        self.call_id = "c1"
        self.name = "slow_tool"
        self.args: dict = {}
        self.responded_with: str | None = None

    async def respond(self, result: str) -> None:
        self.responded_with = result


async def test_a_hanging_tool_does_not_block_the_event_pump() -> None:
    """Regression: tools were dispatched inline. A wall agent on an
    unreachable PC (5s timeout) or a slow embedding call (20s) froze audio
    playback, transcripts and barge-in handling for that whole time."""
    hub = make_hub()
    hub.tools = _SlowTools(delay=5.0)  # type: ignore[assignment]

    delivered: list[str] = []

    async def record(message: dict, **kwargs: object) -> None:
        delivered.append(message["type"])

    hub._broadcast_json = record  # type: ignore[method-assign]

    call = _Call()
    started = asyncio.get_running_loop().time()
    await hub._handle_event(call)  # type: ignore[arg-type]
    # ...and an ordinary event right behind it must still get through
    await hub._handle_event(Transcript(role="model", text="still alive"))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.1, f"pump blocked {elapsed:.2f}s behind a slow tool"
    assert "transcript" in delivered, "events behind a slow tool never arrived"

    for task in list(hub._tool_tasks):
        task.cancel()


async def test_a_hung_tool_is_capped_and_the_model_still_gets_an_answer() -> None:
    """The model holds its turn until the tool answers, so a tool that
    never returns is a visitor standing in silence. Past the cap we tell
    the model it failed instead of waiting the network timeout out."""
    hub = make_hub(tool_timeout_s=0.2)
    hub.tools = _SlowTools(delay=30.0)  # type: ignore[assignment]

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    hub._broadcast_json = noop  # type: ignore[method-assign]

    call = _Call()
    await hub._run_tool(call)  # type: ignore[arg-type]

    assert call.responded_with is not None, "model was left waiting forever"
    assert "вовремя" in call.responded_with, call.responded_with


async def test_barge_in_reopens_the_mic_immediately() -> None:
    """The post-speech guard exists so the robot doesn't hear its own tail.
    On a barge-in the mic is picking up the VISITOR mid-sentence, so arming
    it there threw away the start of the very speech we needed."""
    hub = make_hub()

    class DummySpeaker:
        async def stop(self) -> None:
            return None

    # speaker is a read-only property resolving through _speakers/
    # _active_speaker_target (see SessionHub.__init__).
    hub._speakers = {"robot": DummySpeaker()}  # type: ignore[dict-item]
    hub._active_speaker_target = "robot"
    hub.gate = HalfDuplexGate(guard_s=0.75)
    hub.gate.on_speaking_started()

    await hub._silence_speaker(arm_guard=False)
    assert hub.gate.mic_muted is False, "mic stayed deaf right as the visitor spoke"

    # a normal end-of-turn still arms the guard
    hub.gate.on_speaking_started()
    await hub._silence_speaker(arm_guard=True)
    assert hub.gate.mic_muted is True


def test_post_speech_guard_default_is_not_the_old_three_quarters_of_a_second() -> None:
    """Dead time on every single turn — the visitor answers immediately and
    whatever lands inside this window is discarded."""
    assert Settings().robot_post_speech_guard_ms <= 400


async def test_repeated_question_costs_no_embedding_call() -> None:
    """Visitors ask the same handful of things all day; the second time
    must not pay another round trip to Google."""
    from app.knowledge import KnowledgeBase, KnowledgeChunk

    calls: list[list[str]] = []

    class OfflineKB(KnowledgeBase):
        async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            return [[1.0, 0.0] for _ in texts]

    kb = OfflineKB(directory=Settings().knowledge_dir, api_key="x", embedding_model="m")  # type: ignore[arg-type]
    kb._chunks = [KnowledgeChunk(source="a.md", text="t", embedding=[1.0, 0.0])]

    await kb.search("где кабинет 305?", top_k=1)
    await kb.search("где кабинет 305?", top_k=1)
    assert len(calls) == 1, f"asked the embedding API {len(calls)} times for one question"
