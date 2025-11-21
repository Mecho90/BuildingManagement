# core/models.py
from __future__ import annotations

import mimetypes
import uuid
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Count, Q
from django.db.models.functions import Lower
from django.utils.functional import cached_property
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ---------------------------------------------------------------------------
# QuerySets with per-user visibility helpers
# ---------------------------------------------------------------------------

class BuildingQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not user.is_authenticated:
            return self.none()
        if user.is_superuser:
            return self
        from .authz import CapabilityResolver

        resolver = CapabilityResolver(user)
        building_ids = resolver.visible_building_ids()
        if building_ids is None:
            return self
        if not building_ids:
            return self.none()
        return self.filter(id__in=building_ids)

    def with_unit_stats(self):
        """
        Annotate each Building with:
          - _units_count: total units
          - _work_orders_count: ACTIVE work orders (OPEN + IN_PROGRESS), non-archived
        """
        return self.annotate(
            _units_count=Count("units", distinct=True),
            _work_orders_count=Count(
                "work_orders",
                filter=Q(
                    work_orders__archived_at__isnull=True,
                    work_orders__status__in=[
                        WorkOrder.Status.OPEN,
                        WorkOrder.Status.IN_PROGRESS,
                        WorkOrder.Status.AWAITING_APPROVAL,
                    ],
                ),
                distinct=True,
            ),
        )


class UnitQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not user.is_authenticated:
            return self.none()
        if user.is_superuser:
            return self
        from .authz import CapabilityResolver

        resolver = CapabilityResolver(user)
        building_ids = resolver.visible_building_ids()
        if building_ids is None:
            return self
        if not building_ids:
            return self.none()
        return self.filter(building_id__in=building_ids)


class WorkOrderQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not user.is_authenticated:
            return self.none()
        if user.is_superuser:
            return self
        from .authz import CapabilityResolver

        resolver = CapabilityResolver(user)
        building_ids = resolver.visible_building_ids()
        if building_ids is None:
            return self
        if not building_ids:
            return self.none()
        return self.filter(building_id__in=building_ids)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Building(TimeStampedModel):
    class Role(models.TextChoices):
        TECH_SUPPORT = "TECH_SUPPORT", _("Technical Support")
        PROPERTY_MANAGER = "PROPERTY_MANAGER", _("Property Manager")
        EXTERNAL_CONTRACTOR = "EXTERNAL_CONTRACTOR", _("External Contractor")

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="buildings",
        verbose_name=_("Owner"),
    )
    name = models.CharField(max_length=255, db_index=True, verbose_name=_("Name"))
    address = models.CharField(max_length=512, blank=True, verbose_name=_("Address"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.TECH_SUPPORT,
        verbose_name=_("Role"),
    )

    objects = BuildingQuerySet.as_manager()

    class Meta:
        ordering = ["name", "id"]
        verbose_name = _("Building")
        verbose_name_plural = _("Buildings")

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    # ---------------------- Derived counters (for templates) -----------------

    @property
    def units_count(self) -> int:
        """Prefer annotated value (_units_count), fall back to live count."""
        val = getattr(self, "_units_count", None)
        return int(val) if val is not None else self.units.count()

    @property
    def work_orders_count(self) -> int:
        """
        ACTIVE work orders count = OPEN + IN_PROGRESS (non-archived).
        Prefer annotated value (_work_orders_count); fall back to live count.
        """
        val = getattr(self, "_work_orders_count", None)
        if val is not None:
            return int(val)
        return self.work_orders.filter(
            archived_at__isnull=True,
            status__in=[
                WorkOrder.Status.OPEN,
                WorkOrder.Status.IN_PROGRESS,
                WorkOrder.Status.AWAITING_APPROVAL,
            ],
        ).count()


phone_validator = RegexValidator(
    r"^\+?\d{7,15}$",
    _("Enter a valid phone number (digits with optional leading +, 7–15 digits)."),
)


class Unit(TimeStampedModel):
    """
    A single apartment/unit belonging to a Building.

    - `number` is unique per building (case-insensitive)
    - `contact_phone` is stored but deliberately *not* shown in list views
    """
    building = models.ForeignKey(
        Building,
        on_delete=models.CASCADE,
        related_name="units",  # used by counts/annotations
    )

    # Apartment/Unit number (unique within the building)
    number = models.CharField(_("Apartment number"), max_length=32)

    floor = models.IntegerField(null=True, blank=True)

    # The person who owns the apartment
    owner_name = models.CharField(_("Apartment owner"), max_length=255, blank=True)

    # Private contact field (not shown in list)
    contact_phone = models.CharField(
        _("Contact phone"),
        max_length=32,
        blank=True,
        validators=[phone_validator]
    )

    is_occupied = models.BooleanField(default=False)

    # Optional description of the unit
    description = models.TextField(blank=True, verbose_name=_("Description"))

    objects = UnitQuerySet.as_manager()

    class Meta:
        ordering = ["building_id", "number", "id"]
        # Case-insensitive uniqueness within a building
        constraints = [
            models.UniqueConstraint(
                Lower("number"),
                "building",
                name="unique_unit_number_ci_per_building",
                violation_error_message=_("Apartment number must be unique within this building."),
            )
        ]
        # Helpful index for lookups and counts
        indexes = [
            models.Index(fields=["building", "number"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.number} @ {self.building.name}"

    def save(self, *args, **kwargs):
        # light normalization to avoid surprises
        if self.number is not None:
            self.number = self.number.strip()
        if self.contact_phone:
            self.contact_phone = self.contact_phone.strip()
        super().save(*args, **kwargs)


class WorkOrder(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        IN_PROGRESS = "IN_PROGRESS", _("In progress")
        AWAITING_APPROVAL = "AWAITING_APPROVAL", _("Awaiting approval")
        DONE = "DONE", _("Done")

    class Priority(models.TextChoices):
        LOW = "LOW", _("Low")
        MEDIUM = "MEDIUM", _("Medium")
        HIGH = "HIGH", _("High")

    class Kind(models.TextChoices):
        MAINTENANCE = "MAINTENANCE", _("Maintenance")
        MASS_ASSIGN = "MASS_ASSIGN", _("Mass assignment")

    building = models.ForeignKey(
        Building,
        on_delete=models.CASCADE,
        related_name="work_orders",  # used by annotations in views
    )
    unit = models.ForeignKey(
        Unit,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_orders",
    )

    title = models.CharField(max_length=255, verbose_name=_("Title"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    replacement_request_note = models.TextField(blank=True, verbose_name=_("Replacement request note"))

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    priority = models.CharField(
        max_length=10,
        choices=Priority.choices,
        default=Priority.MEDIUM,
        db_index=True,
    )
    kind = models.CharField(
        max_length=32,
        choices=Kind.choices,
        default=Kind.MAINTENANCE,
        db_index=True,
    )
    # Mandatory deadline (non-nullable at DB level)
    deadline = models.DateField(null=False, blank=False)

    mass_assigned = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Mass assigned"),
    )

    # Archiving toggle (when done and user archives)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    awaiting_approval_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_orders_sent_for_approval",
        verbose_name=_("Awaiting approval requested by"),
    )

    objects = WorkOrderQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:  # pragma: no cover
        return self.title

    # ---------------------------- Validation & persistence -------------------

    def clean(self):
        """
        Model-level validation to ensure we never save a WorkOrder
        without a `deadline`.
        """
        super().clean()

        # If a unit is set but building is missing/mismatched, align it
        if self.unit_id and self.building_id and self.unit.building_id != self.building_id:
            raise ValidationError(
                {"unit": _("Selected unit does not belong to the selected building.")}
            )

    def save(self, *args, **kwargs):
        """
        Normalize and ensure relational consistency:
        - Trim the title
        - If a unit is provided, force the building to that unit's building
        - Run `full_clean()` to enforce model validation (deadline required, etc.)
        """
        validate = kwargs.pop("validate", True)
        if self.title:
            self.title = self.title.strip()

        # Align building with unit if unit is present
        if self.unit_id and self.building_id != self.unit.building_id:
            self.building_id = self.unit.building_id

        # Always validate
        if validate:
            self.full_clean()
        return super().save(*args, **kwargs)

    # ---------------------------- Convenience API ---------------------------

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def archive(self):
        """Mark as archived (caller should ensure status is DONE in views/UI)."""
        if not self.is_archived:
            self.archived_at = timezone.now()
            self.save(update_fields=["archived_at"], validate=False)


def work_order_attachment_upload_to(instance, filename: str) -> str:
    """
    Store attachments under per-work-order directories with a UUID filename so
    user uploads cannot collide and paths remain opaque.
    """
    extension = Path(filename).suffix.lower()
    # Keep extension only if it is reasonably short (guards against crafted names)
    if len(extension) > 10:
        extension = ""
    work_order_id = instance.work_order_id or "unassigned"
    return f"work_orders/{work_order_id}/{uuid.uuid4().hex}{extension}"


class WorkOrderAttachment(TimeStampedModel):
    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="attachments",
        verbose_name=_("Work order"),
    )
    file = models.FileField(
        upload_to=work_order_attachment_upload_to,
        verbose_name=_("File"),
        help_text=_("Upload images or documents related to this work order."),
    )
    original_name = models.CharField(
        _("Original filename"),
        max_length=255,
        editable=False,
    )
    content_type = models.CharField(
        _("Content type"),
        max_length=255,
        editable=False,
        blank=True,
    )
    size = models.PositiveBigIntegerField(
        _("Size (bytes)"),
        editable=False,
        default=0,
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Work order attachment")
        verbose_name_plural = _("Work order attachments")

    def __str__(self) -> str:  # pragma: no cover
        return self.original_name or Path(self.file.name).name

    def save(self, *args, **kwargs):
        if self.file:
            name = Path(self.file.name).name
            if not self.original_name:
                self.original_name = name

            detected = getattr(getattr(self.file, "file", None), "content_type", "")
            if not detected:
                detected, _ = mimetypes.guess_type(name)
            if detected:
                self.content_type = detected

            try:
                self.size = int(self.file.size)
            except (TypeError, AttributeError, ValueError):
                self.size = 0

        super().save(*args, **kwargs)


class NotificationQuerySet(models.QuerySet):
    def active(self, *, on: date | None = None):
        today = on or timezone.localdate()
        return self.filter(
            acknowledged_at__isnull=True,
        ).filter(
            models.Q(snoozed_until__isnull=True) | models.Q(snoozed_until__lte=today)
        )


class Notification(TimeStampedModel):
    """
    Persistent notification targeted at a single user. Uniqueness of ``(user, key)``
    prevents duplicate alerts for the same logical event (e.g. deadline for work
    order X).
    """

    class Level(models.TextChoices):
        INFO = "info", _("Info")
        WARNING = "warning", _("Warning")
        DANGER = "danger", _("Danger")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    key = models.CharField(max_length=255)
    category = models.CharField(max_length=40)
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.INFO)
    title = models.CharField(max_length=255)
    body = models.TextField()
    first_seen_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    snoozed_until = models.DateField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    objects = NotificationQuerySet.as_manager()

    class Meta:
        unique_together = ("user", "key")
        indexes = [
            models.Index(fields=("user", "category")),
            models.Index(fields=("user", "acknowledged_at")),
            models.Index(fields=("user", "snoozed_until")),
        ]
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.user}: {self.title}"

    # ------------------------------------------------------------------ helpers

    def acknowledge(self, *, at=None, save: bool = True) -> None:
        """Mark the notification as acknowledged so it no longer appears."""
        at = at or timezone.now()
        if self.acknowledged_at != at:
            self.acknowledged_at = at
        if save:
            self.save(update_fields=["acknowledged_at", "updated_at"])

    def snooze_until(self, target_date: date | None, *, save: bool = True) -> None:
        """
        Hide the notification until ``target_date`` (inclusive). Passing ``None``
        clears any existing snooze window.
        """
        if target_date is not None and target_date < timezone.localdate():
            raise ValidationError(_("Cannot snooze a notification in the past."))
        self.snoozed_until = target_date
        if save:
            self.save(update_fields=["snoozed_until", "updated_at"])

    def mark_seen(self, *, at=None, save: bool = True) -> None:
        """Record that the notification was presented to the user."""
        if self.first_seen_at:
            return
        at = at or timezone.now()
        self.first_seen_at = at
        if save:
            self.save(update_fields=["first_seen_at", "updated_at"])

    def is_active(self, *, on: date | None = None) -> bool:
        """Return True if the notification should be visible."""
        if self.acknowledged_at:
            return False
        today = on or timezone.localdate()
        if self.snoozed_until and self.snoozed_until > today:
            return False
        return True

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        return result


class MembershipRole(models.TextChoices):
    TECHNICIAN = "TECHNICIAN", _("Technician")
    BACKOFFICE = "BACKOFFICE", _("Backoffice Employee")
    ADMINISTRATOR = "ADMINISTRATOR", _("Administrator")
    AUDITOR = "AUDITOR", _("Read-only auditor")


class Capability:
    VIEW_ALL_BUILDINGS = "view_all_buildings"
    MANAGE_BUILDINGS = "manage_buildings"
    CREATE_UNITS = "create_units"
    CREATE_WORK_ORDERS = "create_work_orders"
    MASS_ASSIGN = "mass_assign"
    APPROVE_WORK_ORDERS = "approve_work_orders"
    VIEW_AUDIT_LOG = "view_audit_log"
    MANAGE_MEMBERSHIPS = "manage_memberships"
    VIEW_USERS = "view_users"


ROLE_CAPABILITIES: dict[str, set[str]] = {
    MembershipRole.TECHNICIAN: {
        Capability.MANAGE_BUILDINGS,
        Capability.CREATE_UNITS,
        Capability.CREATE_WORK_ORDERS,
    },
    MembershipRole.BACKOFFICE: {
        Capability.MANAGE_BUILDINGS,
        Capability.CREATE_UNITS,
        Capability.CREATE_WORK_ORDERS,
        Capability.MASS_ASSIGN,
        Capability.APPROVE_WORK_ORDERS,
        Capability.MANAGE_MEMBERSHIPS,
    },
    MembershipRole.ADMINISTRATOR: {
        Capability.VIEW_ALL_BUILDINGS,
        Capability.MANAGE_BUILDINGS,
        Capability.CREATE_UNITS,
        Capability.CREATE_WORK_ORDERS,
        Capability.MASS_ASSIGN,
        Capability.APPROVE_WORK_ORDERS,
        Capability.VIEW_AUDIT_LOG,
        Capability.MANAGE_MEMBERSHIPS,
        Capability.VIEW_USERS,
    },
    MembershipRole.AUDITOR: {
        Capability.VIEW_ALL_BUILDINGS,
        Capability.VIEW_AUDIT_LOG,
    },
}


def _normalize_capability_list(values):
    normalized = []
    seen = set()
    for value in values or []:
        if not value:
            continue
        if value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


class BuildingMembership(TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="memberships",
    )
    role = models.CharField(max_length=32, choices=MembershipRole.choices)
    capabilities_override = models.JSONField(default=dict, blank=True)
    technician_subrole = models.CharField(
        max_length=32,
        choices=Building.Role.choices,
        blank=True,
        verbose_name=_("Technician sub-role"),
    )

    class Meta:
        ordering = ["user_id", "building_id", "role"]
        unique_together = ("user", "building", "role")
        indexes = [
            models.Index(fields=("user", "building")),
        ]
        verbose_name = _("Building membership")
        verbose_name_plural = _("Building memberships")

    def __str__(self) -> str:  # pragma: no cover
        target = self.building.name if self.building else _("All buildings")
        return f"{self.user} → {target} ({self.get_role_display()})"

    @cached_property
    def resolved_capabilities(self) -> set[str]:
        defaults = ROLE_CAPABILITIES.get(self.role, set())
        overrides = self.capabilities_override or {}
        add = set(overrides.get("add", []))
        remove = set(overrides.get("remove", []))
        return (set(defaults) | add) - remove

    def save(self, *args, **kwargs):
        overrides = self.capabilities_override or {}
        overrides["add"] = _normalize_capability_list(overrides.get("add"))
        overrides["remove"] = _normalize_capability_list(overrides.get("remove"))
        self.capabilities_override = overrides
        if self.role != MembershipRole.TECHNICIAN:
            self.technician_subrole = ""
        super().save(*args, **kwargs)

    @property
    def is_global(self) -> bool:
        return self.building_id is None


class RoleAuditLog(TimeStampedModel):
    class Action(models.TextChoices):
        ROLE_ADDED = "role_added", _("Role added")
        ROLE_REMOVED = "role_removed", _("Role removed")
        CAPABILITY_UPDATED = "capability_updated", _("Capabilities updated")

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="role_actions",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="role_audit_entries",
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    role = models.CharField(max_length=32, choices=MembershipRole.choices)
    action = models.CharField(max_length=32, choices=Action.choices)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = _("Role audit log entry")
        verbose_name_plural = _("Role audit log entries")

    def __str__(self) -> str:  # pragma: no cover
        actor = self.actor or _("System")
        return f"{actor} {self.get_action_display()} {self.target_user}"


class WorkOrderAuditLog(TimeStampedModel):
    class Action(models.TextChoices):
        STATUS_CHANGED = "status_changed", _("Status changed")
        APPROVAL = "approval", _("Approval decision" )
        REASSIGNED = "reassigned", _("Reassigned")

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workorder_actions",
    )
    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="audit_entries",
    )
    building = models.ForeignKey(
        Building,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = _("Work order audit log entry")
        verbose_name_plural = _("Work order audit log entries")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.get_action_display()} {self.work_order}"


class UserSecurityProfile(models.Model):
    class LockReason(models.TextChoices):
        FAILED_ATTEMPTS = "failed_attempts", _("Too many failed login attempts")
        MANUAL = "manual", _("Manually locked")

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="security_profile",
    )
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    lock_reason = models.CharField(max_length=32, choices=LockReason.choices, blank=True)

    class Meta:
        verbose_name = _("User security profile")
        verbose_name_plural = _("User security profiles")

    def reset(self, *, commit: bool = True):
        self.failed_login_attempts = 0
        self.locked_at = None
        self.lock_reason = ""
        if commit:
            self.save(update_fields=["failed_login_attempts", "locked_at", "lock_reason"])

    @property
    def is_locked_for_failures(self) -> bool:
        return self.lock_reason == self.LockReason.FAILED_ATTEMPTS
