# core/models.py
from __future__ import annotations

import hashlib
import mimetypes
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
import time
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Count, Q, Sum
from django.db.models.functions import Lower
from django.utils.functional import cached_property
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def start_of_week(value: date | datetime | None = None) -> date:
    """Return Monday of the week for the provided date (localized)."""
    if value is None:
        base_date = timezone.localdate()
    elif isinstance(value, datetime):
        base_date = timezone.localtime(value).date()
    else:
        base_date = value
    offset = base_date.weekday()  # Monday == 0
    return base_date - timedelta(days=offset)


def default_currency() -> str:
    return getattr(settings, "DEFAULT_CURRENCY", "USD")


# ---------------------------------------------------------------------------
# QuerySets with per-user visibility helpers
# ---------------------------------------------------------------------------

def _resolve_visibility_scope(user):
    """
    Returns a tuple of (scope, resolver, ids) where scope can be:
    - "none": unauthenticated users
    - "all": unrestricted access (superuser or VIEW_ALL buildings)
    - "subset": filter to provided building ids
    """
    if not user or not getattr(user, "is_authenticated", False):
        return "none", None, set()
    if getattr(user, "is_superuser", False):
        return "all", None, set()
    from .authz import CapabilityResolver  # avoid circular import

    resolver = CapabilityResolver(user)
    building_ids = resolver.visible_building_ids()
    if building_ids is None:
        return "all", resolver, set()
    return "subset", resolver, set(building_ids or [])


class BuildingQuerySet(models.QuerySet):
    def visible_to(self, user):
        scope, _, building_ids = _resolve_visibility_scope(user)
        if scope == "none":
            return self.none()
        if scope == "all":
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

    def with_lawyer_alerts(self):
        """
        Annotate each Building with the number of active lawyer-only work orders.
        """
        return self.annotate(
            _lawyer_orders_count=Count(
                "work_orders",
                filter=Q(
                    work_orders__lawyer_only=True,
                    work_orders__archived_at__isnull=True,
                ),
                distinct=True,
            )
        )


class UnitQuerySet(models.QuerySet):
    def visible_to(self, user):
        scope, _, building_ids = _resolve_visibility_scope(user)
        try:
            office_id = Building.system_default_id()
        except Exception:
            office_id = None
        qs = self
        if office_id:
            qs = qs.exclude(building_id=office_id)
            building_ids.discard(office_id)
        if scope == "none":
            return qs.none()
        if scope == "all":
            return qs
        if not building_ids:
            return qs.none()
        return qs.filter(building_id__in=building_ids)

    def with_lawyer_alerts(self):
        """Annotate units with their active lawyer-only work-order counts."""
        return self.annotate(
            _lawyer_orders_count=Count(
                "work_orders",
                filter=Q(
                    work_orders__lawyer_only=True,
                    work_orders__archived_at__isnull=True,
                ),
                distinct=True,
            )
        )


class WorkOrderQuerySet(models.QuerySet):
    def visible_to(self, user):
        scope, resolver, building_ids = _resolve_visibility_scope(user)
        if scope == "none":
            return self.none()
        if getattr(user, "is_superuser", False):
            return self
        if resolver is None:
            from .authz import CapabilityResolver  # avoid circular import

            resolver = CapabilityResolver(user)
        qs = self
        if scope == "subset":
            if not building_ids:
                return self.none()
            qs = qs.filter(
                Q(building_id__in=building_ids)
                | Q(forwarded_to_building_id__in=building_ids)
            )
        can_view_confidential = resolver.has(Capability.VIEW_CONFIDENTIAL_WORK_ORDERS)
        if not can_view_confidential and _user_can_view_confidential_orders(user):
            can_view_confidential = True
        if not can_view_confidential:
            qs = qs.filter(lawyer_only=False)
        return qs


ROLE_TECHNICIAN = "TECHNICIAN"
ROLE_BACKOFFICE = "BACKOFFICE"
ROLE_ADMINISTRATOR = "ADMINISTRATOR"


class MembershipRole(models.TextChoices):
    TECHNICIAN = "TECHNICIAN", _("Technician")
    BACKOFFICE = "BACKOFFICE", _("Backoffice Employee")
    LAWYER = "LAWYER", _("Lawyer")
    ADMINISTRATOR = "ADMINISTRATOR", _("Administrator")


class TodoListQuerySet(models.QuerySet):
    def for_user(self, user):
        if not user or not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(user=user)

    def for_week(self, week_start_date: date | None):
        if week_start_date is None:
            return self
        return self.filter(week_start=week_start_date)


class TodoItemQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not getattr(user, "is_authenticated", False):
            return self.none()
        if getattr(user, "is_superuser", False):
            return self

        Membership = apps.get_model("core", "BuildingMembership")
        roles = set(Membership.objects.filter(user=user).values_list("role", flat=True))
        if ROLE_ADMINISTRATOR in roles:
            return self

        base_filter = models.Q(user=user)
        if ROLE_BACKOFFICE in roles:
            technician_ids = (
                Membership.objects.filter(role=ROLE_TECHNICIAN)
                .values_list("user_id", flat=True)
            )
            base_filter |= models.Q(user_id__in=technician_ids)
        return self.filter(base_filter)

    def for_week(self, week_start_date: date | None):
        if week_start_date is None:
            return self
        return self.filter(week_start=week_start_date)

    def with_history(self):
        return self.prefetch_related("activities")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Building(TimeStampedModel):
    _system_default_id_cache: int | None = None
    _system_default_cache_loaded = False
    _system_default_cache_key = "core:system_default_building_id"
    _system_default_cache_timestamp: float | None = None

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
    is_system_default = models.BooleanField(
        default=False,
        db_index=True,
        help_text=_("Marks the singleton Office building that remains unit-less."),
    )

    objects = BuildingQuerySet.as_manager()

    class Meta:
        ordering = ["name", "id"]
        verbose_name = _("Building")
        verbose_name_plural = _("Buildings")
        constraints = [
            models.UniqueConstraint(
                fields=("is_system_default",),
                condition=Q(is_system_default=True),
                name="unique_system_default_building",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    @classmethod
    def _system_default_cache_timeout(cls) -> int | None:
        raw = getattr(settings, "SYSTEM_DEFAULT_BUILDING_CACHE_TIMEOUT", 300)
        if raw is None:
            return None
        try:
            timeout = int(raw)
        except (TypeError, ValueError):
            return 300
        return max(timeout, 0)

    @classmethod
    def get_system_default(cls) -> "Building | None":
        pk = cls.system_default_id()
        if not pk:
            return None
        return cls.objects.filter(pk=pk).first()

    @classmethod
    def system_default_id(cls, *, force_refresh: bool = False) -> int | None:
        now = time.monotonic()
        ttl = cls._system_default_cache_timeout()
        if (
            cls._system_default_cache_loaded
            and not force_refresh
            and ttl
            and cls._system_default_cache_timestamp is not None
            and now - cls._system_default_cache_timestamp >= ttl
        ):
            force_refresh = True

        if cls._system_default_cache_loaded and not force_refresh:
            return cls._system_default_id_cache

        cache_key = cls._system_default_cache_key
        sentinel = object()
        if not force_refresh:
            cached_value = cache.get(cache_key, sentinel)
            if cached_value is not sentinel:
                cls._system_default_id_cache = cached_value
                cls._system_default_cache_loaded = True
                cls._system_default_cache_timestamp = now
                return cached_value

        cls._system_default_id_cache = (
            cls.objects.filter(is_system_default=True)
            .values_list("id", flat=True)
            .first()
        )
        cls._system_default_cache_loaded = True
        cls._system_default_cache_timestamp = now
        cache_timeout = ttl or None
        cache.set(cache_key, cls._system_default_id_cache, cache_timeout)
        return cls._system_default_id_cache

    @classmethod
    def clear_system_default_cache(cls) -> None:
        cls._system_default_id_cache = None
        cls._system_default_cache_loaded = False
        cls._system_default_cache_timestamp = None
        cache.delete(cls._system_default_cache_key)

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

    @property
    def lawyer_orders_count(self) -> int:
        """Total active lawyer-only work orders associated with this building."""
        val = getattr(self, "_lawyer_orders_count", None)
        if val is not None:
            return int(val)
        return self.work_orders.filter(lawyer_only=True, archived_at__isnull=True).count()


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

    @property
    def lawyer_orders_count(self) -> int:
        """Active lawyer-only work orders associated with this unit."""
        val = getattr(self, "_lawyer_orders_count", None)
        if val is not None:
            return int(val)
        return self.work_orders.filter(lawyer_only=True, archived_at__isnull=True).count()

    def clean(self):
        super().clean()
        building = self.building if isinstance(self.building, Building) else None
        is_system_default = getattr(building, "is_system_default", None)
        if is_system_default is None and self.building_id:
            is_system_default = Building.objects.filter(
                pk=self.building_id,
                is_system_default=True,
            ).exists()
        if is_system_default:
            raise ValidationError({"building": _("Units cannot be added to the Office building.")})

    def save(self, *args, **kwargs):
        validate = kwargs.pop("validate", True)
        # light normalization to avoid surprises
        if self.number is not None:
            self.number = self.number.strip()
        if self.contact_phone:
            self.contact_phone = self.contact_phone.strip()
        if validate:
            self.full_clean()
        super().save(*args, **kwargs)


class WorkOrder(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", _("Open")
        IN_PROGRESS = "IN_PROGRESS", _("In progress")
        AWAITING_APPROVAL = "AWAITING_APPROVAL", _("Очаква одобрение от бекофиса")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")
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
        verbose_name=_("Building"),
    )
    unit = models.ForeignKey(
        Unit,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_orders",
        verbose_name=_("Unit"),
    )

    title = models.CharField(max_length=255, verbose_name=_("Title"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    replacement_request_note = models.TextField(blank=True, verbose_name=_("Replacement request note"))
    forward_note = models.TextField(blank=True, verbose_name=_("Forwarding note"))

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
        verbose_name=_("Priority"),
    )
    kind = models.CharField(
        max_length=32,
        choices=Kind.choices,
        default=Kind.MAINTENANCE,
        db_index=True,
    )
    # Mandatory deadline (non-nullable at DB level)
    deadline = models.DateField(null=False, blank=False, verbose_name=_("Deadline"))

    mass_assigned = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Mass assigned"),
    )

    # Archiving toggle (when done and user archives)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)
    forwarded_to_building = models.ForeignKey(
        Building,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="forwarded_work_orders",
        verbose_name=_("Forwarded to building"),
    )
    forwarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_orders_forwarded",
        verbose_name=_("Forwarded by"),
    )
    forwarded_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Forwarded at"))
    awaiting_approval_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_orders_sent_for_approval",
        verbose_name=_("Awaiting approval requested by"),
    )
    lawyer_only = models.BooleanField(
        _("Lawyer-only visibility"),
        default=False,
        db_index=True,
        help_text=_("Only lawyers, backoffice employees, and administrators can view these work orders."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_orders_created",
        verbose_name=_("Created by"),
    )

    objects = WorkOrderQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=("forwarded_to_building", "archived_at"),
                name="core_wo_forwarded_archived_idx",
            ),
        ]

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
        if self.forwarded_to_building_id:
            origin = self.building
            if not origin or not origin.is_system_default:
                raise ValidationError(
                    {"forwarded_to_building": _("Only Office work orders can be forwarded.")}
                )
            if self.forwarded_to_building_id == self.building_id:
                raise ValidationError(
                    {"forwarded_to_building": _("Forwarded building must differ from the origin.")}
                )
            if not self.forwarded_at:
                self.forwarded_at = timezone.now()
        else:
            if self.forwarded_by_id or self.forwarded_at or self.forward_note:
                raise ValidationError(
                    {"forwarded_to_building": _("Forwarded metadata requires a destination building.")}
                )

    def save(self, *args, **kwargs):
        """
        Normalize and ensure relational consistency:
        - Trim the title
        - If a unit is provided, force the building to that unit's building
        - Run `full_clean()` to enforce model validation (deadline required, etc.)
        """
        validate = kwargs.pop("validate", True)
        previous_forward_target = None
        if self.pk:
            previous_forward_target = (
                WorkOrder.objects.filter(pk=self.pk)
                .values_list("forwarded_to_building_id", flat=True)
                .first()
            )
        if self.title:
            self.title = self.title.strip()

        # Older data may include outdated `kind` values (e.g. REACTIVE); coerce them.
        valid_kinds = {choice[0] for choice in self.Kind.choices}
        if not self.kind or self.kind not in valid_kinds:
            self.kind = self.Kind.MAINTENANCE

        # Align building with unit if unit is present
        if self.unit_id and self.building_id != self.unit.building_id:
            self.building_id = self.unit.building_id

        # Always validate
        if validate:
            self.full_clean()
        result = super().save(*args, **kwargs)
        self._maybe_log_forwarding(previous_forward_target)
        return result

    # ---------------------------- Convenience API ---------------------------

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def archive(self):
        """Mark as archived (caller should ensure status is DONE in views/UI)."""
        if not self.is_archived:
            self.archived_at = timezone.now()
            self.save(update_fields=["archived_at"], validate=False)

    def _maybe_log_forwarding(self, previous_target_id: int | None):
        new_target_id = self.forwarded_to_building_id
        if not new_target_id or new_target_id == previous_target_id:
            return
        forwarded_at = self.forwarded_at or timezone.now()
        if not self.forwarded_at:
            WorkOrder.objects.filter(pk=self.pk).update(forwarded_at=forwarded_at)
            self.forwarded_at = forwarded_at
        from_building_id = previous_target_id or self.building_id
        if not from_building_id:
            return
        WorkOrderForwarding.objects.create(
            work_order=self,
            from_building_id=from_building_id,
            to_building_id=new_target_id,
            forwarded_by=self.forwarded_by,
            forwarded_at=forwarded_at,
            note=self.forward_note or "",
        )


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


class WorkOrderForwarding(TimeStampedModel):
    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="forwarding_history",
    )
    from_building = models.ForeignKey(
        Building,
        on_delete=models.CASCADE,
        related_name="forwarding_origins",
    )
    to_building = models.ForeignKey(
        Building,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="forwarding_targets",
    )
    forwarded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="forwarding_events",
    )
    forwarded_at = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ("-forwarded_at", "-id")
        indexes = [
            models.Index(
                fields=("to_building", "forwarded_at"),
                name="core_forwarding_target_idx",
            ),
        ]
        verbose_name = _("Work order forwarding event")
        verbose_name_plural = _("Work order forwarding events")

    def __str__(self):  # pragma: no cover
        from_name = getattr(self.from_building, "name", "—")
        to_name = getattr(self.to_building, "name", "—")
        return f"{self.work_order} :: {from_name} → {to_name}"


def _budget_attachment_upload_to(instance, filename: str) -> str:
    expense_id = instance.expense_id or "pending"
    extension = Path(filename).suffix.lower()
    if len(extension) > 10:
        extension = ""
    return f"budgets/{expense_id}/{uuid.uuid4().hex}{extension}"


class ExpenseCategory(TimeStampedModel):
    code = models.CharField(max_length=40, unique=True)
    label = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    requires_receipt = models.BooleanField(default=True)
    max_amount_per_day = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    mileage_rate = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("label", "code")
        verbose_name = _("Expense category")
        verbose_name_plural = _("Expense categories")

    def __str__(self):  # pragma: no cover
        return self.label


class BudgetFeatureFlag(TimeStampedModel):
    key = models.CharField(max_length=64, default="budgets")
    building = models.ForeignKey(
        "core.Building",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="budget_feature_flags",
    )
    role = models.CharField(max_length=32, choices=MembershipRole.choices, blank=True)
    is_enabled = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ("key", "building", "role")
        verbose_name = _("Budget feature flag")
        verbose_name_plural = _("Budget feature flags")

    def __str__(self):  # pragma: no cover
        target = self.building or _("All buildings")
        role = self.role or _("All roles")
        return f"{self.key} → {target} ({role})"

    @classmethod
    def is_enabled_for(cls, user, *, key: str = "budgets", building_id: int | None = None) -> bool:
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        qs = cls.objects.filter(key=key, is_enabled=True)
        if not qs.exists():
            return True
        if building_id:
            qs = qs.filter(Q(building_id=building_id) | Q(building__isnull=True))
        if qs.filter(role="").exists():
            return True
        user_roles = set(
            BuildingMembership.objects.filter(user=user)
            .values_list("role", flat=True)
        )
        return qs.filter(role__in=user_roles).exists()


_EXPENSE_ACCUMULATION_STATUSES = {"logged", "approved"}


class BudgetRequestQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not getattr(user, "is_authenticated", False):
            return self.none()
        if getattr(user, "is_superuser", False):
            return self
        scope, resolver, building_ids = _resolve_visibility_scope(user)
        own_q = Q(requester=user)
        if scope == "none":
            return self.filter(own_q)
        if scope == "all":
            return self
        if not building_ids:
            return self.filter(own_q)
        return self.filter(own_q | Q(building_id__in=building_ids))

    def pending_review(self):
        return self.filter(status=BudgetRequest.Status.PENDING_REVIEW)

    def active(self):
        return self.filter(archived_at__isnull=True)

    def archived(self):
        return self.filter(archived_at__isnull=False)

    def with_totals(self):
        return self.annotate(
            _spent_total=Sum(
                "expenses__amount",
                filter=Q(expenses__status__in=list(_EXPENSE_ACCUMULATION_STATUSES)),
            ),
            _attachment_count=Count("expenses__attachments", distinct=True),
        )


class BudgetRequest(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PENDING_REVIEW = "pending_review", _("Pending review")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        CLOSED = "closed", _("Closed")

    class Currency(models.TextChoices):
        USD = "USD", _("USD")
        EUR = "EUR", _("EUR")
        BGN = "BGN", _("BGN")

    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="budget_requests",
    )
    title = models.CharField(max_length=255, blank=True)
    building = models.ForeignKey(
        Building,
        on_delete=models.CASCADE,
        related_name="budget_requests",
        null=True,
        blank=True,
    )
    project_code = models.CharField(max_length=120, blank=True)
    description = models.TextField(blank=True)
    requested_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    approved_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    spent_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    currency = models.CharField(
        max_length=8,
        choices=Currency.choices,
        default=Currency.EUR,
    )
    allow_overage = models.BooleanField(default=False)
    allow_post_close_expense = models.BooleanField(default=False)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="budget_requests_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    feature_flag = models.CharField(max_length=64, default="budgets", blank=True)
    notes = models.TextField(blank=True)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = BudgetRequestQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("building", "status")),
            models.Index(fields=("requester", "status")),
            models.Index(fields=("archived_at", "requester")),
        ]
        verbose_name = _("Budget request")
        verbose_name_plural = _("Budget requests")

    def __str__(self):  # pragma: no cover
        label = (self.title or "").strip()
        if not label:
            label = _("Budget #%(id)s") % {"id": self.pk or "?"}
        return f"{label} ({self.requested_amount} {self.currency})"

    @property
    def approved_total(self) -> Decimal:
        if self.approved_amount is not None:
            return self.approved_amount
        return self.requested_amount

    @property
    def spent_total(self) -> Decimal:
        annotated = getattr(self, "_spent_total", None)
        if annotated is not None:
            return Decimal(annotated or 0)
        return self.spent_amount

    @property
    def remaining_amount(self) -> Decimal:
        return max(self.approved_total - self.spent_total, Decimal("0.00"))

    @property
    def has_overage(self) -> bool:
        return self.spent_total > self.approved_total

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def clean(self):
        super().clean()
        if self.approved_amount is not None and self.approved_amount < Decimal("0.00"):
            raise ValidationError({"approved_amount": _("Approved amount cannot be negative.")})
        if self.requested_amount < Decimal("0.00"):
            raise ValidationError({"requested_amount": _("Requested amount cannot be negative.")})

    def update_spent_amount(self):
        total = (
            self.expenses.filter(status__in=list(_EXPENSE_ACCUMULATION_STATUSES))
            .aggregate(total=Sum("amount"))
            .get("total")
            or Decimal("0.00")
        )
        self.spent_amount = total
        self.save(update_fields=["spent_amount", "updated_at"])

    def log_event(self, *, actor, event_type: str, notes: str = "", payload=None):
        payload = payload or {}
        return BudgetRequestEvent.objects.create(
            budget_request=self,
            actor=actor,
            event_type=event_type,
            notes=notes,
            payload=payload,
        )

    def transition(self, *, status: str, actor=None, comment: str = "", payload=None):
        if status not in dict(self.Status.choices):
            raise ValidationError({"status": _("Unknown status.")})
        previous = self.status
        if previous == status and not comment:
            return
        self.status = status
        updates = ["status", "updated_at"]
        if status == self.Status.APPROVED:
            self.approved_at = timezone.now()
            if actor and not self.approved_by_id:
                self.approved_by = actor
            if not self.approved_amount:
                self.approved_amount = self.requested_amount
            updates.append("approved_at")
            updates.append("approved_by")
            updates.append("approved_amount")
        self.save(update_fields=updates)
        self.log_event(
            actor=actor,
            event_type=BudgetRequestEvent.EventType.STATUS,
            notes=comment,
            payload={"from": previous, "to": status, **(payload or {})},
        )

    def archive(self, *, actor=None, comment: str | None = None):
        if self.archived_at:
            return
        comment = comment or _("Budget archived.")
        if self.status != self.Status.CLOSED:
            self.transition(status=self.Status.CLOSED, actor=actor, comment=comment)
        else:
            self.log_event(
                actor=actor,
                event_type=BudgetRequestEvent.EventType.STATUS,
                notes=comment,
                payload={"from": self.Status.CLOSED, "to": self.Status.CLOSED, "archived": True},
            )
        self.archived_at = timezone.now()
        self.save(update_fields=["archived_at", "updated_at"])


class ExpenseQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(budget_request__in=BudgetRequest.objects.visible_to(user))


class Expense(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        LOGGED = "logged", _("Logged")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    budget_request = models.ForeignKey(
        BudgetRequest,
        on_delete=models.CASCADE,
        related_name="expenses",
    )
    expense_type = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
    )
    label = models.CharField(max_length=255)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses_created",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses_approved",
    )
    incurred_on = models.DateField(default=timezone.localdate)

    objects = ExpenseQuerySet.as_manager()

    class Meta:
        ordering = ("-incurred_on", "-id")
        indexes = [
            models.Index(fields=("budget_request", "status")),
        ]
        verbose_name = _("Expense")
        verbose_name_plural = _("Expenses")

    def __str__(self):  # pragma: no cover
        return f"{self.label} ({self.amount})"

    @property
    def requires_attachment(self) -> bool:
        if self.expense_type:
            return self.expense_type.requires_receipt
        return False

    def clean(self):
        super().clean()
        if self.amount <= Decimal("0.00"):
            raise ValidationError({"amount": _("Expense amount must be positive.")})
        if self.status in _EXPENSE_ACCUMULATION_STATUSES:
            remaining = self.budget_request.remaining_amount
            previous_amount, previous_status = self._previous_amount_snapshot()
            if previous_amount is not None and previous_status in _EXPENSE_ACCUMULATION_STATUSES:
                remaining += Decimal(previous_amount)
            if self.amount > remaining:
                raise ValidationError(
                    {"amount": _("Expense exceeds remaining budget.")},
                )
        elif self.status == self.Status.REJECTED:
            return
        if self.expense_type and self.expense_type.max_amount_per_day:
            if self.amount > self.expense_type.max_amount_per_day:
                raise ValidationError(
                    {"amount": _("Expense amount exceeds the configured daily maximum for this category.")},
                )

    def _countable_amount(self, *, amount: Decimal | None = None, status: str | None = None) -> Decimal:
        amount = amount if amount is not None else self.amount
        status = status if status is not None else self.status
        if status in _EXPENSE_ACCUMULATION_STATUSES:
            return Decimal(amount or 0)
        return Decimal("0.00")

    def _previous_amount_snapshot(self) -> tuple[Decimal, str] | tuple[None, None]:
        if not self.pk:
            return None, None
        previous = (
            Expense.objects.filter(pk=self.pk)
            .values_list("amount", "status", named=False)
            .first()
        )
        if not previous:
            return None, None
        return Decimal(previous[0]), str(previous[1])

    def save(self, *args, **kwargs):
        validate = kwargs.pop("validate", True)
        previous_amount, previous_status = (None, None)
        if self.pk:
            try:
                previous_amount, previous_status = self._previous_amount_snapshot()
            except ValidationError:
                previous_amount, previous_status = (None, None)
        previous_remaining = max(
            Decimal(self.budget_request.approved_total) - Decimal(self.budget_request.spent_amount or 0),
            Decimal("0.00"),
        )
        if validate:
            self.full_clean()
        is_create = self.pk is None
        super().save(*args, **kwargs)
        previous_contribution = (
            self._countable_amount(amount=previous_amount, status=previous_status)
            if previous_amount is not None
            else Decimal("0.00")
        )
        new_contribution = self._countable_amount()
        delta = new_contribution - previous_contribution
        if delta:
            self.budget_request.spent_amount = max(
                Decimal("0.00"), self.budget_request.spent_amount + delta
            )
            self.budget_request.save(update_fields=["spent_amount", "updated_at"])
            try:
                from .services.budgets import BudgetNotificationService

                BudgetNotificationService(self.budget_request).check_thresholds(actor=self.created_by)
                new_remaining = max(
                    Decimal(self.budget_request.approved_total) - Decimal(self.budget_request.spent_amount or 0),
                    Decimal("0.00"),
                )
                if previous_remaining > Decimal("0.00") and new_remaining == Decimal("0.00"):
                    BudgetNotificationService(self.budget_request).notify_depleted()
            except Exception:
                pass
        payload = {
            "expense_id": self.pk,
            "label": self.label,
            "amount": str(self.amount),
            "status": self.status,
            "update": not is_create,
        }
        log_context = getattr(self, "_log_context", None)
        if isinstance(log_context, dict):
            payload.update({k: v for k, v in log_context.items() if v not in (None, "")})
        self.budget_request.log_event(
            actor=self.created_by,
            event_type=BudgetRequestEvent.EventType.EXPENSE_LOGGED,
            notes=self.notes or "",
            payload=payload,
        )

    def delete(self, *args, **kwargs):
        amount = self._countable_amount()
        result = super().delete(*args, **kwargs)
        if amount:
            self.budget_request.spent_amount = max(
                Decimal("0.00"), self.budget_request.spent_amount - amount
            )
            self.budget_request.save(update_fields=["spent_amount", "updated_at"])
        return result


class ExpenseAttachment(TimeStampedModel):
    expense = models.ForeignKey(
        Expense,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to=_budget_attachment_upload_to)
    original_name = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    checksum = models.CharField(max_length=64, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expense_attachments",
    )

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = _("Expense attachment")
        verbose_name_plural = _("Expense attachments")

    def __str__(self):  # pragma: no cover
        return self.original_name or Path(self.file.name).name

    def save(self, *args, **kwargs):
        if self.file:
            path = Path(self.file.name)
            if not self.original_name:
                self.original_name = path.name
            detected = getattr(getattr(self.file, "file", None), "content_type", "")
            if not detected:
                detected, _ = mimetypes.guess_type(path.name)
            if detected:
                self.mime_type = detected
            try:
                self.size = int(self.file.size)
            except (TypeError, AttributeError, ValueError):
                self.size = 0
            self.checksum = self._compute_checksum()
        super().save(*args, **kwargs)
        self.expense.budget_request.log_event(
            actor=self.uploaded_by,
            event_type=BudgetRequestEvent.EventType.ATTACHMENT,
            notes=self.original_name or "",
            payload={"attachment_id": self.pk, "expense_id": self.expense_id},
        )

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        self.expense.budget_request.log_event(
            actor=self.uploaded_by,
            event_type=BudgetRequestEvent.EventType.ATTACHMENT,
            notes=_("Attachment removed"),
            payload={"attachment_id": self.pk, "expense_id": self.expense_id, "removed": True},
        )
        return result

    def _compute_checksum(self) -> str:
        hasher = hashlib.sha256()
        for chunk in self.file.chunks():
            hasher.update(chunk)
        return hasher.hexdigest()


class BudgetRequestEvent(TimeStampedModel):
    class EventType(models.TextChoices):
        STATUS = "status", _("Status changed")
        APPROVAL = "approval", _("Approval decision")
        EXPENSE_LOGGED = "expense", _("Expense recorded")
        ATTACHMENT = "attachment", _("Attachment activity")
        COMMENT = "comment", _("Comment")

    budget_request = models.ForeignKey(
        BudgetRequest,
        on_delete=models.CASCADE,
        related_name="events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="budget_events",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    notes = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = _("Budget event")
        verbose_name_plural = _("Budget events")

    def __str__(self):  # pragma: no cover
        return f"{self.get_event_type_display()} – {self.budget_request_id}"


class TodoList(TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="todo_lists",
    )
    week_start = models.DateField(db_index=True)
    title = models.CharField(max_length=255, blank=True)

    objects = TodoListQuerySet.as_manager()

    class Meta:
        ordering = ("-week_start", "-id")
        unique_together = ("user", "week_start")
        indexes = [
            models.Index(fields=("user", "week_start")),
        ]
        verbose_name = _("To-do list")
        verbose_name_plural = _("To-do lists")

    def save(self, *args, **kwargs):
        self.week_start = start_of_week(self.week_start)
        if not self.title:
            week_label = self.week_start.strftime("%Y-%m-%d")
            self.title = _("Week of %(week)s") % {"week": week_label}
        super().save(*args, **kwargs)


class TodoItem(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        IN_PROGRESS = "in_progress", _("In progress")
        DONE = "done", _("Done")
        ARCHIVED = "archived", _("Archived")

    todo_list = models.ForeignKey(
        TodoList,
        on_delete=models.CASCADE,
        related_name="items",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="todo_items",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    week_start = models.DateField(db_index=True)

    objects = TodoItemQuerySet.as_manager()

    class Meta:
        ordering = ("due_date", "pk")
        indexes = [
            models.Index(fields=("user", "week_start")),
            models.Index(fields=("user", "status")),
        ]
        verbose_name = _("To-do item")
        verbose_name_plural = _("To-do items")

    def _resolve_week_start(self) -> date:
        reference = self.week_start or self.due_date or timezone.localdate()
        return start_of_week(reference)

    def _ensure_list(self):
        desired_week = self._resolve_week_start()
        if not self.user_id and self.todo_list_id:
            self.user = self.todo_list.user
        if not self.todo_list_id or self.todo_list.week_start != desired_week:
            list_obj, _ = TodoList.objects.get_or_create(
                user=self.user,
                week_start=desired_week,
                defaults={"title": ""},
            )
            self.todo_list = list_obj
        self.week_start = desired_week

    def save(self, *args, **kwargs):
        if not self.user_id and self.todo_list_id:
            self.user = self.todo_list.user
        if not self.week_start and not self.due_date:
            self.week_start = start_of_week()
        self._ensure_list()
        if self.status == self.Status.DONE and not self.completed_at:
            self.completed_at = timezone.now()
        elif self.status in {self.Status.PENDING, self.Status.IN_PROGRESS}:
            self.completed_at = None
        super().save(*args, **kwargs)

    def log_activity(self, *, action: str, actor=None, metadata=None):
        metadata = metadata or {}
        return TodoActivity.objects.create(
            todo_item=self,
            actor=actor,
            action=action,
            metadata=metadata,
        )

    def set_status(self, new_status: str, *, actor=None, metadata=None):
        if new_status not in dict(self.Status.choices):
            raise ValidationError({"status": _("Invalid status.")})
        if self.status == new_status:
            return
        previous = self.status
        self.status = new_status
        if new_status == self.Status.DONE and not self.completed_at:
            self.completed_at = timezone.now()
        elif new_status in {self.Status.PENDING, self.Status.IN_PROGRESS}:
            self.completed_at = None
        self.save(update_fields=["status", "completed_at", "updated_at"])
        payload = {"from": previous, "to": new_status}
        if metadata:
            payload.update(metadata)
        self.log_activity(
            action=TodoActivity.Action.STATUS_CHANGED,
            actor=actor,
            metadata=payload,
        )
        try:
            from .services.todos import TodoHistoryService

            TodoHistoryService(self).handle_status_change(previous_status=previous, actor=actor)
        except Exception:  # pragma: no cover
            pass


class TodoActivity(TimeStampedModel):
    class Action(models.TextChoices):
        CREATED = "created", _("Created")
        UPDATED = "updated", _("Updated")
        STATUS_CHANGED = "status_changed", _("Status changed")
        DELETED = "deleted", _("Deleted")

    todo_item = models.ForeignKey(
        TodoItem,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="todo_activities",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = _("To-do activity")
        verbose_name_plural = _("To-do activities")


class TodoWeekSnapshot(TimeStampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="todo_week_snapshots",
    )
    todo_item = models.ForeignKey(
        TodoItem,
        on_delete=models.CASCADE,
        related_name="week_snapshots",
    )
    week_start = models.DateField(db_index=True)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    completed_at = models.DateTimeField()
    is_active = models.BooleanField(default=True, db_index=True)
    reopened_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-week_start", "-completed_at")
        indexes = [
            models.Index(fields=("user", "week_start")),
            models.Index(fields=("todo_item", "week_start")),
        ]
        verbose_name = _("Weekly to-do snapshot")
        verbose_name_plural = _("Weekly to-do snapshots")

    def __str__(self):  # pragma: no cover
        return f"{self.title} ({self.week_start})"


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
    VIEW_CONFIDENTIAL_WORK_ORDERS = "view_confidential_work_orders"
    VIEW_BUDGETS = "view_budgets"
    MANAGE_BUDGETS = "manage_budgets"
    APPROVE_BUDGETS = "approve_budgets"
    EXPORT_BUDGETS = "export_budgets"

    @classmethod
    def all(cls) -> set[str]:
        return {
            cls.VIEW_ALL_BUILDINGS,
            cls.MANAGE_BUILDINGS,
            cls.CREATE_UNITS,
            cls.CREATE_WORK_ORDERS,
            cls.MASS_ASSIGN,
            cls.APPROVE_WORK_ORDERS,
            cls.VIEW_AUDIT_LOG,
            cls.MANAGE_MEMBERSHIPS,
            cls.VIEW_USERS,
            cls.VIEW_CONFIDENTIAL_WORK_ORDERS,
            cls.VIEW_BUDGETS,
            cls.MANAGE_BUDGETS,
            cls.APPROVE_BUDGETS,
            cls.EXPORT_BUDGETS,
        }


ROLE_CAPABILITIES: dict[str, set[str]] = {
    MembershipRole.TECHNICIAN: {
        Capability.MANAGE_BUILDINGS,
        Capability.CREATE_UNITS,
        Capability.CREATE_WORK_ORDERS,
        Capability.VIEW_BUDGETS,
        Capability.MANAGE_BUDGETS,
    },
    MembershipRole.BACKOFFICE: {
        Capability.VIEW_ALL_BUILDINGS,
        Capability.MANAGE_BUILDINGS,
        Capability.CREATE_UNITS,
        Capability.CREATE_WORK_ORDERS,
        Capability.MASS_ASSIGN,
        Capability.APPROVE_WORK_ORDERS,
        Capability.MANAGE_MEMBERSHIPS,
        Capability.VIEW_CONFIDENTIAL_WORK_ORDERS,
        Capability.VIEW_BUDGETS,
        Capability.MANAGE_BUDGETS,
        Capability.APPROVE_BUDGETS,
        Capability.EXPORT_BUDGETS,
    },
    MembershipRole.LAWYER: {
        Capability.VIEW_ALL_BUILDINGS,
        Capability.CREATE_UNITS,
        Capability.CREATE_WORK_ORDERS,
        Capability.VIEW_CONFIDENTIAL_WORK_ORDERS,
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
        Capability.VIEW_CONFIDENTIAL_WORK_ORDERS,
        Capability.VIEW_BUDGETS,
        Capability.MANAGE_BUDGETS,
        Capability.APPROVE_BUDGETS,
        Capability.EXPORT_BUDGETS,
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


def _validate_capability_entries(values):
    values = values or []
    known = Capability.all()
    invalid = sorted({value for value in values if value and value not in known})
    if invalid:
        raise ValidationError(
            _("Unknown capability/capabilities: %(values)s."),
            params={"values": ", ".join(invalid)},
        )


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

    def clean(self):
        super().clean()
        if not self.user_id or not self.role:
            return
        conflict = (
            BuildingMembership.objects.filter(user=self.user)
            .exclude(pk=self.pk)
            .exclude(role=self.role)
        )
        if conflict.exists():
            raise ValidationError(
                {
                    "role": _(
                        "%(user)s already has the %(role)s role. Users can only have one role."
                    )
                    % {
                        "user": self.user.get_full_name() or self.user.username,
                        "role": MembershipRole(conflict.values_list("role", flat=True).first()).label,
                    }
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        overrides = self.capabilities_override or {}
        _validate_capability_entries(overrides.get("add"))
        _validate_capability_entries(overrides.get("remove"))
        overrides["add"] = _normalize_capability_list(overrides.get("add"))
        overrides["remove"] = _normalize_capability_list(overrides.get("remove"))
        self.capabilities_override = overrides
        if self.role != MembershipRole.TECHNICIAN:
            self.technician_subrole = ""
        super().save(*args, **kwargs)

    @property
    def is_global(self) -> bool:
        return self.building_id is None


def _user_can_view_confidential_orders(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return BuildingMembership.objects.filter(
        user=user,
        role__in=(
            MembershipRole.LAWYER,
            MembershipRole.BACKOFFICE,
            MembershipRole.ADMINISTRATOR,
        ),
    ).exists()


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
        CREATED = "created", _("Created")
        UPDATED = "updated", _("Updated")
        ATTACHMENTS = "attachments", _("Attachments updated")
        STATUS_CHANGED = "status_changed", _("Status changed")
        APPROVAL = "approval", _("Approval decision" )
        REASSIGNED = "reassigned", _("Reassigned")
        ARCHIVED = "archived", _("Archived")

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
