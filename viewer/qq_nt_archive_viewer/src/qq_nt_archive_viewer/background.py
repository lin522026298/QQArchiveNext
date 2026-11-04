"""Cancellable reads. Each task owns its SQLite connections on its own thread."""
from __future__ import annotations

from threading import Event

from PySide6.QtCore import QObject, QRunnable, Signal


class ReadSignals(QObject):
    completed = Signal(str, int, object, object)


class ReadTask(QRunnable):
    def __init__(self, signals, kind, generation, cancel: Event, function):
        super().__init__()
        self.signals, self.kind, self.generation = signals, kind, generation
        self.cancel, self.function = cancel, function

    def run(self):
        value = error = None
        try:
            if not self.cancel.is_set():
                value = self.function(self.cancel)
        except Exception as exc:
            error = str(exc)
        if not self.cancel.is_set():
            self.signals.completed.emit(self.kind, self.generation, value, error)
