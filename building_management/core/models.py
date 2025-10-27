# core/models.py
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce, Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ---------------------------------------------------------------------------
# QuerySets with per-user visibility helpers
# ---------------------------------------------------------------------------

class BuildingQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not user.is_authenticated:
            return self.none()
        if user.is_staff or user.is_superuser:
            return self
        return self.filter(owner=user)

    def with_unit_stats(self):
        """
        Annotate each Building with:
          - _units_count: total units
          - _work_orders_count: ACTIVE work orders (OPEN + IN_PROGRESS), non-archived
        """
        unit_totals = (
            Unit.objects.filter(building_id=OuterRef("pk"))
            .order_by()
            .values("building_id")
            .annotate(total=Count("pk"))
            .values("total")
        )
        active_workorders = (
            WorkOrder.objects.filter(
                building_id=OuterRef("pk"),
                archived_at__isnull=True,
                status__in=[
                    WorkOrder.Status.OPEN,
                    WorkOrder.Status.IN_PROGRESS,
                ],
            )
            .order_by()
            .values("building_id")
            .annotate(total=Count("pk"))
            .values("total")
        )
        return self.annotate(
            _units_count=Coalesce(Subquery(unit_totals[:1]), Value(0), output_field=IntegerField()),
            _work_orders_count=Coalesce(Subquery(active_workorders[:1]), Value(0), output_field=IntegerField()),
        )


class UnitQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not user.is_authenticated:
            return self.none()
        if user.is_staff or user.is_superuser:
            return self
        return self.filter(building__owner=user)


class WorkOrderQuerySet(models.QuerySet):
    def visible_to(self, user):
        if not user or not user.is_authenticated:
            return self.none()
        if user.is_staff or user.is_superuser:
            return self
        return self.filter(building__owner=user)


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
        TECH_SUPPORT = "TECH_SUPPORT", _( "Technical Support" )
        PROPERTY_MANAGER = "PROPERTY_MANAGER", _( "Property Manager" )
        EXTERNAL_CONTRACTOR = "EXTERNAL_CONTRACTOR", _( "External Contractor" )

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
            status__in=[WorkOrder.Status.OPEN, WorkOrder.Status.IN_PROGRESS],
        ).count()


phone_validator = RegexValidator(
    r"^\+?\d{7,15}$",
    _( "Enter a valid phone number (digits with optional leading +, 7–15 digits)." ),
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
        OPEN = "OPEN", _( "Open" )
        IN_PROGRESS = "IN_PROGRESS", _( "In progress" )
        DONE = "DONE", _( "Done" )

    class Priority(models.TextChoices):
        LOW = "LOW", _( "Low" )
        MEDIUM = "MEDIUM", _( "Medium" )
        HIGH = "HIGH", _( "High" )

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
    # Mandatory deadline (non-nullable at DB level)
    deadline = models.DateField(null=False, blank=False)

    # Archiving toggle (when done and user archives)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

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
                {"unit": _( "Selected unit does not belong to the selected building." )}
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


class UserSecurityProfile(models.Model):
    class LockReason(models.TextChoices):
        FAILED_ATTEMPTS = "failed_attempts", _( "Too many failed login attempts" )
        MANUAL = "manual", _( "Manually locked" )

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
