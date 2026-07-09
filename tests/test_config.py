"""Tests for :mod:`app.config`."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.config.settings import Settings


class TestSettings:
    def test_defaults_are_sane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        get_settings.cache_clear()

        settings = Settings()

        assert settings.read_only_mode is True
        assert settings.vm_port == 22
        assert settings.model_name.startswith("gemini")
        assert settings.log_level == "INFO"

    def test_cors_origins_wildcard(self) -> None:
        settings = Settings(cors_origins="*")
        assert settings.cors_origins_list == ["*"]

    def test_cors_origins_list(self) -> None:
        settings = Settings(cors_origins="https://a.example, https://b.example")
        assert settings.cors_origins_list == ["https://a.example", "https://b.example"]

    def test_extra_denylist_parsing(self) -> None:
        settings = Settings(ssh_extra_denylist="rm -rf /home; nc -l 4444")
        assert settings.ssh_extra_denylist_list == ["rm -rf /home", "nc -l 4444"]

    def test_log_level_validation(self) -> None:
        with pytest.raises(ValueError, match="LOG_LEVEL"):
            Settings(log_level="chatty")

    def test_app_env_validation(self) -> None:
        with pytest.raises(ValueError, match="APP_ENV"):
            Settings(app_env="staging-nightly")

    def test_vertex_requires_project(self) -> None:
        with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
            Settings(google_genai_use_vertexai=True, google_cloud_project=None)

    def test_settings_singleton_is_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODEL_NAME", "gemini-2.5-pro")
        get_settings.cache_clear()

        first = get_settings()
        second = get_settings()

        assert first is second
        assert first.model_name == "gemini-2.5-pro"
