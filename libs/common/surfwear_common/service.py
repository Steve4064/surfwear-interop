"""Utilitaires communs aux services FastAPI."""
from __future__ import annotations

import logging
import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def setup_logging(service: str) -> logging.Logger:
    logging.basicConfig(
        level=env("LOG_LEVEL", "INFO"),
        format=f"%(asctime)s {service} %(levelname)s %(name)s : %(message)s",
    )
    return logging.getLogger(service)
