from __future__ import annotations

from loguru import logger
from pathlib import Path
import sys


def setup_logging(log_file: str, level: str = "INFO") -> None:
    # Nettoyer handlers par défaut
    logger.remove()
    logger.add(sys.stdout, level=level)
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(log_file, level=level, rotation="5 MB", retention="7 days", compression="zip")
