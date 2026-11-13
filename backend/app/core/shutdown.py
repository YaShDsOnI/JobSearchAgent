"""Graceful shutdown for long-running processes (worker and scheduler loops).

SIGTERM and SIGINT only set a flag. The loop finishes the task or tick in progress and
exits at its next check, so a deploy never abandons a claimed task mid-run; task leases
still cover hard crashes.
"""

import signal
import threading
from types import FrameType


class StopSignal:
    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> "StopSignal":
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._handle)
        return self

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        self._event.set()

    def stop(self) -> None:
        self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True as soon as a stop was requested."""
        return self._event.wait(max(seconds, 0))
