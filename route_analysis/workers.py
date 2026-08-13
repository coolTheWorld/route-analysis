"""Small Qt thread-pool adapter that never exposes tracebacks to the UI."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from route_analysis.errors import RouteAnalysisError


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
            self.signals.failed.emit(str(exc))
        except Exception:
            self.signals.failed.emit("操作失败，应用已保留当前数据")
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()
