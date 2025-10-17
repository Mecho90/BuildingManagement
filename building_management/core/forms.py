from __future__ import annotations

from typing import Any, Optional

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from .models import Building, Unit, WorkOrder

import re

# ---------------------------
# Helpers for widget styling
# ---------------------------
def _add_cls(widget: forms.Widget, cls: str) -> None:
    """Merge CSS classes into a widget (idempotent)."""
    existing = widget.attrs.get("class", "")
    classes = {c for c in existing.split() if c}
    classes.update(cls.split())
    widget.attrs["class"] = " ".join(sorted(classes))


def _default_text_widgets() -> dict[str, forms.Widget]:
    # Common widgets used across forms for a consistent look
    return {
        "name": forms.TextInput(attrs={"placeholder": "Name"}),
        "address": forms.TextInput(attrs={"placeholder": "Address"}),
        "description": forms.Textarea(attrs={"rows": 6, "placeholder": "Optional description (Markdown supported)"}),
    }


# ---------------
# Building form
# ---------------
class BuildingForm(forms.ModelForm):
    """No owner_name / contact in the form."""
    class Meta:
        model = Building
        fields = ["name", "address", "description", "owner"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 6, "placeholder": "Optional description (Markdown supported)"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user and not getattr(user, "is_superuser", False):
            self.fields["owner"].widget = forms.HiddenInput()
            if self.instance and self.instance.pk:
                self.initial["owner"] = self.instance.owner_id


_PHONE_RE = re.compile(r"^\+?\d{7,15}$")
# ----------
# Unit form
# ----------
class UnitForm(forms.ModelForm):
    number = forms.IntegerField(min_value=0, label="Apartment number")

    class Meta:
        model = Unit
        fields = ["number", "floor", "owner_name", "contact_phone", "is_occupied", "description"]
        widgets = {
            "contact_phone": forms.TextInput(attrs={
                "type": "tel",
                "placeholder": "e.g. +359...",
                "pattern": _PHONE_RE.pattern,
                "title": "Phone number, digits only; e.g. +3591234567",
            }),
            "description": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, building: Building | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._building = building

    def clean_contact_phone(self):
        val = (self.cleaned_data.get("contact_phone") or "").strip()
        if not val:
            return ""
        bad = re.sub(r"[\s().-]+", "", val)
        if not _PHONE_RE.fullmatch(bad):
            raise ValidationError("Enter a valid phone number (digits only, e.g. +3591234567).")
        return bad

    def clean(self):
        cleaned = super().clean()
        bld = self._building or getattr(self.instance, "building", None)
        num = cleaned.get("number")
        if bld and num is not None:
            exists = Unit.objects.filter(building=bld, number=num).exclude(pk=self.instance.pk).exists()
            if exists:
                self.add_error("number", "A unit with this apartment number already exists in this building.")
        return cleaned


# -------------------
# Work order form
# -------------------
class WorkOrderForm(forms.ModelForm):
    """Accepts optional `user` (ignored) and `building` (to prefill/lock)."""
    class Meta:
        model = WorkOrder
        fields = ["building", "unit", "title", "description", "priority", "status", "deadline"]
        widgets = {
            "deadline": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 6, "placeholder": "Optional details"}),
        }

    def __init__(self, *args, building: Building | None = None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._building = building
        if building is not None:
            self.fields["building"].initial = building.pk
            # To lock the building, uncomment next line:
            # self.fields["building"].widget = forms.HiddenInput()

    def save(self, commit=True):
        obj: WorkOrder = super().save(commit=False)
        if not obj.building_id and self._building:
            obj.building = self._building
        if commit:
            obj.save()
            self.save_m2m()
        return obj
