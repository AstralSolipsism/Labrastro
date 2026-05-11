from __future__ import annotations

import pytest

from reuleauxcoder.domain.config.models import Config, GitHubConfig, PersistenceConfig
from reuleauxcoder.services.config.loader import ConfigLoader


def test_github_config_masks_webhook_secret() -> None:
    config = GitHubConfig(
        enabled=True,
        app_id="123",
        installation_id="456",
        private_key_path="/run/secrets/github.pem",
        webhook_secret="super-secret-value",
    )

    masked = config.to_dict(mask_secret=True)

    assert "webhook_secret" not in masked
    assert masked["webhook_secret_hint"] == "supe...alue"


def test_github_env_refs_are_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LABRASTRO_GITHUB_APP_ID", "123")
    monkeypatch.setenv("LABRASTRO_GITHUB_INSTALLATION_ID", "456")
    monkeypatch.setenv("LABRASTRO_GITHUB_WEBHOOK_SECRET", "secret")

    expanded = ConfigLoader()._expand_env_refs(
        {
            "github": {
                "enabled": True,
                "app_id": "${LABRASTRO_GITHUB_APP_ID}",
                "installation_id": "${LABRASTRO_GITHUB_INSTALLATION_ID}",
                "private_key_path": "/run/secrets/github.pem",
                "webhook_secret": "${LABRASTRO_GITHUB_WEBHOOK_SECRET}",
            }
        }
    )

    assert expanded["github"]["app_id"] == "123"
    assert expanded["github"]["installation_id"] == "456"
    assert expanded["github"]["webhook_secret"] == "secret"


def test_github_enabled_requires_postgres() -> None:
    config = Config(
        api_key="test",
        github=GitHubConfig(
            enabled=True,
            app_id="123",
            installation_id="456",
            private_key_path="/run/secrets/github.pem",
            webhook_secret="secret",
        ),
        persistence=PersistenceConfig(backend="memory"),
    )

    assert "github.enabled requires Postgres persistence.database_url" in config.validate()
