"""Push notifications via Firebase Cloud Messaging.

Phase 2: sending is not implemented yet. Callers already invoke notify_new_lead() after
committing, so wiring FCM in later is a change to this file only."""

import logging
import uuid

log = logging.getLogger(__name__)


def notify_new_lead(lead_id: uuid.UUID, title: str, body: str) -> None:
    """Never raises: a push failure must not fail the request that created the lead."""
    try:
        log.info("new lead %s: %s / %s (push not configured yet)", lead_id, title, body)
    except Exception:  # pragma: no cover
        log.exception("push notification failed")
