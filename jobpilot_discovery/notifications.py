from __future__ import annotations

import logging

logger = logging.getLogger("jobpilot.discovery.notifications")


class WindowsNotifier:
    def __init__(self, *, enabled: bool = True, notification_factory=None):
        self.enabled = enabled
        self.notification_factory = notification_factory

    def notify(self, title: str, message: str) -> bool:
        if not self.enabled:
            return False
        notification_factory = self.notification_factory
        if notification_factory is None:
            try:
                from winotify import Notification
            except ImportError:
                logger.info("Windows notification support is unavailable; event remains in SQLite.")
                return False
            notification_factory = Notification
        try:
            notification_factory(
                app_id="JobPilot AI",
                title=title[:64],
                msg=message[:200],
            ).show()
        except (OSError, RuntimeError):
            logger.warning("Windows notification could not be shown; event remains in SQLite.")
            return False
        return True
