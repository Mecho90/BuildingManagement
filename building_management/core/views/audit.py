from __future__ import annotations

import csv
from io import StringIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from ..authz import Capability, CapabilityResolver
from ..models import RoleAuditLog, WorkOrderAuditLog


class AuditTrailView(LoginRequiredMixin, TemplateView):
    template_name = "core/audit_trail.html"

    def dispatch(self, request, *args, **kwargs):
        resolver = CapabilityResolver(request.user)
        if not resolver.has(Capability.VIEW_AUDIT_LOG):
            messages.error(request, _("You do not have access to the audit log."))
            return HttpResponseForbidden()
        self._resolver = resolver
        if request.GET.get("export"):
            return self._export(request.GET.get("export"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        filters = self._filters()
        ctx.update(filters)
        ctx["role_entries"] = self._role_queryset(filters)
        ctx["workorder_entries"] = self._workorder_queryset(filters)
        ctx["role_actions"] = RoleAuditLog.Action.choices
        ctx["workorder_actions"] = WorkOrderAuditLog.Action.choices
        ctx["per"] = filters["per"]
        ctx["per_choices"] = (25, 50, 100, 200)
        return ctx

    # ------------------------------------------------------------------ helpers

    def _filters(self):
        request = self.request
        return {
            "q": (request.GET.get("q") or "").strip(),
            "action": (request.GET.get("action") or "").strip(),
            "kind": (request.GET.get("kind") or "all").strip(),
            "per": self._per_page(),
        }

    def _per_page(self) -> int:
        try:
            per = int(self.request.GET.get("per", 25))
        except (TypeError, ValueError):
            per = 25
        if per not in {25, 50, 100, 200}:
            per = 25
        return per

    def _role_queryset(self, filters):
        qs = RoleAuditLog.objects.select_related("actor", "target_user", "building").order_by("-created_at")
        q = filters["q"]
        if q:
            qs = qs.filter(
                Q(actor__username__icontains=q)
                | Q(target_user__username__icontains=q)
                | Q(role__icontains=q)
            )
        action = filters["action"]
        if action:
            qs = qs.filter(action=action)
        return qs[:filters["per"]]

    def _workorder_queryset(self, filters):
        qs = WorkOrderAuditLog.objects.select_related("actor", "work_order", "building").order_by("-created_at")
        q = filters["q"]
        if q:
            qs = qs.filter(
                Q(actor__username__icontains=q)
                | Q(work_order__title__icontains=q)
                | Q(building__name__icontains=q)
            )
        action = filters["action"]
        if action:
            qs = qs.filter(action=action)
        return qs[:filters["per"]]

    def _export(self, kind: str):
        kind = (kind or "").lower()
        if kind not in {"role", "workorder"}:
            return HttpResponseForbidden()
        filters = self._filters()
        if kind == "role":
            rows = self._role_queryset(filters)
            header = ["timestamp", "actor", "user", "role", "action", "building"]
            data = [
                [
                    timezone.localtime(entry.created_at).isoformat(),
                    getattr(entry.actor, "username", ""),
                    entry.target_user.username,
                    entry.get_role_display(),
                    entry.get_action_display(),
                    getattr(entry.building, "name", ""),
                ]
                for entry in rows
            ]
            filename = "role-audit.csv"
        else:
            rows = self._workorder_queryset(filters)
            header = ["timestamp", "actor", "work_order", "action", "building", "payload"]
            data = [
                [
                    timezone.localtime(entry.created_at).isoformat(),
                    getattr(entry.actor, "username", ""),
                    entry.work_order.title,
                    entry.get_action_display(),
                    getattr(entry.building, "name", ""),
                    entry.payload,
                ]
                for entry in rows
            ]
            filename = "workorder-audit.csv"

        stream = StringIO()
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(data)
        response = HttpResponse(stream.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f"attachment; filename={filename}"
        return response
