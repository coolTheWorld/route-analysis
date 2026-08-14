"""Small Qt thread-pool adapter that never exposes tracebacks to the UI."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from route_analysis.errors import RouteAnalysisError
from route_analysis.logging_setup import log_event

LOGGER = logging.getLogger(__name__)


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class Worker[T](QRunnable):
    def __init__(self, operation: Callable[[], T]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation()
        except RouteAnalysisError as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "worker_operation_failed",
                message=str(exc),
                exc_info=True,
                thread=threading.current_thread().name,
                operation=repr(self.operation),
            )
            self.signals.failed.emit(str(exc))
        except Exception:
            log_event(
                LOGGER,
                logging.ERROR,
                "worker_unhandled_exception",
                exc_info=True,
                thread=threading.current_thread().name,
                operation=repr(self.operation),
            )
            self.signals.failed.emit("操作失败，应用已保留当前数据")
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()
