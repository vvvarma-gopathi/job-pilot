from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from ..db import JobRepository

logger = logging.getLogger("jobpilot.discovery.agent")
T = TypeVar("T")


@dataclass(frozen=True)
class TaskOutcome(Generic[T]):
    task_id: str
    status: str
    result: T | None = None
    error: str | None = None


class AgentTaskManager:
    def __init__(self, repository: JobRepository):
        self.repository = repository

    def execute(
        self,
        task_type: str,
        dedupe_key: str,
        action: Callable[[], T],
        *,
        max_retries: int = 3,
        related_job_id: int | None = None,
        related_application_id: int | None = None,
    ) -> TaskOutcome[T]:
        task_id, _ = self.repository.create_agent_task(
            task_type,
            dedupe_key,
            max_retries=max_retries,
            related_job_id=related_job_id,
            related_application_id=related_application_id,
        )
        if not self.repository.claim_agent_task(task_id):
            task = self.repository.agent_task(task_id)
            status = task["status"] if task else "unknown"
            return TaskOutcome(task_id=task_id, status=status)
        try:
            result = action()
        except Exception as exc:
            error = f"{type(exc).__name__}: task failed; inspect local agent logs"
            self.repository.finish_agent_task(task_id, status="failed", error_details=error)
            logger.error("Agent task %s failed with %s", task_type, type(exc).__name__)
            return TaskOutcome(task_id=task_id, status="failed", error=error)
        self.repository.finish_agent_task(task_id, status="completed")
        return TaskOutcome(task_id=task_id, status="completed", result=result)
