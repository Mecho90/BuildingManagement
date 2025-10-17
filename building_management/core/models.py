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
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True, default="")
    description = models.TextField(blank=True, default="")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )

    def __str__(self):
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
    building = models.ForeignKey(Building, on_delete=models.CASCADE, related_name="units")
    number = models.PositiveIntegerField("Apartment number")
    floor = models.IntegerField()
    owner_name = models.CharField("Unit Owner", max_length=200, blank=True, default="")
    contact_phone = models.CharField("Contact", max_length=50, blank=True, default="")
    is_occupied = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["building", "number"], name="unique_unit_number_per_building"
            )
        ]

    def __str__(self):
        return f"{self.building} • #{self.number}"

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
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"

    PRIORITY_HIGH = "high"
    PRIORITY_MEDIUM = "medium"
    PRIORITY_LOW = "low"
    PRIORITY_CHOICES = (
        (PRIORITY_HIGH, "High"),
        (PRIORITY_MEDIUM, "Medium"),
        (PRIORITY_LOW, "Low"),
    )

    building = models.ForeignKey(Building, on_delete=models.CASCADE, related_name="work_orders")
    unit = models.ForeignKey(Unit, null=True, blank=True, on_delete=models.SET_NULL, related_name="work_orders")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default=PRIORITY_MEDIUM, db_index=True)
    deadline = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title