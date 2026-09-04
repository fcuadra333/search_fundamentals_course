"""Application configuration.

Every value can be overridden with an environment variable so the same image
runs in development, in the hosted product and in an on-premise deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VAR_DIR = PROJECT_ROOT / "var"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí"}


@dataclass(frozen=True)
class MatchingSettings:
    """Tunables for the deterministic stages. Kept out of the code so a client
    can be tuned without a deploy, but with conservative defaults."""

    # Stage 2: exact match tolerance in days (0 or 1 per the specification).
    exact_date_slack_days: int = 1
    # Stage 3: fuzzy date window in days.
    fuzzy_date_window_days: int = 5
    # Stage 3: maximum number of items aggregated on one side of an N:1 match.
    max_group_size: int = 4
    # Stage 3: how far apart grouped items may sit. A consolidated deposit
    # gathers several days of collections, so it needs more room than a pair.
    group_date_window_days: int = 15
    # Stage 3: candidates considered per grouping target, nearest by date. Caps
    # the combinatorics so a 4,000-line statement stays inside a night window.
    max_group_pool: int = 25
    # Stage 3: date window for a match carried by a matching reference alone.
    reference_date_window_days: int = 120
    # Minimum length of a reference before it may be used as a substring key.
    min_reference_length: int = 4
    # Minimum description similarity (0-1) to break a tie in stage 3.
    min_description_similarity: float = 0.45
    # Amounts never carry a tolerance: a reconciliation that does not tie to the
    # cent is not a reconciliation. Kept explicit so nobody "improves" it later.
    amount_tolerance_cents: int = 0


@dataclass(frozen=True)
class ModelSettings:
    """Model routing. Two tiers only: no frontier models (CLAUDE.md)."""

    provider: str = field(default_factory=lambda: os.getenv("CUADRE_LLM_PROVIDER", "stub"))
    economy_model: str = field(
        default_factory=lambda: os.getenv("CUADRE_MODEL_ECONOMY", "claude-haiku-4-5-20251001")
    )
    mid_model: str = field(
        default_factory=lambda: os.getenv("CUADRE_MODEL_MID", "claude-sonnet-5")
    )
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    # Stage 4 batching: exceptions per model call, and the token ceiling that
    # keeps the context short by design (~10K tokens per call).
    exceptions_per_call: int = field(default_factory=lambda: _env_int("CUADRE_EXC_PER_CALL", 15))
    candidates_per_exception: int = field(default_factory=lambda: _env_int("CUADRE_CANDIDATES", 8))
    max_retries: int = 2
    request_timeout_seconds: int = 120
    # Use the provider batch API when available: reconciliation is night work.
    prefer_batch_api: bool = field(default_factory=lambda: _env_bool("CUADRE_BATCH_API", False))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "CUADRE_DATABASE_URL", f"sqlite:///{DEFAULT_VAR_DIR / 'cuadre.db'}"
        )
    )
    secret_key: str = field(
        default_factory=lambda: os.getenv("CUADRE_SECRET_KEY", "dev-only-change-me")
    )
    blob_dir: Path = field(
        default_factory=lambda: Path(os.getenv("CUADRE_BLOB_DIR", str(DEFAULT_VAR_DIR / "blobs")))
    )
    export_dir: Path = field(
        default_factory=lambda: Path(os.getenv("CUADRE_EXPORT_DIR", str(DEFAULT_VAR_DIR / "exports")))
    )
    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("CUADRE_MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
    )
    session_hours: int = field(default_factory=lambda: _env_int("CUADRE_SESSION_HOURS", 12))
    # Ages an exception is flagged at, in days.
    exception_aging_buckets: tuple[int, ...] = (30, 60, 90)
    matching: MatchingSettings = field(default_factory=MatchingSettings)
    models: ModelSettings = field(default_factory=ModelSettings)

    def ensure_dirs(self) -> None:
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        DEFAULT_VAR_DIR.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def reset_settings() -> None:
    """Test hook: force the next get_settings() to re-read the environment."""
    global _settings
    _settings = None
