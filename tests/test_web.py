"""Regression coverage for the shared TUI/browser configuration contract."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bot.config import Config, update_env_file
from bot.web import EventBroker, config_snapshot, secret_snapshot, validate_config_payload


def test_web_snapshot_uses_openai_compatible_config() -> None:
    config = Config(
        discord_token="discord-token",
        api_key="provider-key",
        api_base_url="http://127.0.0.1:11434/v1",
        model="llama3.2",
        cache_members=True,
    )

    snapshot = config_snapshot(config)

    assert snapshot["api_base_url"] == "http://127.0.0.1:11434/v1"
    assert snapshot["model"] == "llama3.2"
    assert snapshot["cache_members"] is True
    assert all("anthropic" not in key for key in snapshot)
    assert secret_snapshot(config) == {"discord_token": True, "api_key": True}


def test_config_payload_maps_every_dashboard_field() -> None:
    updates = validate_config_payload({
        "discord_token": "discord-token",
        "api_key": "local-placeholder",
        "api_base_url": "http://127.0.0.1:1234/v1",
        "model": "local-model",
        "max_tokens": 4096,
        "max_agent_iterations": 6,
        "rate_limit_max": 7,
        "rate_limit_window": 90,
        "bulk_confirm_threshold": 4,
        "enable_punitive": False,
        "cache_members": True,
        "auto_update": True,
        "auto_update_interval": 15,
        "auto_restart": True,
        "restart": False,
    })

    assert updates == {
        "DISCORD_TOKEN": "discord-token",
        "OPENAI_BASE_URL": "http://127.0.0.1:1234/v1",
        "OPENAI_API_KEY": "local-placeholder",
        "OPENAI_MODEL": "local-model",
        "MAX_TOKENS": "4096",
        "MAX_AGENT_ITERATIONS": "6",
        "RATE_LIMIT_MAX": "7",
        "RATE_LIMIT_WINDOW": "90",
        "BULK_CONFIRM_THRESHOLD": "4",
        "ENABLE_PUNITIVE": "false",
        "CACHE_MEMBERS": "true",
        "AUTO_UPDATE": "true",
        "AUTO_UPDATE_INTERVAL": "15",
        "AUTO_RESTART": "true",
    }


def test_blank_secret_fields_keep_saved_values() -> None:
    updates = validate_config_payload({
        "discord_token": "",
        "api_key": "",
        "api_base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4.1-mini",
    })

    assert "DISCORD_TOKEN" not in updates
    assert "OPENAI_API_KEY" not in updates
    assert updates["OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"anthropic_api_key": "old"}, "Unknown configuration field"),
        ({"api_base_url": "localhost:11434/v1"}, "complete http:// or https:// URL"),
        ({"max_tokens": 0}, "must be at least 1"),
        ({"auto_update": "yes"}, "must be true or false"),
        ({"restart": "yes"}, "restart must be true or false"),
        ({"model": "model\nINJECTED=value"}, "must be a single line"),
    ],
)
def test_invalid_config_payloads_are_rejected(payload: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_config_payload(payload)


def test_env_updates_preserve_comments_and_unmentioned_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".env"
    path.write_text("# provider\nOPENAI_API_KEY=keep-me\nOPENAI_MODEL=old\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    update_env_file(
        {
            "OPENAI_MODEL": "new-model",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
        },
        path,
    )

    contents = path.read_text()
    assert contents.startswith("# provider\n")
    assert "OPENAI_API_KEY=keep-me" in contents
    assert "OPENAI_MODEL=new-model" in contents
    assert contents.endswith("OPENAI_BASE_URL=https://api.openai.com/v1\n")
    assert not list(tmp_path.glob(".*.tmp"))


def test_event_broker_drops_oldest_event_for_slow_clients() -> None:
    async def exercise() -> list[tuple[str, object]]:
        broker = EventBroker(queue_size=2)
        queue = broker.subscribe()
        broker.publish("activity", {"id": 1})
        broker.publish("activity", {"id": 2})
        broker.publish("activity", {"id": 3})
        result = [queue.get_nowait(), queue.get_nowait()]
        broker.unsubscribe(queue)
        return result

    assert asyncio.run(exercise()) == [
        ("activity", {"id": 2}),
        ("activity", {"id": 3}),
    ]
