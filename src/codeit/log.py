"""Structured logging setup.

JSON lines go to `{data_dir}/logs/codeit.jsonl`; a readable copy goes to stderr.
Bind `run_id` and `ticket_key` with `bind_run()` so every line carries them.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

LOG_FILE_NAME = "codeit.jsonl"


def configure_logging(log_dir: Path, level: int = logging.INFO, console: bool = True) -> Path:
    """Configure stdlib logging and structlog. Returns the JSON log file path."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILE_NAME

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    handlers: list[logging.Handler] = [file_handler]

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=shared,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=False),
                ],
            )
        )
        handlers.append(console_handler)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    return log_file


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def bind_run(run_id: str | None = None, ticket_key: str | None = None) -> None:
    """Attach run context to all log lines emitted from the current task."""
    structlog.contextvars.bind_contextvars(run_id=run_id, ticket_key=ticket_key)


def clear_run() -> None:
    structlog.contextvars.unbind_contextvars("run_id", "ticket_key")
