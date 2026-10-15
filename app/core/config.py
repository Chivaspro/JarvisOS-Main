"""Validated configuration for the optional JARVIS service layer."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration loaded from environment variables."""

    base_dir: Path
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 8010
    environment: str = "development"

    @classmethod
    def from_environment(cls, base_dir: Path | None = None) -> "AppConfig":
        root = (base_dir or Path(__file__).resolve().parents[2]).resolve()
        raw_port = os.getenv("JARVIS_PORT", "8010")
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError("JARVIS_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("JARVIS_PORT must be between 1 and 65535")
        database = Path(os.getenv("JARVIS_DATABASE", str(root / "data" / "jarvis.db")))
        if not database.is_absolute():
            database = root / database
        return cls(
            base_dir=root,
            database_path=database.resolve(),
            host=os.getenv("JARVIS_HOST", "127.0.0.1"),
            port=port,
            environment=os.getenv("JARVIS_ENV", "development"),
        )
