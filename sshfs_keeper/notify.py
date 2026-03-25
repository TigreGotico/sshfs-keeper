"""Webhook-based notification delivery for mount events."""

import asyncio
import datetime
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Events emitted by the monitor
EVENT_FAILURE = "failure"
EVENT_RECOVERY = "recovery"
EVENT_BACKOFF = "backoff"


async def send_webhook(url: str, event: str, mount: str, error: Optional[str] = None) -> None:
    """POST a JSON notification to *url*.  Fire-and-forget — errors are logged, not raised.

    Args:
        url: HTTP/HTTPS endpoint that accepts a JSON POST body.
        event: One of ``"failure"``, ``"recovery"``, or ``"backoff"``.
        mount: Name of the affected mount.
        error: Optional human-readable error message.
    """
    import httpx  # deferred import — optional at call site

    payload = {
        "event": event,
        "mount": mount,
        "error": error,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        log.debug("[notify] %s event for '%s' delivered (HTTP %s)", event, mount, resp.status_code)
    except Exception as exc:  # pragma: no cover — network errors
        log.warning("[notify] failed to deliver %s event for '%s': %s", event, mount, exc)


async def notify(
    *,
    webhook_url: Optional[str],
    on_failure: bool,
    on_recovery: bool,
    on_backoff: bool,
    event: str,
    mount: str,
    error: Optional[str] = None,
) -> None:
    """Conditionally send a webhook notification based on event type and config flags.

    Args:
        webhook_url: Destination URL; if ``None`` notifications are disabled.
        on_failure: Whether to send ``"failure"`` events.
        on_recovery: Whether to send ``"recovery"`` events.
        on_backoff: Whether to send ``"backoff"`` events.
        event: Event type string.
        mount: Mount name.
        error: Optional error detail.
    """
    if not webhook_url:
        return
    if event == EVENT_FAILURE and not on_failure:
        return
    if event == EVENT_RECOVERY and not on_recovery:
        return
    if event == EVENT_BACKOFF and not on_backoff:
        return
    asyncio.create_task(send_webhook(webhook_url, event, mount, error))
