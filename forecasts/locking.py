"""
Cross-process locking for per-site forecast generation.

Deduplication used to be a module-level set, which only worked inside one
process. Production runs four gunicorn workers plus a cron process, so that
set never prevented the collision it was written for. These helpers put the
same guard in the database, where every process can see it.
"""

import logging
import os
import socket
from contextlib import contextmanager
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ForecastLock

logger = logging.getLogger(__name__)

# How long before a lock is assumed abandoned. Background runs are daemon
# threads, so a deploy or a worker restart kills them mid-flight and leaves
# the row behind; without a stale window that site would never forecast
# again. Comfortably longer than a real run, which is seconds to a minute.
STALE_AFTER = timedelta(minutes=30)


def _holder():
    return f"{socket.gethostname()}:{os.getpid()}"[:120]


def acquire(site_id: int) -> bool:
    """
    Take the lock for this site, or return False if another process holds it.

    The INSERT is the test: the primary key makes it succeed or raise, with
    no window between checking and claiming.
    """
    cutoff = timezone.now() - STALE_AFTER
    stale = ForecastLock.objects.filter(site_id=site_id, acquired_at__lt=cutoff)
    if stale.exists():
        deleted, _ = stale.delete()
        if deleted:
            logger.warning(
                "Reclaimed a stale forecast lock for site %s (older than %s)",
                site_id, STALE_AFTER,
            )

    try:
        # Its own atomic block: an IntegrityError would otherwise mark an
        # enclosing transaction for rollback.
        with transaction.atomic():
            ForecastLock.objects.create(site_id=site_id, holder=_holder())
        return True
    except IntegrityError:
        return False


def release(site_id: int) -> None:
    """Drop the lock. Safe to call when it is already gone."""
    ForecastLock.objects.filter(site_id=site_id).delete()


@contextmanager
def site_forecast_lock(site_id: int):
    """
    Hold the lock for the duration of the block.

    Yields True if it was acquired, False if someone else holds it — the
    caller decides whether that is a skip or an error. Only releases a lock
    it actually took, so a losing caller cannot free the winner's.
    """
    acquired = acquire(site_id)
    try:
        yield acquired
    finally:
        if acquired:
            release(site_id)
