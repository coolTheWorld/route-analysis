"""Structured local logging with hybrid daily/size rotation and recovery."""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

LOG_FILE_NAME = "route-analysis.log"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 30

_SESSION_ID = uuid.uuid4().hex
_CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "route_analysis_correlation_id", default=_SESSION_ID
)


@dataclass(frozen=True, slots=True)
class LoggingState:
    available: bool
    current_file: Path
    error: str | None = None


def _json_default(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (Path, Enum)):
        return str(value)
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return repr(value)


class JsonEventFormatter(logging.Formatter):
    """Format each event as one UTF-8 JSON object with stable base fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event_name", "message"),
            "correlationId": getattr(record, "correlation_id", _CORRELATION_ID.get()),
        }
        fields = getattr(record, "event_fields", {})
        if isinstance(fields, Mapping):
            payload.update(fields)
        message = record.getMessage()
        if message:
            payload["message"] = message
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":"))


class HybridRotatingFileHandler(logging.handlers.BaseRotatingHandler):
    """Rotate at local midnight or a size limit, retaining newest histories."""

    def __init__(
        self,
        filename: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        clock: Callable[[], datetime] = datetime.now,
        state_callback: Callable[[LoggingState], None] | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("maximum log size must be greater than zero")
        if backup_count < 0:
            raise ValueError("log backup count must not be negative")
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.clock = clock
        self.state_callback = state_callback
        self._active_date = clock().date()
        self.state = LoggingState(True, Path(filename))
        super().__init__(filename, mode="a", encoding="utf-8", delay=True)

    @property
    def path(self) -> Path:
        return Path(self.baseFilename)

    def _publish_state(self, available: bool, error: str | None = None) -> None:
        state = LoggingState(available, self.path, error)
        if state == self.state:
            return
        self.state = state
        if self.state_callback is not None:
            with suppress(Exception):
                self.state_callback(state)

    def _history_path(self) -> Path:
        stem = self.path.stem
        suffix = self.path.suffix
        date_part = self._active_date.isoformat()
        sequence = 1
        while True:
            candidate = self.path.with_name(f"{stem}.{date_part}.{sequence:03d}{suffix}")
            if not candidate.exists():
                return candidate
            sequence += 1

    def _remove_expired_histories(self) -> None:
        histories = sorted(
            self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        for expired in histories[: max(0, len(histories) - self.backup_count)]:
            expired.unlink()

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.clock().date() != self._active_date:
            has_content = (
                self.path.exists() and self.path.is_file() and self.path.stat().st_size > 0
            )
            if not has_content:
                self._active_date = self.clock().date()
            return has_content
        if not self.path.exists() or not self.path.is_file():
            return False
        formatted = self.format(record) + self.terminator
        return self.path.stat().st_size + len(formatted.encode("utf-8")) > self.max_bytes

    def doRollover(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        if self.path.exists() and self.path.is_file() and self.path.stat().st_size > 0:
            os.replace(self.path, self._history_path())
        self._active_date = self.clock().date()
        self._remove_expired_histories()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.shouldRollover(record):
                self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            message = self.format(record)
            self.stream.write(message + self.terminator)
            self.flush()
            self._publish_state(True)
        except Exception as exc:
            if self.stream is not None:
                with suppress(Exception):
                    self.stream.close()
                self.stream = None
            self._publish_state(False, f"{type(exc).__name__}: {exc}")


class LoggingManager:
    def __init__(self, logger: logging.Logger, handler: HybridRotatingFileHandler) -> None:
        self.logger = logger
        self.handler = handler

    @property
    def state(self) -> LoggingState:
        return self.handler.state

    @property
    def current_file(self) -> Path:
        return self.handler.path

    def set_level(self, level: str) -> None:
        normalized = level.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"unsupported log level: {level}")
        numeric_level = logging.getLevelNamesMapping()[normalized]
        self.logger.setLevel(numeric_level)
        self.handler.setLevel(numeric_level)

    def close(self) -> None:
        self.logger.removeHandler(self.handler)
        self.handler.close()


def configure_logging(
    log_dir: Path,
    *,
    level: str = "INFO",
    state_callback: Callable[[LoggingState], None] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> LoggingManager:
    """Configure the application logger without allowing file failures to escape."""

    logger = logging.getLogger("route_analysis")
    logger.propagate = False
    for existing in tuple(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    handler = HybridRotatingFileHandler(
        log_dir / LOG_FILE_NAME,
        max_bytes=max_bytes,
        backup_count=backup_count,
        state_callback=state_callback,
    )
    handler.setFormatter(JsonEventFormatter())
    logger.addHandler(handler)
    manager = LoggingManager(logger, handler)
    manager.set_level(level)
    return manager


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    message: str = "",
    correlation_id: str | None = None,
    exc_info: bool | BaseException | tuple[type[BaseException], BaseException, Any] | None = None,
    **fields: object,
) -> None:
    """Emit a structured event while preserving arbitrary DEBUG diagnostic fields."""

    extra = {
        "event_name": event,
        "event_fields": fields,
        "correlation_id": correlation_id or _CORRELATION_ID.get(),
    }
    logger.log(level, message, extra=extra, exc_info=exc_info)


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Attach one correlation ID to all events in a synchronous operation."""

    value = correlation_id or uuid.uuid4().hex
    token = _CORRELATION_ID.set(value)
    try:
        yield value
    finally:
        _CORRELATION_ID.reset(token)
