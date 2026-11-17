from __future__ import annotations

import logging
import threading
from pathlib import Path

from .orchestrator import AgentOrchestrator

logger = logging.getLogger("jobpilot.discovery.agent")


class AgentScheduler:
    def __init__(self, orchestrator: AgentOrchestrator, stop_file: str | Path):
        self.orchestrator = orchestrator
        self.stop_file = Path(stop_file)

    def run_once(self) -> dict:
        recovered = self.orchestrator.repository.recover_agent_tasks()
        outcomes = self.orchestrator.run_cycle(force=True)
        return {"recovered": recovered, "outcomes": outcomes}

    def run_forever(
        self,
        *,
        stop_event: threading.Event | None = None,
        poll_seconds: int = 15,
    ) -> None:
        if poll_seconds < 1:
            raise ValueError("poll_seconds must be at least one")
        self.orchestrator.repository.recover_agent_tasks()
        stop_event = stop_event or threading.Event()
        while not stop_event.is_set() and not self.stop_file.exists():
            self.orchestrator.run_cycle()
            stop_event.wait(poll_seconds)
        logger.info("Local JobPilot agent stopped.")
