"""Standalone runner for a live test on the real robot: `uv run python -m app.robot_main`.

No browser, no FastAPI — this pumps the head mic's multicast stream straight
into `GeminiLiveAdapter` and plays its audio back through the real speaker
(see app/robot_audio.py). Everything else (tools, adapter, Gemini protocol)
is exactly what app/main.py already uses for the browser-based session.

Half-duplex by necessity: this hardware has no acoustic echo cancellation,
so mic frames are dropped while the robot is speaking, and for
`robot_post_speech_guard_ms` after — otherwise it reliably hears (and reacts
to) its own voice. Ported from ../../humanoid-robot/apps/voice-live/src/
humanoid_robot/voice_live/runner.py's `_handle_mic_frame`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.config import settings
from app.gemini_live import (
    AudioChunk,
    ConnectionStateChanged,
    GeminiLiveAdapter,
    Interrupted,
    ToolCall,
    Transcript,
    TurnComplete,
)
from app.camera_hub import camera_hub, sampled
from app.g1_tools import log_relay_divergence
from app.presence import PresenceRunner
from app.robot_audio import HalfDuplexGate, RobotMic, RobotSpeaker
from app.scenarios import PRESENCE_MARKER
from app.tool_registry import build_tool_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOG = logging.getLogger("robot")


async def main() -> None:
    api_key = settings.resolved_api_key()
    if not api_key:
        _LOG.error("no Gemini API key set: export %s", settings.api_key_env)
        return

    tools = build_tool_registry(settings)
    adapter = GeminiLiveAdapter(settings=settings, tools=tools)
    mic = RobotMic(
        multicast_group=settings.robot_mic_multicast_group,
        multicast_port=settings.robot_mic_multicast_port,
        interface_ip=settings.robot_mic_interface_ip,
        input_channels=settings.robot_mic_input_channels,
    )
    speaker = RobotSpeaker(
        network_interface=settings.robot_speaker_network_interface,
        volume_pct=settings.robot_speaker_volume_pct,
    )
    gate = HalfDuplexGate(guard_s=settings.robot_post_speech_guard_ms / 1000.0)
    # See app/main.py's identical bookkeeping for why this accumulates
    # fragments instead of comparing the first Transcript(role="model") chunk.
    last_tool_name: str | None = None
    last_tool_text = ""
    model_turn_fragments: list[str] = []

    async def mic_task() -> None:
        async for pcm in mic.stream():
            if gate.mic_muted:
                continue
            await adapter.send_audio(pcm)

    async def presence_task() -> None:
        """Greets a visitor once they appear (R1 ТЗ §4.1 / G1 ТЗ §3.1) — see
        app/presence.py and app/scenarios.py's PRESENCE_MARKER. Samples the
        shared app/camera_hub.py singleton rather than polling the DDS
        VideoClient a second time on its own."""

        async def on_visitor_detected() -> None:
            _LOG.info("presence.visitor_detected")
            await adapter.send_text(PRESENCE_MARKER)

        runner = PresenceRunner(
            frames=sampled(camera_hub, fps=settings.presence_fps),
            on_visitor_detected=on_visitor_detected,
            threshold=settings.presence_threshold,
            rearm_s=settings.presence_rearm_s,
        )
        await runner.run()

    async def gemini_task() -> None:
        nonlocal last_tool_name, last_tool_text, model_turn_fragments
        async for event in adapter.events():
            match event:
                case AudioChunk(pcm=pcm):
                    gate.on_speaking_started()
                    await speaker.play(pcm, sample_rate_hz=settings.receive_sample_rate_hz)
                case TurnComplete():
                    gate.on_speaking_stopped()
                    # See RobotSpeaker.stop's docstring — closing the vendor
                    # stream after every turn (not just on barge-in) avoids
                    # the second+ utterance going silently mute.
                    await speaker.stop()
                    if last_tool_name is not None and model_turn_fragments:
                        log_relay_divergence(
                            last_tool_name, last_tool_text, "".join(model_turn_fragments)
                        )
                    last_tool_name, last_tool_text, model_turn_fragments = None, "", []
                    _LOG.info("turn complete")
                case Interrupted():
                    gate.on_speaking_stopped()
                    await speaker.stop()
                    last_tool_name, last_tool_text, model_turn_fragments = None, "", []
                    _LOG.info("interrupted — barge-in")
                case ConnectionStateChanged(state=state, detail=detail):
                    _LOG.info("status=%s %s", state, detail)
                case Transcript(role=role, text=text):
                    if role == "model":
                        model_turn_fragments.append(text)
                    _LOG.info("[%s] %s", role, text)
                case ToolCall() as call:
                    _LOG.info("tool_call name=%s args=%s", call.name, call.args)
                    result = await tools.dispatch(call.name, call.args)
                    await call.respond(result.text)
                    last_tool_name, last_tool_text, model_turn_fragments = call.name, result.text, []
                    _LOG.info("tool_result name=%s ok=%s text=%s", call.name, result.ok, result.text)

    _LOG.info(
        "starting — profile=%s tools=%s, mic %s:%d via %s, speaker via %s, wall agent %s",
        settings.robot_profile,
        tools.names(),
        settings.robot_mic_multicast_group,
        settings.robot_mic_multicast_port,
        settings.robot_mic_interface_ip,
        settings.robot_speaker_network_interface,
        settings.wall_agent_url,
    )
    tasks = [asyncio.create_task(mic_task(), name="mic"), asyncio.create_task(gemini_task(), name="gemini")]
    if settings.presence_enabled:
        tasks.append(asyncio.create_task(presence_task(), name="presence"))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await mic.close()
        await adapter.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
