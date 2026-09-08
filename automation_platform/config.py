"""Application configuration.

Everything here can be overridden by an environment variable so a deployment
never has to edit code. The defaults are the ones the demo runs with.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .engine.cost import PriceTable
from .engine.drift import DEFAULT_DRIFT_THRESHOLD

DEFAULT_DATABASE = Path(os.environ.get("PLATFORM_DB", "var/platform.sqlite3"))


@dataclass
class Config:
    database_path: Path = DEFAULT_DATABASE
    secret_key: str = field(default_factory=lambda: os.environ.get("PLATFORM_SECRET", "dev-secret"))
    default_locale: str = os.environ.get("PLATFORM_LOCALE", "es")
    drift_threshold: float = float(os.environ.get("PLATFORM_DRIFT_THRESHOLD", DEFAULT_DRIFT_THRESHOLD))
    dry_runs: int = int(os.environ.get("PLATFORM_DRY_RUNS", 10))
    dry_run_threshold: int = int(os.environ.get("PLATFORM_DRY_RUN_THRESHOLD", 9))
    # Prices are placeholders until an operator sets the ones they actually pay;
    # everything derived from them is reported as estimated until then.
    prices: PriceTable = field(default_factory=PriceTable)
    export_dir: Path = Path(os.environ.get("PLATFORM_EXPORT_DIR", "var/exports"))

    def __post_init__(self) -> None:
        # Flask resolves relative paths against the package directory, so both
        # paths are made absolute here rather than at every use site.
        self.database_path = Path(self.database_path).resolve()
        self.export_dir = Path(self.export_dir).resolve()

    @classmethod
    def from_env(cls) -> "Config":
        return cls(database_path=Path(os.environ.get("PLATFORM_DB", str(DEFAULT_DATABASE))))
