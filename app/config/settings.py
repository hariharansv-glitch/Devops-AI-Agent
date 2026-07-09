"""Application settings.

Configuration is read from environment variables (a local ``.env`` file is
automatically picked up during development). Values are validated with Pydantic
v2 so that invalid settings fail fast at process startup rather than at first
use.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed application settings.

    All fields map 1:1 to environment variables. See ``.env.example`` for the
    documentation of each field.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------- LLM provider
    # The MODEL_NAME prefix decides which provider is used:
    #   - "gemini-*"    -> Google Gemini (needs google_api_key OR Vertex AI)
    #   - "groq/*"      -> Groq via LiteLLM (needs groq_api_key)
    #   - "openai/*"    -> OpenAI via LiteLLM (needs OPENAI_API_KEY)
    #   - "anthropic/*" -> Anthropic via LiteLLM (needs ANTHROPIC_API_KEY)
    google_api_key: Optional[str] = Field(default=None)
    google_genai_use_vertexai: bool = Field(default=False)
    google_cloud_project: Optional[str] = Field(default=None)
    google_cloud_location: str = Field(default="us-central1")
    groq_api_key: Optional[str] = Field(default=None)
    model_name: str = Field(default="gemini-2.5-flash")

    # ---------------------------------------------------------------------- VM
    vm_host: str = Field(default="localhost")
    vm_port: int = Field(default=22, ge=1, le=65535)
    vm_user: str = Field(default="root")
    vm_password: Optional[str] = Field(default=None)
    vm_private_key: Optional[str] = Field(default=None)
    vm_private_key_passphrase: Optional[str] = Field(default=None)

    ssh_connect_timeout: float = Field(default=15.0, ge=1.0, le=300.0)
    ssh_command_timeout: float = Field(default=60.0, ge=1.0, le=3600.0)
    ssh_auto_add_host_keys: bool = Field(default=True)

    # ------------------------------------------------------------- Application
    app_name: str = Field(default="ai-devops-assistant")
    app_env: str = Field(default="development")
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: str = Field(default="*")

    # ------------------------------------------------------------------ Safety
    read_only_mode: bool = Field(default=True)
    ssh_extra_denylist: str = Field(default="")

    # ----------------------------------------------------------------- Logging
    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="logs")
    log_json: bool = Field(default=False)

    # ---------------------------------------------------------------- Derived
    @property
    def cors_origins_list(self) -> List[str]:
        """Return :attr:`cors_origins` parsed into a list."""
        raw = (self.cors_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def ssh_extra_denylist_list(self) -> List[str]:
        """Return additional command patterns forbidden on the SSH channel."""
        raw = (self.ssh_extra_denylist or "").strip()
        if not raw:
            return []
        return [pattern.strip() for pattern in raw.split(";") if pattern.strip()]

    @property
    def log_dir_path(self) -> Path:
        """Return :attr:`log_dir` as an absolute :class:`~pathlib.Path`."""
        return Path(self.log_dir).expanduser().resolve()

    @property
    def uses_vertex_ai(self) -> bool:
        """Return ``True`` when Gemini calls should be routed through Vertex."""
        return bool(self.google_genai_use_vertexai)

    @property
    def llm_provider(self) -> str:
        """Return the provider key inferred from :attr:`model_name`.

        Examples
        --------
        - ``"gemini-2.5-flash"``            -> ``"gemini"``
        - ``"groq/llama-3.3-70b-versatile"``-> ``"groq"``
        - ``"openai/gpt-4o-mini"``          -> ``"openai"``
        """
        name = (self.model_name or "").strip().lower()
        if not name:
            return "gemini"
        if name.startswith("gemini"):
            return "gemini"
        if "/" in name:
            return name.split("/", 1)[0]
        return "gemini"

    # -------------------------------------------------------------- Validation
    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(
                f"LOG_LEVEL must be one of {sorted(allowed)}; got {value!r}"
            )
        return upper

    @field_validator("app_env")
    @classmethod
    def _validate_app_env(cls, value: str) -> str:
        allowed = {"development", "staging", "production", "test"}
        lower = value.lower()
        if lower not in allowed:
            raise ValueError(
                f"APP_ENV must be one of {sorted(allowed)}; got {value!r}"
            )
        return lower

    @model_validator(mode="after")
    def _validate_auth_material(self) -> "Settings":
        """Ensure the credential matching the chosen LLM provider is present.

        The only hard requirement raised here is the Vertex AI one - other
        missing credentials are surfaced later by the runner so that unit
        tests and dry-runs can import :class:`Settings` without real keys.
        """
        if self.uses_vertex_ai and not self.google_cloud_project:
            raise ValueError(
                "GOOGLE_CLOUD_PROJECT must be set when GOOGLE_GENAI_USE_VERTEXAI=TRUE"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Using :func:`functools.lru_cache` guarantees that configuration is loaded
    exactly once per process, which is critical for logging setup and the
    ADK runner initialization.
    """
    return Settings()


__all__ = ["Settings", "get_settings"]
