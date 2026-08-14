import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from route_analysis.logging_setup import (
    HybridRotatingFileHandler,
    JsonEventFormatter,
    LoggingState,
    configure_logging,
    log_event,
)


def test_info_default_writes_structured_utf8_and_omits_debug(tmp_path: Path) -> None:
    manager = configure_logging(tmp_path / "log")
    logger = logging.getLogger("route_analysis.test")

    log_event(logger, logging.INFO, "command_loaded", command_id="命令-1", point_count=12)
    log_event(logger, logging.DEBUG, "full_response", body={"token": "明文"})
    manager.close()

    records = [
        json.loads(line)
        for line in (tmp_path / "log" / "route-analysis.log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["event"] for record in records] == ["command_loaded"]
    assert records[0]["command_id"] == "命令-1"
    assert records[0]["point_count"] == 12
    assert records[0]["level"] == "INFO"
    assert records[0]["correlationId"]


def test_debug_level_immediately_records_unredacted_fields(tmp_path: Path) -> None:
    manager = configure_logging(tmp_path / "log", level="INFO")
    manager.set_level("DEBUG")
    logger = logging.getLogger("route_analysis.api")

    log_event(
        logger,
        logging.DEBUG,
        "http_exchange",
        password="plain-password",
        headers={"Authorization": "Bearer secret", "tenant": "suntae"},
        response={"path": [[1, 2, 3]]},
    )
    manager.close()

    record = json.loads(
        (tmp_path / "log" / "route-analysis.log").read_text(encoding="utf-8")
    )
    assert record["password"] == "plain-password"
    assert record["headers"]["Authorization"] == "Bearer secret"
    assert record["response"]["path"] == [[1, 2, 3]]


def test_handler_rotates_on_size_or_local_date_and_retains_latest_files(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 8, 14, 23, 59)]
    handler = HybridRotatingFileHandler(
        tmp_path / "route-analysis.log",
        max_bytes=220,
        backup_count=2,
        clock=lambda: current[0],
    )
    handler.setFormatter(JsonEventFormatter())
    logger = logging.getLogger("rotation-test")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for index in range(8):
        log_event(logger, logging.INFO, "sized", index=index, payload="值" * 30)
    current[0] += timedelta(days=1)
    log_event(logger, logging.INFO, "next_day", index=9)
    handler.close()

    histories = sorted(tmp_path.glob("route-analysis.*.log"))
    assert len(histories) == 2
    assert (tmp_path / "route-analysis.log").exists()
    assert "next_day" in (tmp_path / "route-analysis.log").read_text(encoding="utf-8")


def test_logging_failure_is_non_blocking_and_next_event_retries(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    blocking_directory = log_dir / "route-analysis.log"
    blocking_directory.mkdir()
    states: list[LoggingState] = []
    manager = configure_logging(log_dir, state_callback=states.append)
    logger = logging.getLogger("route_analysis.recovery")

    log_event(logger, logging.INFO, "first_attempt")
    assert manager.state.available is False
    assert manager.state.error

    blocking_directory.rmdir()
    log_event(logger, logging.INFO, "recovered")
    manager.close()

    assert any(state.available is False for state in states)
    assert any(state.available is True for state in states)
    assert "recovered" in blocking_directory.read_text(encoding="utf-8")
