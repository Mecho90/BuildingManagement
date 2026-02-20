from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from io import StringIO
from typing import Iterable

from django.conf import settings
from django.http import HttpResponse
from django.utils import formats, timezone
from django.utils.translation import gettext as _

from core.models import BudgetRequest, Notification
from .notifications import NotificationPayload, NotificationService


@dataclass
class BudgetSnapshot:
    identifier: int
    building: str
    requester: str
    status: str
    requested_amount: Decimal
    approved_amount: Decimal
    spent_amount: Decimal
    currency: str
    approved_at: str
    remaining: Decimal


class BudgetExporter:
    def __init__(self, budgets: Iterable[BudgetRequest]):
        self.budgets = list(budgets)

    def _snapshot(self, budget: BudgetRequest) -> BudgetSnapshot:
        approved = budget.approved_total
        remaining = budget.remaining_amount
        approved_at = budget.approved_at.isoformat() if budget.approved_at else ""
        requester = budget.requester.get_full_name() or budget.requester.get_username()
        return BudgetSnapshot(
            identifier=budget.pk,
            building=getattr(budget.building, "name", ""),
            requester=requester,
            status=budget.get_status_display(),
            requested_amount=budget.requested_amount,
            approved_amount=approved,
            spent_amount=budget.spent_total,
            currency=budget.currency,
            approved_at=approved_at,
            remaining=remaining,
        )

    def as_csv_response(self, *, filename: str | None = None) -> HttpResponse:
        filename = filename or f"budgets-{timezone.now().date().isoformat()}.csv"
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                _("Budget ID"),
                _("Building"),
                _("Requester"),
                _("Status"),
                _("Requested"),
                _("Approved"),
                _("Spent"),
                _("Remaining"),
                _("Currency"),
                _("Approved at"),
            ]
        )
        for budget in self.budgets:
            snap = self._snapshot(budget)
            writer.writerow(
                [
                    snap.identifier,
                    snap.building,
                    snap.requester,
                    snap.status,
                    formats.number_format(snap.requested_amount, decimal_pos=2),
                    formats.number_format(snap.approved_amount, decimal_pos=2),
                    formats.number_format(snap.spent_amount, decimal_pos=2),
                    formats.number_format(snap.remaining, decimal_pos=2),
                    snap.currency,
                    snap.approved_at,
                ]
            )
        response = HttpResponse(buffer.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class BudgetNotificationService:
    def __init__(self, budget: BudgetRequest):
        self.budget = budget

    def _threshold(self) -> Decimal:
        raw = getattr(settings, "BUDGET_ALERT_THRESHOLD", 0.9)
        try:
            val = Decimal(str(raw))
        except Exception:
            val = Decimal("0.9")
        return min(max(val, Decimal("0.10")), Decimal("1.00"))

    def check_thresholds(self, *, actor=None):
        total = self.budget.approved_total
        if not total:
            return
        spent = self.budget.spent_total
        threshold = self._threshold()
        if spent < total * threshold:
            return
        key = f"budget:{self.budget.pk}:threshold"
        payload = NotificationPayload(
            key=key,
            category="budgets",
            title=_("Budget %(id)s nearing limit") % {"id": self.budget.pk},
            body=_(
                "%(pct)s%% of the %(total)s %(currency)s cap has been spent for building %(building)s."
            )
            % {
                "pct": formats.number_format(spent / total * 100, decimal_pos=1),
                "total": formats.number_format(total, decimal_pos=2),
                "currency": self.budget.currency,
                "building": getattr(self.budget.building, "name", ""),
            },
            level=Notification.Level.WARNING,
        )
        recipients = {self.budget.requester}
        if self.budget.approved_by:
            recipients.add(self.budget.approved_by)
        for user in recipients:
            try:
                service = NotificationService(user)
                service.upsert(payload)
            except Exception:
                continue

    def notify_depleted(self):
        requester = getattr(self.budget, "requester", None)
        if not requester:
            return
        key = f"budget:{self.budget.pk}:depleted"
        payload = NotificationPayload(
            key=key,
            category="budgets",
            title=_("Budget %(id)s is fully spent") % {"id": self.budget.pk},
            body=_(
                "The budget for %(building)s now has 0 %(currency)s remaining."
            )
            % {
                "building": getattr(self.budget.building, "name", _("Unassigned")),
                "currency": self.budget.currency,
            },
            level=Notification.Level.WARNING,
        )
        try:
            NotificationService(requester).upsert(payload)
        except Exception:
            pass
