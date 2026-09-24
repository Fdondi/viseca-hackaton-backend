"""(I) Worker: long-poll the platform and answer every request before its deadline.

Runs in a background thread so it keeps deciding while step-ups wait for the
customer. A 204 means "nothing yet", not "done". Errors back off and retry;
the engine's ledger makes redeliveries idempotent.
"""
from __future__ import annotations

import threading
import time

from .platform import PlatformError
from .session import Session


class Worker(threading.Thread):
    def __init__(self, session: Session, wait: int = 25) -> None:
        super().__init__(daemon=True, name="leash-worker")
        self.session = session
        self.wait = wait
        self._halt = threading.Event()
        self.handled = 0
        self.last_error: str | None = None

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        backoff = 0.5
        while not self._halt.is_set():
            try:
                env = self.session.platform.next_request(wait=self.wait)
            except PlatformError as exc:
                self.last_error = exc.message
                time.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            except Exception as exc:  # network trouble: keep the loop alive
                self.last_error = repr(exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 10)
                continue
            backoff = 0.5
            if env is None:
                continue
            try:
                self.session.handle(env)
                self.handled += 1
            except Exception as exc:
                self.last_error = repr(exc)
