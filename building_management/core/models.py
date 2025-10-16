# app/models.py
from __future__ import annotations

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Count, Q


class BuildingQuerySet(models.QuerySet):
    def with_unit_stats(self) -> "BuildingQuerySet":
        """
        Annotate counts so templates can render numbers without extra queries.
        """
        return self.annotate(
            total_units=Count("units", distinct=True),
            occupied_units=Count("units", filter=Q(units__is_occupied=True), distinct=True),
        )


class Building(models.Model):
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    description = models.TextField(blank=True, default="")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="buildings",
    )

    objects: BuildingQuerySet = BuildingQuerySet.as_manager()  # leverage annotations

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    # Prefer annotations when available to avoid extra queries.
    @property
    def units_count(self) -> int:
        val = getattr(self, "total_units", None)
        return int(val) if val is not None else self.units.count()

    @property
    def occupied_count(self) -> int:
        val = getattr(self, "occupied_units", None)
        if val is not None:
            return int(val)
        return self.units.filter(is_occupied=True).count()

    @property
    def vacancy_count(self) -> int:
        return self.units_count - self.occupied_count

    @property
    def occupancy_rate(self) -> float:
        # Avoid ZeroDivisionError in empty buildings
        return (self.occupied_count / self.units_count) if self.units_count else 0.0


class Unit(models.Model):
    building = models.ForeignKey(
        Building,
        on_delete=models.CASCADE,
        related_name="units",
    )
    number = models.CharField(max_length=50)
    floor = models.IntegerField(validators=[MinValueValidator(-5)])
    is_occupied = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")

    class Meta:
        unique_together = ("building", "number")
        ordering = ["building__name", "floor", "number"]

    def __str__(self) -> str:
        return f"{self.building.name} #{self.number}"

    @property
    def occupied(self) -> bool:  # compatibility with templates using `u.occupied`
        return self.is_occupied


class Tenant(models.Model):
    unit = models.OneToOneField(
        Unit,
        on_delete=models.CASCADE,
        related_name="tenant",
    )
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=50)

    class Meta:
        ordering = ["full_name"]

    def __str__(self) -> str:
        return self.full_name


class WorkOrder(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        CLOSED = "closed", "Closed"

    # NEW: allow orders to belong to a building even if no specific unit is chosen
    building = models.ForeignKey(
        "core.Building",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="work_orders",
    )

    # CHANGED: unit is optional; if a unit is deleted we keep the order (set to NULL)
    unit = models.ForeignKey(
        "core.Unit",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="work_orders",
    )

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )

    # NEW: deadline (date only) – renders as a browser calendar with the form widget below
    deadline = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title
