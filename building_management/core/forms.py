from __future__ import annotations

import re
from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from .models import Building, Unit, WorkOrder

User = get_user_model()


# -----------------------------
# Buildings
# -----------------------------
class BuildingForm(forms.ModelForm):
    """
    - Staff users can choose an Owner from a dropdown.
    - Non-staff users do not see the Owner field; the form will force
      owner = request.user on save.
    - Pass the current user via: BuildingForm(..., user=request.user)
    """

    class Meta:
        model = Building
        fields = ["name", "role", "address", "description", "owner"]  # 'owner' shown only to staff
        widgets = {
            "description": forms.Textarea(attrs={"rows": 6}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._user = user

        if user and user.is_staff:
            # Admins can assign to anyone
            self.fields["owner"].queryset = User.objects.order_by("username")
        else:
            # Non-admins must not control the owner; remove the field completely
            self.fields.pop("owner", None)

    def save(self, commit: bool = True):
        obj: Building = super().save(commit=False)

        # Safety net: non-staff cannot set arbitrary owners
        if not (self._user and self._user.is_staff):
            if self._user is not None:
                obj.owner = self._user

        if commit:
            obj.save()
        return obj


_PHONE_RE = re.compile(r"^\+?\d{7,15}$")
# -----------------------------
# Units
# -----------------------------
class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = ("number", "floor", "owner_name", "contact_phone", "is_occupied", "description")
        # If your form includes "building", keep it in fields and this class will lock it.

    def __init__(self, *args, user=None, building=None, **kwargs):
        # ALWAYS set these attributes so save() never fails
        self._user = user
        self._building = building
        super().__init__(*args, **kwargs)

        # If your form exposes the building field, lock it to the provided building
        if "building" in self.fields:
            if self._building is not None:
                self.fields["building"].initial = self._building
                self.fields["building"].disabled = True
                
                # show an inline hint in the input
        self.fields["contact_phone"].widget.attrs.setdefault(
            "placeholder", "+359..."
        )

    def clean_number(self):
        number = (self.cleaned_data.get("number") or "").strip()

        # Decide which building to validate against without touching a missing relation
        building_id = getattr(self._building, "pk", None) or self.instance.building_id

        # Enforce uniqueness only when we know the building
        if building_id and number:
            qs = Unit.objects.filter(building_id=building_id, number__iexact=number)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    "Apartment number must be unique within this building."
                )
        return number

    def save(self, commit=True):
        obj = super().save(commit=False)

        # Ensure the unit is bound to the building coming from the view
        if self._building is not None:
            obj.building = self._building

        # Optional permission safety: only owners/staff can save
        if self._user and not (self._user.is_staff or self._user.is_superuser):
            if obj.building and obj.building.owner_id != self._user.id:
                raise forms.ValidationError("You don't have permission to edit this unit.")

        if commit:
            obj.save()
        return obj

# -----------------------------
# Work Orders
# -----------------------------

class WorkOrderForm(forms.ModelForm):
    """
    Usage from views:
        WorkOrderForm(..., user=request.user, building=<Building|id|None>)

    - If `building` is provided → lock to it, hide the Building field,
      and filter Unit choices to that building.
    - Otherwise → limit Building choices to those visible to the user,
      and filter Unit based on current selection/instance.
    - Validates Unit belongs to Building and that non-staff own the Building.
    """

    class Meta:
        model = WorkOrder
        fields = ["title", "building", "unit", "priority", "status", "deadline", "description"]
        widgets = {
            "deadline": forms.DateInput(attrs={"type": "date", "class": "input-date"}),
            "description": forms.Textarea(attrs={"rows": 6}),
        }

    # ---------- helpers ----------
    @staticmethod
    def _resolve_building_id(b) -> int | None:
        if isinstance(b, Building):
            return b.pk
        if b in (None, ""):
            return None
        try:
            return int(b)
        except (TypeError, ValueError):
            return None

    # ---------- init ----------
    def __init__(self, *args, user=None, building=None, **kwargs):
        self._user = user
        self._building = building
        super().__init__(*args, **kwargs)

        # 1) Building choices / lock + hide if provided
        if self._building is not None:
            b_id = self._resolve_building_id(self._building)
            self.fields["building"].queryset = Building.objects.filter(pk=b_id)
            self.fields["building"].initial = b_id
            self.fields["building"].widget = forms.HiddenInput()
        else:
            bqs = Building.objects.all()
            if user and not (user.is_staff or user.is_superuser):
                # Use your visibility helper if available
                try:
                    bqs = Building.objects.visible_to(user)
                except AttributeError:
                    bqs = bqs.filter(owner=user)
            self.fields["building"].queryset = bqs

        # 2) Units: filter by effective building (kwarg > POST > initial > instance)
        effective_b = (
            self._building
            if self._building is not None
            else self.data.get("building")
                or self.initial.get("building")
                or getattr(self.instance, "building_id", None)
        )
        b_id = self._resolve_building_id(effective_b)
        self.fields["unit"].queryset = (
            Unit.objects.filter(building_id=b_id).order_by("number") if b_id else Unit.objects.none()
        )

    # ---------- validation ----------
    def clean(self):
        cleaned = super().clean()

        # Prefer locked building when provided
        building = cleaned.get("building")
        if building is None and self._building is not None:
            b_id = self._resolve_building_id(self._building)
            building = Building.objects.filter(pk=b_id).first()

        unit = cleaned.get("unit")

        # Unit must belong to building
        if building and unit and unit.building_id != building.id:
            self.add_error("unit", "Selected unit does not belong to the chosen building.")

        # Non-staff must own the building
        if self._user and not (self._user.is_staff or self._user.is_superuser) and building:
            if building.owner_id != self._user.id:
                self.add_error("building", "You cannot create work orders for buildings you do not own.")

        return cleaned

    # ---------- save ----------
    def save(self, commit: bool = True):
        obj: WorkOrder = super().save(commit=False)

        # Force building when form initialized with one
        if self._building is not None:
            b_id = self._resolve_building_id(self._building)
            if b_id:
                obj.building_id = b_id

        # Extra safety: enforce ownership
        if self._user and not (self._user.is_staff or self._user.is_superuser):
            if obj.building_id and not Building.objects.filter(pk=obj.building_id, owner=self._user).exists():
                raise ValidationError("You cannot assign work orders to this building.")

        if commit:
            obj.save()
        return obj
