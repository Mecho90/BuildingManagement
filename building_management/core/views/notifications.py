from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View

from ..models import Notification
from ..services import NotificationService
from .common import _safe_next_url

__all__ = ["NotificationSnoozeView"]

class NotificationSnoozeView(LoginRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, key: str, *args, **kwargs):
        service = NotificationService(request.user)
        try:
            note = Notification.objects.get(user=request.user, key=key)
        except Notification.DoesNotExist:
            return JsonResponse({"error": "not_found"}, status=404)

        is_hx = bool(request.headers.get("Hx-Request"))
        next_url = _safe_next_url(request) or request.META.get("HTTP_REFERER") or reverse("buildings_list")

        if note.category == "mass_assign":
            note.acknowledge()
            if is_hx:
                response = HttpResponse(status=204)
                response["HX-Trigger"] = "notifications:updated"
                return response
            messages.info(request, _("Notification dismissed."))
            return HttpResponseRedirect(next_url)

        note = service.snooze_until(key, target_date=timezone.localdate() + timedelta(days=1))

        if is_hx:
            response = HttpResponse(status=204)
            response["HX-Trigger"] = "notifications:updated"
            return response

        messages.info(
            request,
            _("Notification dismissed until %(date)s.") % {"date": note.snoozed_until.strftime("%Y-%m-%d")},
        )
        return HttpResponseRedirect(next_url)


