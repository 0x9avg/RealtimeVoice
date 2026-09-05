"""RobotSpeaker's pacing (app/robot_audio.py) — item 10 of the 2026-09-04
fixes list ("робот иногда запинается при воспроизведении голоса").

No real vendor SDK/DDS here: the fake client is pre-assigned to
`RobotSpeaker._client` directly (bypassing `_ensure_client`, which
short-circuits once `_client` is already set), so these tests run without
unitree_sdk2py installed.
"""

from __future__ import annotations

from app.robot_audio import RobotSpeaker


class FakeAudioClient:
    def __init__(self) -> None:
        self.play_calls = 0

    def PlayStream(self, app_name: str, stream_id: str, chunk: bytes) -> tuple[int, str]:
        self.play_calls += 1
        return (0, "")

    def PlayStop(self, app_name: str) -> tuple[int, str]:
        return (0, "")


def make_speaker() -> RobotSpeaker:
    speaker = RobotSpeaker(network_interface="eth0")
    speaker._client = FakeAudioClient()
    return speaker


async def test_priming_burst_fires_once_per_utterance_not_every_play_call() -> None:
    """Regression: the "prime the vendor buffer" backdating used to fire on
    EVERY play() call whenever the pacer had merely caught up to real time
    between two chunks of the SAME utterance (the normal case, since Gemini
    delivers chunks roughly as fast as they play) -- audible as a stutter,
    not just a one-time buffer prime at the true start of an utterance."""
    speaker = make_speaker()
    pcm = b"\x00\x00" * 800  # 50ms of 16kHz mono silence, already at target rate

    assert speaker._is_first_chunk_of_utterance is True

    await speaker.play(pcm, sample_rate_hz=16000)
    assert speaker._is_first_chunk_of_utterance is False, "first play() must prime and clear the flag"

    # A second AudioChunk of the SAME utterance, arriving right after the
    # first (as it normally would) -- must NOT re-arm priming.
    await speaker.play(pcm, sample_rate_hz=16000)
    assert speaker._is_first_chunk_of_utterance is False, (
        "a second play() within the same utterance must not re-trigger the priming burst"
    )


async def test_stop_rearms_priming_for_the_next_utterance() -> None:
    speaker = make_speaker()
    pcm = b"\x00\x00" * 800

    await speaker.play(pcm, sample_rate_hz=16000)
    assert speaker._is_first_chunk_of_utterance is False

    await speaker.stop()
    assert speaker._is_first_chunk_of_utterance is True, (
        "stop() (called after every turn, not just barge-in) must rearm priming for the NEXT utterance"
    )
