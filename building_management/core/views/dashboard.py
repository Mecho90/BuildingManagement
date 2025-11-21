from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Q
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from ..authz import Capability, CapabilityResolver
from ..models import Building, MembershipRole, Notification, UserSecurityProfile, WorkOrder
from ..services import NotificationService
from .common import _querystring_without


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "core/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        resolver = CapabilityResolver(user)

        ctx.setdefault("dashboard_label", self._label_for(resolver))
        tech_page, tech_query = self._technician_cards(user, resolver)
        ctx["technician_cards_page"] = tech_page
        ctx["technician_cards"] = tech_page.object_list if tech_page else []
        ctx["technician_page_query"] = tech_query
        ctx["backoffice_cards"] = self._backoffice_cards(user, resolver)
        ctx["assignment_load"] = self._assignment_load(user)
        ctx.update(self._notifications_context())
        return ctx

    # ------------------------------------------------------------------ helpers

    def _visible_building_filter(self, resolver: CapabilityResolver):
        ids = resolver.visible_building_ids()
        if ids is None:
            return {}
        if not ids:
            return {"pk__in": []}
        return {"pk__in": list(ids)}

    def _technician_cards(self, user, resolver):
        query = _querystring_without(self.request, "jobs_page")
        if not resolver.has(Capability.CREATE_WORK_ORDERS):
            empty_page = Paginator([], 1).get_page(1)
            return empty_page, query

        today = timezone.localdate()
        assigned_ids = self._assigned_building_ids(user)
        filters = Q(
            building_id__in=assigned_ids or [],
            status__in=[WorkOrder.Status.OPEN, WorkOrder.Status.IN_PROGRESS],
            deadline=today,
        )

        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(archived_at__isnull=True)
            .filter(filters)
            .select_related("building")
            .order_by("deadline", "priority", "-id")
        )

        cards = [
            {
                "id": wo.pk,
                "title": wo.title,
                "building": getattr(wo.building, "name", "-"),
                "priority": wo.priority,
                "priority_label": wo.get_priority_display(),
                "status_label": wo.get_status_display(),
                "deadline": wo.deadline,
                "description": wo.description,
                "can_update": True,
            }
            for wo in qs
        ]

        paginator = Paginator(cards, 4)
        try:
            page_number = int(self.request.GET.get("jobs_page", 1))
        except (TypeError, ValueError):
            page_number = 1
        page_obj = paginator.get_page(page_number)
        return page_obj, query

    def _assigned_building_ids(self, user):
        if not user or not user.is_authenticated:
            return []
        membership_ids = list(
            user.memberships.filter(building__isnull=False).values_list("building_id", flat=True)
        )
        owned_ids = list(
            Building.objects.filter(owner=user).values_list("pk", flat=True)
        )
        return list({*membership_ids, *owned_ids})

    def _backoffice_cards(self, user, resolver):
        if not resolver.has(Capability.MANAGE_BUILDINGS):
            return []
        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(
                archived_at__isnull=True,
                status=WorkOrder.Status.AWAITING_APPROVAL,
            )
            .select_related("building", "awaiting_approval_by")
            .order_by("-updated_at")[:8]
        )
        cards = []
        for wo in qs:
            requester = None
            if wo.awaiting_approval_by:
                requester = wo.awaiting_approval_by.get_full_name() or wo.awaiting_approval_by.username
            cards.append(
                {
                    "id": wo.pk,
                    "title": wo.title,
                    "building": getattr(wo.building, "name", "-"),
                    "deadline": wo.deadline,
                    "note": wo.replacement_request_note,
                    "awaiting_since": wo.updated_at.strftime("%Y-%m-%d %H:%M"),
                    "requested_by": requester,
                }
            )

        return cards

    def _assignment_load(self, user):
        building_ids = self._assigned_building_ids(user)
        if not building_ids:
            return 0
        today = timezone.localdate()
        return (
            WorkOrder.objects.visible_to(user)
            .filter(
                archived_at__isnull=True,
                building_id__in=building_ids,
                status__in=[WorkOrder.Status.OPEN, WorkOrder.Status.IN_PROGRESS],
                deadline=today,
            )
            .count()
        )

    def _notifications_context(self):
        notifications_list = self._build_notifications()
        note_paginator = Paginator(notifications_list, 5)
        try:
            note_page_number = int(self.request.GET.get("note_page", 1))
        except (TypeError, ValueError):
            note_page_number = 1
        notifications_page = note_paginator.get_page(note_page_number)
        return {
            "notifications_page": notifications_page,
            "notifications": notifications_page.object_list,
            "note_page_query": _querystring_without(self.request, "note_page"),
        }

    def _build_notifications(self):
        user = self.request.user
        if not user.is_authenticated:
            return []

        cache_key = f"dashboard:notifications:{user.pk}"
        cached_notifications = cache.get(cache_key)
        if cached_notifications is not None:
            return [note.copy() for note in cached_notifications]

        notifications: list[dict[str, str | bool]] = []

        service = NotificationService(user)
        deadline_notifications = list(service.sync_work_order_deadlines())
        new_flags = {note.key: note.first_seen_at is None for note in deadline_notifications}
        if deadline_notifications:
            service.mark_seen([note.key for note in deadline_notifications])

        request_language = getattr(self.request, "LANGUAGE_CODE", translation.get_language())
        with translation.override(request_language):
            level_labels = {
                Notification.Level.INFO.value: _("Info"),
                Notification.Level.WARNING.value: _("Warning"),
                Notification.Level.DANGER.value: _("Danger"),
            }

        level_weights = {
            Notification.Level.DANGER.value: 0,
            Notification.Level.WARNING.value: 1,
            Notification.Level.INFO.value: 2,
        }

        level_styles = {
            Notification.Level.DANGER.value: {
                "card": "border-rose-200 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-900/40 dark:text-rose-100",
                "badge": "bg-rose-100 text-rose-700 dark:bg-rose-900/60 dark:text-rose-200",
                "card_style": "background-color:#fee2e2;border-color:#fecaca;color:#7f1d1d;",
                "badge_style": "background-color:#fecaca;color:#7f1d1d;",
            },
            Notification.Level.WARNING.value: {
                "card": "border-amber-200 bg-amber-100 text-amber-900 dark:border-amber-700 dark:bg-amber-900/40 dark:text-amber-100",
                "badge": "bg-amber-100 text-amber-700 dark:bg-amber-900/60 dark:text-amber-200",
                "card_style": "background-color:#fef9c3;border-color:#fde68a;color:#78350f;",
                "badge_style": "background-color:#fde68a;color:#78350f;",
            },
            Notification.Level.INFO.value: {
                "card": "border-emerald-200 bg-emerald-100 text-emerald-900 dark:border-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-100",
                "badge": "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/60 dark:text-emerald-200",
                "card_style": "background-color:#ecfdf5;border-color:#a7f3d0;color:#065f46;",
                "badge_style": "background-color:#a7f3d0;color:#065f46;",
            },
        }
        default_style = level_styles[Notification.Level.INFO.value]

        def attach_styles(payload):
            level = payload.get("level", Notification.Level.INFO.value)
            style = level_styles.get(level, default_style)
            payload["card_classes"] = style["card"]
            payload["badge_classes"] = style["badge"]
            # inline fallbacks ensure consistent colours even if Tailwind
            # purges unused classes before a rebuild.
            payload["card_style"] = style.get("card_style", "")
            payload["badge_style"] = style.get("badge_style", "")
            return payload

        for note in deadline_notifications:
            notifications.append(
                attach_styles(
                    {
                        "id": note.key,
                        "level": note.level,
                        "level_label": level_labels.get(note.level, note.get_level_display()),
                        "message": note.body,
                        "category": note.category,
                        "is_new": new_flags.get(note.key, False),
                        "dismissible": True,
                        "_priority_weight": level_weights.get(note.level, 99),
                    }
                )
            )

        mass_notifications = service.sync_recent_mass_assign()
        if mass_notifications:
            service.mark_seen([note.key for note in mass_notifications])
        for note in mass_notifications:
            notifications.append(
                attach_styles(
                    {
                        "id": note.key,
                        "level": note.level,
                        "level_label": level_labels.get(note.level, note.get_level_display()),
                        "message": note.body,
                        "category": note.category,
                        "is_new": False,
                        "dismissible": True,
                        "_priority_weight": 3,
                    }
                )
            )

        is_admin = user.is_staff or user.is_superuser

        if is_admin:
            locked_accounts = (
                UserSecurityProfile.objects.select_related("user")
                .filter(
                    user__is_active=False,
                    lock_reason=UserSecurityProfile.LockReason.FAILED_ATTEMPTS,
                )
                .order_by("-locked_at")[:20]
            )

            for profile in locked_accounts:
                locked_at = profile.locked_at
                locked_display = (
                    formats.date_format(timezone.localtime(locked_at), "DATETIME_FORMAT")
                    if locked_at
                    else _("an unknown time")
                )
                locked_user = profile.user
                display_name = locked_user.get_full_name() or locked_user.username
                message = _(
                    "%(user)s was locked after too many failed login attempts on %(locked)s. Reactivate the account and set a new password to restore access."
                ) % {"user": display_name, "locked": locked_display}
                notifications.append(
                    attach_styles(
                        {
                            "id": f"user-locked-{locked_user.pk}",
                            "level": Notification.Level.DANGER.value,
                            "level_label": level_labels.get(Notification.Level.DANGER.value, Notification.Level.DANGER.label),
                            "message": message,
                            "category": "account_lock",
                            "is_new": False,
                            "dismissible": False,
                        }
                    )
                )

        notifications.sort(key=lambda item: (item.get("_priority_weight", 99), item.get("id")))
        for item in notifications:
            item.pop("_priority_weight", None)
            item.setdefault("is_new", False)
            item.setdefault("dismissible", False)

        cache.set(
            cache_key,
            tuple(note.copy() for note in notifications),
            timeout=60,
        )
        return notifications

    def _label_for(self, resolver):
        if resolver.has(Capability.APPROVE_WORK_ORDERS):
            return ""
        if resolver.has(Capability.CREATE_WORK_ORDERS) and not resolver.has(Capability.MANAGE_BUILDINGS):
            return "Technician overview"
        if resolver.has(Capability.VIEW_AUDIT_LOG):
            return "Auditor overview"
        return ""
