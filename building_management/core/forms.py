# path: core/forms.py
from __future__ import annotations

from typing import Any, Optional

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from .models import Building, Unit, WorkOrder


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
    """
    - Admins can assign 'owner' (dropdown of users).
    - Non-admins: owner field is hidden when `user` is provided.
    - Views still enforce final ownership in form_valid for extra safety.
    """

    class Meta:
        model = Building
        fields = ["name", "address", "description", "owner"]
        widgets = _default_text_widgets() | {
            "owner": forms.Select(),
        }

    def __init__(self, *args: Any, user=None, **kwargs: Any) -> None:
        """
        Pass `user=request.user` from the view to hide owner for non-admins.
        (If not provided, the field remains visible—views still guard assignment.)
        """
        super().__init__(*args, **kwargs)

        # Consistent styling
        for fname, field in self.fields.items():
            if isinstance(field.widget, (forms.TextInput, forms.Select)):
                _add_cls(field.widget, "input-lg")
            elif isinstance(field.widget, forms.Textarea):
                _add_cls(field.widget, "textarea-lg")

        # Configure owner field visibility/queryset
        owner_field = self.fields.get("owner")
        if owner_field:
            User = get_user_model()
            owner_field.queryset = User.objects.order_by("username")
            owner_field.required = False

            if user is not None and not getattr(user, "is_superuser", False):
                # Hide for non-admins when user is provided
                self.fields.pop("owner", None)

    def clean_name(self) -> str:
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise ValidationError("Name is required.")
        return name

    def clean_address(self) -> str:
        address = (self.cleaned_data.get("address") or "").strip()
        if not address:
            raise ValidationError("Address is required.")
        return address


# ----------
# Unit form
# ----------

class UnitForm(forms.ModelForm):
    """
    Expects `building` to be provided in __init__ for create (and update we infer from instance).
    Enforces unique (building, number) at the form layer to give a friendly error
    instead of a DB IntegrityError.
    """

    number = forms.IntegerField(min_value=0, label="Apartment number")
    floor = forms.IntegerField(label="Floor", required=True)

    class Meta:
        model = Unit
        fields = ["number", "floor", "is_occupied", "description"]
        widgets = {
            "number": forms.NumberInput(),
            "floor": forms.NumberInput(),
            "is_occupied": forms.CheckboxInput(),
            "description": forms.Textarea(attrs={"rows": 6, "placeholder": "Optional description (Markdown supported)"}),
        }

    def __init__(self, *args: Any, building: Optional[Building] = None, **kwargs: Any) -> None:
        """
        Pass `building=<Building>` from the view on create.
        On update, if not provided, we infer from the instance.
        """
        super().__init__(*args, **kwargs)
        self._building: Optional[Building] = building or getattr(self.instance, "building", None)

        # Styling
        _add_cls(self.fields["number"].widget, "input-lg")
        _add_cls(self.fields["floor"].widget, "input-lg")
        _add_cls(self.fields["description"].widget, "textarea-lg")

    # Friendly uniqueness validation
    def clean_number(self) -> int:
        num = self.cleaned_data.get("number")
        if num is None:
            raise ValidationError("Apartment number is required.")
        if num < 0:
            raise ValidationError("Apartment number must be zero or a positive integer.")
        return num

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        building = self._building
        num = cleaned.get("number")

        # Only validate if we know the building
        if building and num is not None:
            qs = Unit.objects.filter(building=building, number=num)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error(
                    "number",
                    "A unit with this apartment number already exists in this building."
                )
        return cleaned


# -------------------
# Work order form
# -------------------
class WorkOrderForm(forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = ["unit", "title", "description", "status", "deadline"]
        widgets = {
            # HTML5 date input => browser calendar picker
            "deadline": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        # these are passed in by the view's _WorkOrderBase.get_form_kwargs()
        user = kwargs.pop("user", None)
        building = kwargs.pop("building", None)
        super().__init__(*args, **kwargs)

        # Unit is optional now
        self.fields["unit"].required = False
        self.fields["unit"].empty_label = "— None —"

        # Limit units to the chosen building if provided; otherwise restrict to user's units
        if building is not None:
            self.fields["unit"].queryset = Unit.objects.filter(building=building)
        elif user and not getattr(user, "is_staff", False):
            self.fields["unit"].queryset = Unit.objects.filter(building__owner=user)

        # keep the building for save()
        self._building = building

    def save(self, commit=True):
        obj: WorkOrder = super().save(commit=False)

        # ensure building is set even if no unit is chosen
        if obj.unit_id and not obj.building_id:
            obj.building = obj.unit.building
        elif self._building and not obj.building_id:
            obj.building = self._building

        if commit:
            obj.save()
            self.save_m2m()
        return obj