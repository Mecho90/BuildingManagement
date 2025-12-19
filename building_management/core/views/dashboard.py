from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Q
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _, ngettext
from django.views.generic import TemplateView

from ..authz import Capability, CapabilityResolver
from ..models import Building, MembershipRole, Notification, WorkOrder, WorkOrderAuditLog
from ..utils.roles import user_can_approve_work_orders, user_is_lawyer
from ..services import NotificationService
from .common import _querystring_without


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "core/dashboard.html"
    DEADLINE_WINDOWS = {
        WorkOrder.Priority.HIGH: 5,
        WorkOrder.Priority.MEDIUM: 3,
        WorkOrder.Priority.LOW: 1,
    }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        resolver = CapabilityResolver(user)
        self._is_lawyer = user_is_lawyer(user)
        staff_role = self._has_global_staff_role(user)
        self._lawyer_scope_only = self._is_lawyer and not staff_role

        ctx.setdefault("dashboard_label", self._label_for(resolver))
        tech_page, tech_query = self._technician_cards(user, resolver)
        ctx["technician_cards_page"] = tech_page
        ctx["technician_cards"] = tech_page.object_list if tech_page else []
        ctx["technician_page_query"] = tech_query
        ctx["technician_section_title"] = self._technician_section_title(user)
        ctx["backoffice_cards"] = self._backoffice_cards(user, resolver)
        deadline_page, deadline_query = self._deadline_alert_cards(user)
        ctx["deadline_alert_page"] = deadline_page
        ctx["deadline_alert_cards"] = deadline_page.object_list if deadline_page else []
        ctx["deadline_page_query"] = deadline_query
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
        show_all_today = self._has_global_staff_role(user) or self._is_lawyer
        filter_kwargs = {}
        visible_statuses = self._statuses_for_today(user)
        if not show_all_today:
            assigned_ids = self._assigned_building_ids(user)
            if not assigned_ids:
                empty_page = Paginator([], 1).get_page(1)
                return empty_page, query
            filter_kwargs["building_id__in"] = assigned_ids

        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(
                archived_at__isnull=True,
                status__in=visible_statuses,
                deadline=today,
                **filter_kwargs,
            )
            .select_related("building")
            .order_by("deadline", "priority", "-id")
        )
        qs = self._restrict_queryset_to_lawyer(qs, user)

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
        if not user or not user.is_authenticated:
            return []
        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(
                archived_at__isnull=True,
                status=WorkOrder.Status.AWAITING_APPROVAL,
            )
            .select_related("building", "awaiting_approval_by")
            .order_by("-updated_at")
        )
        qs = self._restrict_queryset_to_lawyer(qs, user)[:8]
        cards = []
        for wo in qs:
            requester = None
            if wo.awaiting_approval_by:
                requester = wo.awaiting_approval_by.get_full_name() or wo.awaiting_approval_by.username
            can_take_action = user_can_approve_work_orders(user, getattr(wo.building, "pk", None))
            cards.append(
                {
                    "id": wo.pk,
                    "title": wo.title,
                    "building": getattr(wo.building, "name", "-"),
                    "deadline": wo.deadline,
                    "note": wo.replacement_request_note,
                    "awaiting_since": wo.updated_at.strftime("%Y-%m-%d %H:%M"),
                    "requested_by": requester,
                    "can_take_action": can_take_action,
                }
            )

        return cards

    def _deadline_alert_cards(self, user):
        query = _querystring_without(self.request, "deadline_page")
        if not user or not user.is_authenticated:
            empty_page = Paginator([], 1).get_page(1)
            return empty_page, query
        today = timezone.localdate()
        windows = self.DEADLINE_WINDOWS
        active_statuses = [
            WorkOrder.Status.OPEN,
            WorkOrder.Status.IN_PROGRESS,
            WorkOrder.Status.AWAITING_APPROVAL,
        ]
        attention_filter = Q(deadline__lt=today)
        for priority, window_days in windows.items():
            attention_filter |= Q(
                priority=priority,
                deadline__gte=today,
                deadline__lte=today + timedelta(days=window_days),
            )

        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(
                archived_at__isnull=True,
                status__in=active_statuses,
            )
            .filter(attention_filter)
            .select_related("building")
            .order_by("deadline", "-priority")
        )
        qs = self._restrict_queryset_to_lawyer(qs, user)
        priority_order = {
            WorkOrder.Priority.HIGH: 0,
            WorkOrder.Priority.MEDIUM: 1,
            WorkOrder.Priority.LOW: 2,
        }
        cards = []
        for wo in qs:
            days_delta = (wo.deadline - today).days
            is_overdue = days_delta < 0
            if is_overdue:
                overdue_days = abs(days_delta)
                if overdue_days == 1:
                    timing_text = _("просрочено с 1 ден")
                else:
                    timing_text = ngettext(
                        "просрочено с %(count)s ден",
                        "просрочено с %(count)s дни",
                        overdue_days,
                    ) % {"count": overdue_days}
                reason = _("Пропуснат краен срок")
            else:
                if days_delta == 0:
                    timing_text = _("Deadline is today")
                    reason = _("Deadline is today")
                elif days_delta == 1:
                    timing_text = _("срокът е утре")
                    priority_label = wo.get_priority_display()
                    reason = _("%(priority)s задача с наближаващ срок") % {"priority": priority_label}
                else:
                    timing_text = ngettext(
                        "срок след %(count)s ден",
                        "срок след %(count)s дни",
                        days_delta,
                    ) % {"count": days_delta}
                    priority_label = wo.get_priority_display()
                    reason = _("%(priority)s задача с наближаващ срок") % {"priority": priority_label}

            cards.append(
                {
                    "id": wo.pk,
                    "title": wo.title,
                    "building": getattr(wo.building, "name", "-"),
                    "priority": wo.priority,
                    "priority_label": wo.get_priority_display(),
                    "status_label": wo.get_status_display(),
                    "deadline": wo.deadline,
                    "deadline_display": formats.date_format(wo.deadline, "DATE_FORMAT"),
                    "timing_text": timing_text,
                    "reason": reason,
                    "is_overdue": is_overdue,
                }
            )

        cards.sort(
            key=lambda item: (
                0 if item["is_overdue"] else 1,
                item["deadline"],
                priority_order.get(item["priority"], 99),
            )
        )
        paginator = Paginator(cards, 4)
        try:
            page_number = int(self.request.GET.get("deadline_page", 1))
        except (TypeError, ValueError):
            page_number = 1
        page_obj = paginator.get_page(page_number)
        return page_obj, query

    def _assignment_load(self, user):
        today = timezone.localdate()
        statuses = self._statuses_for_today(user)
        qs = WorkOrder.objects.visible_to(user).filter(
            archived_at__isnull=True,
            status__in=statuses,
            deadline=today,
        )
        qs = self._restrict_queryset_to_lawyer(qs, user)
        if getattr(self, "_lawyer_scope_only", False):
            return qs.count()
        building_ids = self._assigned_building_ids(user)
        if not building_ids:
            return 0
        return qs.filter(building_id__in=building_ids).count()

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

    # ------------------------------------------------------------------ roles & labels

    def _global_roles(self, user):
        if not user or not user.is_authenticated:
            return set()
        if not hasattr(self, "_global_roles_cache"):
            roles = set(
                user.memberships.filter(building__isnull=True).values_list("role", flat=True)
            )
            self._global_roles_cache = roles
        return self._global_roles_cache

    def _has_global_staff_role(self, user):
        roles = self._global_roles(user)
        return bool(roles & {MembershipRole.BACKOFFICE, MembershipRole.ADMINISTRATOR})

    def _technician_section_title(self, user):
        if self._has_global_staff_role(user):
            return _("Днешните отворени задачи")
        return _("Днешните ми задачи")

    def _statuses_for_today(self, user):
        return [
            code
            for code in (choice[0] for choice in WorkOrder.Status.choices)
            if code != WorkOrder.Status.AWAITING_APPROVAL
        ]

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
                        "_priority_weight": 0,
                    }
                )
            )

        recent_activity_notes = self._work_order_activity_notifications(user)
        for note in recent_activity_notes:
            notifications.append(attach_styles(note))

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

    def _restrict_queryset_to_lawyer(self, queryset, user):
        if not getattr(self, "_lawyer_scope_only", False):
            return queryset
        return queryset.filter(lawyer_only=True)
    def _work_order_activity_notifications(self, user):
        if not user.is_authenticated:
            return []

        resolver = CapabilityResolver(user)
        visible_buildings = resolver.visible_building_ids()
        if visible_buildings == set():
            return []

        now = timezone.now()
        window_start = now - timedelta(days=7)
        recent_threshold = now - timedelta(hours=12)
        qs = (
            WorkOrderAuditLog.objects.select_related("work_order", "actor", "building")
            .filter(created_at__gte=window_start)
            .exclude(actor=user)
        )
        if visible_buildings is not None:
            qs = qs.filter(building_id__in=list(visible_buildings))
        logs = list(qs.order_by("-created_at")[:15])
        dismissed_ids = self._dismissed_activity_ids()

        notifications = []
        for log in logs:
            if log.pk in dismissed_ids:
                continue
            message, level = self._format_activity_message(log)
            if not message:
                continue
            notifications.append(
                {
                    "id": f"wo-activity-{log.pk}",
                    "level": level,
                    "level_label": _("Информация"),
                    "message": message,
                    "category": "activity",
                    "is_new": log.created_at >= recent_threshold,
                    "dismissible": True,
                    "_priority_weight": 1,
                }
            )
        return notifications

    def _dismissed_activity_ids(self):
        store = self.request.session.get("dismissed_activity_logs", [])
        try:
            return {int(val) for val in store}
        except (TypeError, ValueError):
            return set()

    def _format_activity_message(self, log):
        order = getattr(log, "work_order", None)
        if order is None:
            return None, Notification.Level.INFO.value
        actor = getattr(log, "actor", None)
        actor_name = actor.get_full_name() or actor.username if actor else _("System")
        building_name = getattr(log.building, "name", _("their building"))
        title = order.title
        payload = log.payload or {}

        level = Notification.Level.INFO.value
        message = None
        if log.action == WorkOrderAuditLog.Action.CREATED:
            message = _('%(actor)s създаде "%(title)s" за %(building)s.') % {
                "actor": actor_name,
                "title": title,
                "building": building_name,
            }
        elif log.action == WorkOrderAuditLog.Action.STATUS_CHANGED:
            status_labels = dict(WorkOrder.Status.choices)
            from_label = status_labels.get(payload.get("from"), payload.get("from"))
            to_label = status_labels.get(payload.get("to"), payload.get("to"))
            message = _('%(actor)s промени статуса на "%(title)s" от %(from)s на %(to)s.') % {
                "actor": actor_name,
                "title": title,
                "from": from_label,
                "to": to_label,
            }
            level = Notification.Level.WARNING.value
        elif log.action == WorkOrderAuditLog.Action.APPROVAL:
            status_labels = dict(WorkOrder.Status.choices)
            to_label = status_labels.get(payload.get("to"), payload.get("to"))
            message = _('%(actor)s записа решение за одобрение за "%(title)s" (%(status)s).') % {
                "actor": actor_name,
                "title": title,
                "status": to_label or _("updated status"),
            }
            level = Notification.Level.WARNING.value
        elif log.action == WorkOrderAuditLog.Action.UPDATED:
            fields = payload.get("fields", {})
            field_label_map = {
                "deadline": _("краен срок"),
                "description": _("описание"),
                "priority": _("приоритет"),
                "replacement_request_note": _("бележка за замяна"),
                "title": _("заглавие"),
                "unit": _("апартамент"),
            }
            translated_fields = []
            for field in sorted(fields.keys()):
                translated_fields.append(field_label_map.get(field, field))
            field_names = ", ".join(translated_fields) or _("детайли")
            message = _('%(actor)s актуализира %(fields)s за "%(title)s".') % {
                "actor": actor_name,
                "fields": field_names,
                "title": title,
            }
        elif log.action == WorkOrderAuditLog.Action.ATTACHMENTS:
            message = _('%(actor)s актуализира файловете към "%(title)s".') % {
                "actor": actor_name,
                "title": title,
            }
        elif log.action == WorkOrderAuditLog.Action.REASSIGNED:
            message = _('%(actor)s пренасочи "%(title)s".') % {
                "actor": actor_name,
                "title": title,
            }
            level = Notification.Level.WARNING.value
        elif log.action == WorkOrderAuditLog.Action.ARCHIVED:
            message = _('%(actor)s архивира "%(title)s".') % {
                "actor": actor_name,
                "title": title,
            }

        return message, level

    def _label_for(self, resolver):
        if resolver.has(Capability.APPROVE_WORK_ORDERS):
            return ""
        if resolver.has(Capability.CREATE_WORK_ORDERS) and not resolver.has(Capability.MANAGE_BUILDINGS):
            return "Technician overview"
        return ""
