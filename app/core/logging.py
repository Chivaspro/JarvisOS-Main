"""Safe structured logging helpers."""
from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_path: Path | None = None) -> logging.Logger:
    """Configure the service logger once and return it."""
    logger = logging.getLogger("jarvis.service")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger
