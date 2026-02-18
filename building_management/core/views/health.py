from __future__ import annotations

import time

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from ..models import Building, WorkOrder

METRICS_CACHE_KEY = "forwarding_health:metrics"


def _is_staff(user) -> bool:
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def _rate_limit_exceeded(request) -> bool:
    limit, window = getattr(settings, "FORWARDING_HEALTH_RATE_LIMIT", (30, 60))
    identifier = getattr(getattr(request, "user", None), "pk", None)
    if identifier is None:
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded_for:
            identifier = forwarded_for.split(",")[0].strip()
        else:
            identifier = request.META.get("REMOTE_ADDR", "unknown")
    cache_key = f"forwarding_health:rate:{identifier}"
    record = cache.get(cache_key)
    now = time.time()
    if not record:
        cache.set(cache_key, (1, now), window)
        return False
    count, start = record
    if now - start >= window:
        cache.set(cache_key, (1, now), window)
        return False
    if count >= limit:
        return True
    ttl = max(1, int(window - (now - start)))
    cache.set(cache_key, (count + 1, start), ttl)
    return False


def _calculate_forwarding_metrics():
    now = timezone.now()
    office_ids = list(
        Building.objects.filter(is_system_default=True).values_list("id", flat=True)
    )
    office_status = "ok"
    office_id = None
    duplicate_ids: list[int] = []
    if not office_ids:
        office_status = "missing"
    elif len(office_ids) > 1:
        office_status = "duplicate"
        duplicate_ids = office_ids[:]
    else:
        office_id = office_ids[0]

    pending_forwarding = 0
    forwarded_pipeline = 0
    if office_ids:
        aggregates = (
            WorkOrder.objects.filter(
                building_id__in=office_ids,
                archived_at__isnull=True,
            ).aggregate(
                pending=Count("id", filter=Q(forwarded_to_building__isnull=True)),
                forwarded=Count("id", filter=Q(forwarded_to_building__isnull=False)),
            )
        )
        pending_forwarding = aggregates.get("pending") or 0
        forwarded_pipeline = aggregates.get("forwarded") or 0

    awaiting_approval = WorkOrder.objects.filter(
        status=WorkOrder.Status.AWAITING_APPROVAL,
        archived_at__isnull=True,
    ).count()

    payload = {
        "timestamp": now.isoformat(),
        "office_building_id": office_id,
        "office_status": office_status,
        "office_queue_size": pending_forwarding,
        "forwarded_pipeline_size": forwarded_pipeline,
        "awaiting_approval_total": awaiting_approval,
    }
    if duplicate_ids:
        payload["duplicate_office_ids"] = duplicate_ids
    return payload


def _get_forwarding_metrics():
    timeout = getattr(settings, "FORWARDING_HEALTH_CACHE_TIMEOUT", 30)
    metrics = cache.get(METRICS_CACHE_KEY)
    if metrics is not None:
        return metrics, True
    metrics = _calculate_forwarding_metrics()
    cache.set(METRICS_CACHE_KEY, metrics, timeout)
    return metrics, False


@login_required
@require_GET
def forwarding_health(request):
    if not _is_staff(request.user):
        return HttpResponseForbidden("Staff access required.")
    if _rate_limit_exceeded(request):
        return JsonResponse(
            {"detail": "Forwarding health endpoint rate limit exceeded."},
            status=429,
        )
    metrics, cache_hit = _get_forwarding_metrics()
    payload = dict(metrics)
    payload["cache_hit"] = cache_hit
    return JsonResponse(payload)
