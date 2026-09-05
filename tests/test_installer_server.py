"""app/installer/server.py's HTTP API — the wizard backend.

Deliberately routed at real .env files under tmp_path (via monkeypatched
module globals), never the developer machine's own `.env`. Network/systemd/
ALSA calls are stubbed — this tests the wizard's own request handling and
`.env` bookkeeping, not systemd or Gemini's servers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.installer import gemini_check, server


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(server, "_DEPLOY_DIR", tmp_path / "deploy")

    r1_template = tmp_path / "r1.example"
    g1_template = tmp_path / "g1.example"
    r1_template.write_text("# r1 example\n# GEMINI_API_KEY=\nROBOT_PROFILE=r1\n", encoding="utf-8")
    g1_template.write_text("# g1 example\n# GEMINI_API_KEY=\nROBOT_PROFILE=g1\n", encoding="utf-8")
    monkeypatch.setitem(server._PROFILE_META["r1"], "template", r1_template)
    monkeypatch.setitem(server._PROFILE_META["g1"], "template", g1_template)

    return TestClient(server.app)


def test_state_before_any_profile_is_chosen(client: TestClient) -> None:
    body = client.get("/api/state").json()
    assert body["profile"] is None
    assert body["env_exists"] is False
    assert body["api_key_set"] is False


def test_choosing_a_profile_creates_env_from_the_matching_template(client: TestClient) -> None:
    res = client.post("/api/profile", json={"profile": "g1"})
    assert res.status_code == 200

    state = client.get("/api/state").json()
    assert state["profile"] == "g1"
    assert state["env_exists"] is True


def test_api_key_endpoint_requires_a_profile_first(client: TestClient) -> None:
    res = client.post("/api/api-key", json={"api_key": "some-key"})
    assert res.status_code == 400


def test_api_key_is_saved_and_the_check_result_is_reported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/profile", json={"profile": "r1"})

    async def fake_check(_key: str) -> gemini_check.KeyCheckResult:
        return gemini_check.KeyCheckResult(True, "Ключ рабочий.")

    monkeypatch.setattr(gemini_check, "check_api_key", fake_check)

    res = client.post("/api/api-key", json={"api_key": "AIza-fake-test-key"})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "message": "Ключ рабочий."}

    state = client.get("/api/state").json()
    assert state["api_key_set"] is True


def test_wake_words_are_trimmed_deduped_of_blanks_and_joined(client: TestClient) -> None:
    client.post("/api/profile", json={"profile": "r1"})

    res = client.post("/api/wake-words", json={"phrases": ["робот", " ассистент ", "", "  "]})
    assert res.status_code == 200
    assert res.json()["phrases"] == ["робот", "ассистент"]

    state = client.get("/api/state").json()
    assert state["wake_word_phrases"] == "робот,ассистент"


def test_guard_kb_status_is_rejected_for_the_r1_profile(client: TestClient) -> None:
    client.post("/api/profile", json={"profile": "r1"})
    res = client.get("/api/guard-kb/status")
    assert res.status_code == 400


def test_wall_agent_is_rejected_for_the_g1_profile(client: TestClient) -> None:
    client.post("/api/profile", json={"profile": "g1"})
    res = client.post("/api/wall-agent", json={"url": "http://example.invalid:8093"})
    assert res.status_code == 400


def test_advanced_settings_requires_a_profile_first(client: TestClient) -> None:
    res = client.get("/api/advanced-settings")
    assert res.status_code == 400


def test_advanced_settings_shows_schema_defaults_when_env_has_no_override(client: TestClient) -> None:
    client.post("/api/profile", json={"profile": "r1"})
    res = client.get("/api/advanced-settings")
    assert res.status_code == 200
    fields = {f["key"]: f for group in res.json()["groups"] for f in group["fields"]}
    assert fields["MAX_SESSION_DURATION_S"]["value"] == "90"


def test_advanced_settings_reflects_a_saved_override(client: TestClient) -> None:
    client.post("/api/profile", json={"profile": "r1"})
    res = client.post("/api/advanced-settings", json={"values": {"MAX_SESSION_DURATION_S": "120"}})
    assert res.status_code == 200
    assert res.json()["saved"] == ["MAX_SESSION_DURATION_S"]

    fields = {f["key"]: f for group in client.get("/api/advanced-settings").json()["groups"] for f in group["fields"]}
    assert fields["MAX_SESSION_DURATION_S"]["value"] == "120"


def test_advanced_settings_hides_the_other_profiles_fields(client: TestClient) -> None:
    client.post("/api/profile", json={"profile": "r1"})
    r1_keys = {f["key"] for group in client.get("/api/advanced-settings").json()["groups"] for f in group["fields"]}
    assert "WALL_AGENT_TOKEN" in r1_keys
    assert "GUARD_KB_PATH" not in r1_keys

    client.post("/api/profile", json={"profile": "g1"})
    g1_keys = {f["key"] for group in client.get("/api/advanced-settings").json()["groups"] for f in group["fields"]}
    assert "GUARD_KB_PATH" in g1_keys
    assert "WALL_AGENT_TOKEN" not in g1_keys


def test_advanced_settings_ignores_unknown_keys(client: TestClient, tmp_path: Path) -> None:
    client.post("/api/profile", json={"profile": "r1"})
    res = client.post(
        "/api/advanced-settings",
        json={"values": {"NOT_A_REAL_SETTING": "x", "TOOL_TIMEOUT_S": "5"}},
    )
    assert res.json()["saved"] == ["TOOL_TIMEOUT_S"]
    assert "NOT_A_REAL_SETTING" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_finish_refuses_to_start_the_service_without_an_api_key(client: TestClient) -> None:
    client.post("/api/profile", json={"profile": "r1"})
    res = client.post("/api/finish")
    assert res.status_code == 400
