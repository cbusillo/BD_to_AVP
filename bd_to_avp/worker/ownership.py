from __future__ import annotations

import os
import signal
import threading

from types import FrameType
from typing import Callable

import psutil


class WorkerCancelled(Exception):
    pass


class WorkerProcessOwner:
    def __init__(self) -> None:
        self.cancellation_event = threading.Event()
        self._cleanup_lock = threading.Lock()
        self._signal_cleanup_lock = threading.Lock()
        self._signal_cleanup_started = False

    def establish_session(self) -> int:
        if os.getpgrp() != os.getpid():
            os.setsid()
        return os.getpgrp()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, _signum: int, _frame: FrameType | None) -> None:
        self.cancellation_event.set()
        with self._signal_cleanup_lock:
            if self._signal_cleanup_started:
                return
            self._signal_cleanup_started = True
        threading.Thread(
            target=self.terminate_descendants,
            kwargs={"timeout": 0.5},
            name="worker-descendant-cleanup",
            daemon=True,
        ).start()

    def request_cancel(self) -> None:
        self.cancellation_event.set()
        self.terminate_descendants()

    def check_cancelled(self) -> None:
        if self.cancellation_event.is_set():
            raise WorkerCancelled("The worker job was cancelled.")

    def terminate_descendants(self, timeout: float = 1.5) -> None:
        with self._cleanup_lock:
            try:
                descendants = psutil.Process().children(recursive=True)
            except psutil.Error:
                return

            for process in descendants:
                try:
                    process.terminate()
                except psutil.Error:
                    continue

            _, alive = psutil.wait_procs(descendants, timeout=timeout)
            for process in alive:
                try:
                    process.kill()
                except psutil.Error:
                    continue


SessionSetup = Callable[[], int]
