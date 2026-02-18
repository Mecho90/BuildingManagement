from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import formats, timezone
from django.utils.translation import gettext as _, ngettext

from core.authz import Capability, CapabilityResolver
from core.models import BuildingMembership, MembershipRole, Notification, WorkOrder


@dataclass
class NotificationPayload:
    """
    Serializable representation of a notification to be persisted.

    Required fields match the ``Notification`` model; optional metadata such as
    ``snoozed_until`` may be supplied when relevant.
    """

    key: str
    category: str
    title: str
    body: str
    level: str = Notification.Level.INFO
    snoozed_until: date | None = None
    expires_at: datetime | None = None


class NotificationService:
    """
    Service wrapper responsible for persisting user notifications. By funnelling
    creation through this class we enforce idempotent upserts and centralise any
    additional bookkeeping (e.g. first-seen timestamps).
    """

    def __init__(self, user):
        self.user = user

    # ------------------------------------------------------------------ public

    def upsert(self, payload: NotificationPayload) -> Notification:
        """Create or update a notification for the bound user."""
        defaults = {
            "category": payload.category,
            "title": payload.title,
            "body": payload.body,
            "level": payload.level,
            "snoozed_until": payload.snoozed_until,
            "expires_at": payload.expires_at,
        }
        obj, _created = Notification.objects.update_or_create(
            user=self.user,
            key=payload.key,
            defaults=defaults,
        )
        return obj

    def bulk_upsert(self, payloads: Iterable[NotificationPayload]) -> list[Notification]:
        """Persist a batch of notifications and return the resulting objects."""
        payloads = list(payloads)
        if not payloads:
            return []

        keys = [payload.key for payload in payloads]
        now = timezone.now()

        with transaction.atomic():
            existing = {
                note.key: note
                for note in Notification.objects.select_for_update().filter(user=self.user, key__in=keys)
            }

            to_create: list[Notification] = []
            to_update: list[Notification] = []

            for payload in payloads:
                defaults = {
                    "category": payload.category,
                    "title": payload.title,
                    "body": payload.body,
                    "level": payload.level,
                    "snoozed_until": payload.snoozed_until,
                    "expires_at": payload.expires_at,
                }
                note = existing.get(payload.key)
                if note is None:
                    note = Notification(user=self.user, key=payload.key, **defaults)
                    to_create.append(note)
                    existing[payload.key] = note
                else:
                    for field, value in defaults.items():
                        setattr(note, field, value)
                    note.updated_at = now
                    to_update.append(note)

            if to_create:
                Notification.objects.bulk_create(to_create)

            if to_update:
                Notification.objects.bulk_update(
                    to_update,
                    ["category", "title", "body", "level", "snoozed_until", "expires_at", "updated_at"],
                )

        refreshed = Notification.objects.filter(user=self.user, key__in=keys)
        refreshed_map = {note.key: note for note in refreshed}
        return [refreshed_map[payload.key] for payload in payloads]

    def acknowledge(self, keys: Sequence[str]) -> int:
        """Acknowledge notifications identified by ``keys``."""
        now = timezone.now()
        return (
            Notification.objects.filter(user=self.user, key__in=keys)
            .exclude(acknowledged_at__isnull=False)
            .update(acknowledged_at=now, updated_at=now)
        )

    def snooze_until(self, key: str, *, target_date: date | None) -> Notification:
        """Set ``snoozed_until`` for a single notification."""
        obj = Notification.objects.get(user=self.user, key=key)
        obj.snooze_until(target_date)
        return obj

    def delete(self, keys: Sequence[str]) -> int:
        """Delete notifications by key."""
        return Notification.objects.filter(user=self.user, key__in=keys).delete()[0]

    def mark_seen(self, keys: Sequence[str]) -> int:
        """Update ``first_seen_at`` for notifications that have just been shown."""
        now = timezone.now()
        return (
            Notification.objects.filter(user=self.user, key__in=keys, first_seen_at__isnull=True)
            .update(first_seen_at=now, updated_at=now)
        )

    def active(self, *, on: date | None = None) -> Iterable[Notification]:
        """Return active notifications as of ``on`` (defaults to today)."""
        return Notification.objects.filter(user=self.user).active(on=on)

    # ---------------------------------------------------------------- deadline

    def sync_work_order_deadlines(self, *, today: date | None = None) -> Iterable[Notification]:
        """
        Upsert notifications for upcoming work order deadlines.

        - Generates/updates one notification per active work order.
        - Respects any future snooze window set by the user.
        - Deletes stale notifications for work orders that are completed/archived.
        """

        today = today or timezone.localdate()
        user = self.user
        if not user.is_authenticated:
            return []

        is_admin = user.is_staff or user.is_superuser

        thresholds = {
            WorkOrder.Priority.HIGH: 7,
            WorkOrder.Priority.MEDIUM: 7,
            WorkOrder.Priority.LOW: 30,
        }

        max_window = max(thresholds.values())
        priority_levels = {
            WorkOrder.Priority.HIGH: Notification.Level.DANGER,
            WorkOrder.Priority.MEDIUM: Notification.Level.WARNING,
            WorkOrder.Priority.LOW: Notification.Level.INFO,
        }

        priority_window = Case(
            When(priority=WorkOrder.Priority.HIGH, then=Value(thresholds[WorkOrder.Priority.HIGH])),
            When(priority=WorkOrder.Priority.MEDIUM, then=Value(thresholds[WorkOrder.Priority.MEDIUM])),
            When(priority=WorkOrder.Priority.LOW, then=Value(thresholds[WorkOrder.Priority.LOW])),
            default=Value(max_window),
            output_field=IntegerField(),
        )

        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(
                archived_at__isnull=True,
                status__in=[
                    WorkOrder.Status.OPEN,
                    WorkOrder.Status.IN_PROGRESS,
                    WorkOrder.Status.AWAITING_APPROVAL,
                ],
                deadline__gte=today,
                deadline__lte=today + timedelta(days=max_window),
            )
            .annotate(priority_window=priority_window)
            .select_related("building__owner", "unit", "forwarded_to_building")
            .order_by("deadline", "-pk")
        )

        per_priority_counts = {key: 0 for key in thresholds}
        existing = {
            note.key: note
            for note in Notification.objects.filter(user=user, category="deadline")
        }
        keep_keys: set[str] = set()

        for wo in qs:
            if wo.priority not in thresholds:
                continue

            window = thresholds.get(wo.priority, max_window)
            days_left = (wo.deadline - today).days
            if days_left > window or per_priority_counts[wo.priority] >= 10:
                continue

            per_priority_counts[wo.priority] += 1

            building = wo.building
            building_name = building.name if wo.building_id else _("your building")
            if wo.forwarded_to_building_id:
                target = getattr(wo.forwarded_to_building, "name", _("target building"))
                building_name = _("%(origin)s → %(target)s") % {
                    "origin": building_name,
                    "target": target,
                }
            owner_label = None
            if is_admin and wo.building_id:
                owner = getattr(building, "owner", None)
                if owner:
                    owner_label = owner.get_full_name() or owner.username

            if days_left == 0:
                due_text = _("due today")
            elif days_left == 1:
                due_text = _("due tomorrow")
            else:
                due_text = ngettext(
                    "due in %(count)s day",
                    "due in %(count)s days",
                    days_left,
                ) % {"count": days_left}

            deadline_display = formats.date_format(wo.deadline, "DATE_FORMAT")
            message = _(
                '%(priority)s priority work order "%(title)s" in %(building)s is %(due)s '
                "(deadline %(deadline)s)") % {
                "priority": wo.get_priority_display(),
                "title": wo.title,
                "building": building_name,
                "due": due_text,
                "deadline": deadline_display,
            }
            if owner_label:
                message += _(" (owner: %(owner)s)") % {"owner": owner_label}
            message += "."

            key = f"wo-deadline-{wo.pk}"
            keep_keys.add(key)
            level = priority_levels.get(wo.priority, Notification.Level.INFO)

            existing_note = existing.get(key)
            if existing_note:
                fields: list[str] = []
                if existing_note.category != "deadline":
                    existing_note.category = "deadline"
                    fields.append("category")
                if existing_note.title != wo.title:
                    existing_note.title = wo.title
                    fields.append("title")
                if existing_note.body != message:
                    existing_note.body = message
                    fields.append("body")
                if existing_note.level != level:
                    existing_note.level = level
                    fields.append("level")
                if existing_note.snoozed_until is None or existing_note.snoozed_until <= today:
                    if existing_note.snoozed_until != today:
                        existing_note.snoozed_until = today
                        fields.append("snoozed_until")
                if fields:
                    existing_note.save(update_fields=[*fields, "updated_at"])
            else:
                Notification.objects.create(
                    user=user,
                    key=key,
                    category="deadline",
                    level=level,
                    title=wo.title,
                    body=message,
                    snoozed_until=today,
                )

        stale_qs = Notification.objects.filter(user=user, category="deadline")
        if keep_keys:
            stale_qs = stale_qs.exclude(key__in=keep_keys)
        stale_qs.delete()

        return Notification.objects.filter(user=user, category="deadline").active(on=today)

    def prune_acknowledged(self, *, older_than_days: int = 30) -> int:
        """Delete acknowledged notifications older than the provided age."""
        cutoff = timezone.now() - timedelta(days=older_than_days)
        deleted, _ = Notification.objects.filter(
            user=self.user,
            acknowledged_at__isnull=False,
            acknowledged_at__lt=cutoff,
        ).delete()
        return deleted

    def sync_recent_mass_assign(self, *, today: date | None = None) -> list[Notification]:
        today = today or timezone.localdate()
        user = self.user
        window = timezone.now() - timedelta(days=7)

        qs = (
            WorkOrder.objects.visible_to(user)
            .filter(mass_assigned=True, created_at__gte=window)
            .select_related("building__owner")
            .order_by("-created_at")[:10]
        )

        existing = {
            note.key: note
            for note in Notification.objects.filter(user=user, category="mass_assign")
        }
        keep_keys: set[str] = set()
        for order in qs:
            key = f"wo-mass-{order.pk}"
            keep_keys.add(key)
            existing_note = existing.get(key)
            if existing_note and existing_note.acknowledged_at:
                continue

            building = order.building
            building_name = building.name if order.building_id else _("your building")
            message = _(
                'A new mass-assigned work order "%(title)s" was created for %(building)s (deadline %(deadline)s).'
            ) % {
                "title": order.title,
                "building": building_name,
                "deadline": formats.date_format(order.deadline, "DATE_FORMAT"),
            }

            Notification.objects.update_or_create(
                user=user,
                key=key,
                defaults={
                    "category": "mass_assign",
                    "level": Notification.Level.INFO,
                    "title": order.title,
                    "body": message,
                },
            )

        if keep_keys:
            Notification.objects.filter(user=user, category="mass_assign").exclude(key__in=keep_keys).delete()
        else:
            Notification.objects.filter(user=user, category="mass_assign").delete()

        return list(
            Notification.objects.filter(user=user, category="mass_assign").active(on=today)
        )


def _notify_owner_and_backoffice(building, *, key: str, title: str, body: str, notified: set[int] | None = None) -> set[int]:
    """
    Helper to fan out notifications to a building owner and its backoffice members.
    Returns the set of user IDs that were notified so callers can avoid duplicates.
    """
    if not building:
        return set()
    notified_ids: set[int] = set(notified or [])
    owner = getattr(building, "owner", None)
    if owner and owner.is_active and owner.pk not in notified_ids:
        NotificationService(owner).upsert(
            NotificationPayload(
                key=key,
                category="forwarding",
                title=title,
                body=body,
                level=Notification.Level.INFO,
            )
        )
        notified_ids.add(owner.pk)

    backoffice_members = BuildingMembership.objects.filter(
        building=building,
        role=MembershipRole.BACKOFFICE,
    ).select_related("user")
    for membership in backoffice_members:
        user = membership.user
        if not user or not user.is_active or user.pk in notified_ids:
            continue
        NotificationService(user).upsert(
            NotificationPayload(
                key=key,
                category="forwarding",
                title=title,
                body=body,
                level=Notification.Level.INFO,
            )
        )
        notified_ids.add(user.pk)
    return notified_ids


def _notify_building_technicians(
    building,
    *,
    key: str,
    title: str,
    body: str,
    exclude_user_ids: set[int] | None = None,
    category: str = "forwarding",
) -> set[int]:
    """Reusable helper that alerts technicians assigned to ``building``."""
    if not building:
        return set()
    exclude = set(exclude_user_ids or [])
    notified: set[int] = set()
    technicians = BuildingMembership.objects.filter(
        building=building,
        role=MembershipRole.TECHNICIAN,
    ).select_related("user")
    for membership in technicians:
        user = membership.user
        if not user or not user.is_active:
            continue
        if user.pk in exclude or user.pk in notified:
            continue
        NotificationService(user).upsert(
            NotificationPayload(
                key=key,
                category=category,
                title=title,
                body=body,
                level=Notification.Level.INFO,
            )
        )
        notified.add(user.pk)
    return notified


def notify_approvers_of_pending_order(order: WorkOrder, *, exclude_user_id: int | None = None) -> None:
    """Send notifications to users who can approve work orders for the given building."""
    if not order.building_id:
        return

    memberships = BuildingMembership.objects.filter(
        Q(building=order.building) | Q(building__isnull=True)
    ).select_related("user")

    recipients: dict[int, object] = {}
    for membership in memberships:
        user = membership.user
        if not user or not user.is_active:
            continue
        if exclude_user_id and user.id == exclude_user_id:
            continue
        resolver = CapabilityResolver(user)
        if resolver.has(Capability.APPROVE_WORK_ORDERS, building_id=order.building_id):
            recipients[user.id] = user

    if not recipients:
        return

    building_name = getattr(order.building, "name", _("their building"))
    if order.forwarded_to_building_id:
        destination = getattr(order.forwarded_to_building, "name", _("target building"))
        building_name = _("%(origin)s → %(target)s") % {
            "origin": building_name,
            "target": destination,
        }
    note = (order.replacement_request_note or "").strip()
    note_section = ""
    if note:
        note_section = "\n\n" + _("Technician request:") + f" {note}"
    forwarding_note = (order.forward_note or "").strip()
    if forwarding_note:
        note_section += "\n\n" + _("Forwarding note:") + f" {forwarding_note}"

    body = _(
        'Work order "%(title)s" in %(building)s is awaiting approval.'
    ) % {
        "title": order.title,
        "building": building_name,
    }
    body += note_section

    for user in recipients.values():
        NotificationService(user).upsert(
            NotificationPayload(
                key=f"wo-awaiting-{order.pk}",
                category="approval",
                title=order.title,
                body=body,
                level=Notification.Level.WARNING,
            )
        )


def notify_building_technicians_of_mass_assignment(
    order: WorkOrder,
    *,
    building=None,
    exclude_user_ids: set[int] | None = None,
) -> None:
    building = building or order.building
    if not building:
        return
    body = _(
        'New work order "%(title)s" was created for %(building)s with deadline %(deadline)s.'
    ) % {
        "title": order.title,
        "building": getattr(building, "name", _("your building")),
        "deadline": formats.date_format(order.deadline, "DATE_FORMAT"),
    }
    _notify_building_technicians(
        building,
        key=f"wo-mass-{order.pk}",
        title=order.title,
        body=body,
        category="mass_assign",
        exclude_user_ids=exclude_user_ids,
    )


def notify_forwarded_work_order(order: WorkOrder, *, actor=None) -> None:
    target = getattr(order, "forwarded_to_building", None)
    if not target or not target.pk:
        return
    origin = order.building
    origin_label = getattr(origin, "name", _("Office"))
    destination_label = getattr(target, "name", _("destination building"))
    actor_label = ""
    if actor and getattr(actor, "is_authenticated", False):
        actor_label = actor.get_full_name() or actor.username or ""
    note = (order.forward_note or "").strip()
    note_section = ""
    if note:
        note_section = "\n\n" + _("Forwarding note:") + f" {note}"
    by_clause = ""
    if actor_label:
        by_clause = " " + _("by %(actor)s") % {"actor": actor_label}
    body = _(
        'Work order "%(title)s" was forwarded from %(origin)s to %(destination)s%(by)s.'
    ) % {
        "title": order.title,
        "origin": origin_label,
        "destination": destination_label,
        "by": by_clause,
    }
    body += note_section
    title = _("Forwarded to %(destination)s") % {"destination": destination_label}
    notified = _notify_owner_and_backoffice(
        target,
        key=f"wo-forward-{order.pk}-{target.pk}",
        title=title,
        body=body,
    )

    notify_building_technicians_of_mass_assignment(
        order,
        building=target,
        exclude_user_ids=notified,
    )

    if origin and getattr(origin, "is_system_default", False):
        _notify_owner_and_backoffice(
            origin,
            key=f"wo-forward-office-{order.pk}",
            title=_('Forwarded "%(title)s" to %(destination)s') % {
                "title": order.title,
                "destination": destination_label,
            },
            body=body,
        )


def notify_forwarding_reset(order: WorkOrder, *, previous_building=None, actor=None) -> None:
    origin = getattr(order, "building", None)
    if not origin or not getattr(origin, "is_system_default", False):
        return
    origin_label = getattr(origin, "name", _("Office"))
    destination_label = (
        getattr(previous_building, "name", _("the previous destination"))
        if previous_building
        else _("the previous destination")
    )
    actor_label = ""
    if actor and getattr(actor, "is_authenticated", False):
        actor_label = actor.get_full_name() or actor.username or ""
    by_clause = ""
    if actor_label:
        by_clause = " " + _("by %(actor)s") % {"actor": actor_label}
    body = _(
        'Work order "%(title)s" was returned to %(origin)s from %(destination)s%(by)s.'
    ) % {
        "title": order.title,
        "origin": origin_label,
        "destination": destination_label,
        "by": by_clause,
    }
    note = (order.forward_note or "").strip()
    if note:
        body += "\n\n" + _("Forwarding note:") + f" {note}"

    base_key = f"wo-forward-reset-{order.pk}"
    office_title = _("Returned to %(origin)s") % {"origin": origin_label}
    _notify_owner_and_backoffice(
        origin,
        key=f"{base_key}-office",
        title=office_title,
        body=body,
    )

    if previous_building and previous_building.pk:
        previous_title = _("Removed from %(destination)s") % {"destination": destination_label}
        _notify_owner_and_backoffice(
            previous_building,
            key=f"{base_key}-{previous_building.pk}",
            title=previous_title,
            body=body,
        )
        _notify_building_technicians(
            previous_building,
            key=f"{base_key}-{previous_building.pk}-tech",
            title=previous_title,
            body=body,
        )
