from __future__ import annotations

from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm, UserChangeForm, SetPasswordForm
from django.utils import timezone
from django.utils.html import format_html
from django.utils.text import capfirst
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext_lazy as _

from .models import Building, Unit, WorkOrder, WorkOrderAttachment, UserSecurityProfile
from .services.files import validate_work_order_attachment

User = get_user_model()


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """
    Accept multiple uploaded files. Returns a list of UploadedFile objects.
    """

    widget = MultipleFileInput(attrs={"multiple": True})

    def __init__(self, *args, **kwargs):
        widget = kwargs.pop("widget", self.widget)
        widget.attrs.setdefault("multiple", True)
        widget.attrs.setdefault("class", "input")
        super().__init__(*args, widget=widget, **kwargs)

    def clean(self, data, initial=None):
        if not data:
            return []
        if not isinstance(data, (list, tuple)):
            data = [data]

        cleaned = []
        errors = []
        for uploaded in data:
            try:
                cleaned_file = super().clean(uploaded, initial)
                validate_work_order_attachment(cleaned_file)
                cleaned.append(cleaned_file)
            except forms.ValidationError as exc:
                errors.extend(exc.error_list)
        if errors:
            raise forms.ValidationError(errors)
        return cleaned


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
        self._resolved_building = None
        super().__init__(*args, **kwargs)

        # If your form exposes the building field, lock it to the provided building
        if "building" in self.fields:
            if self._building is not None:
                self.fields["building"].initial = self._building
                self.fields["building"].disabled = True

        self.fields["floor"].label = _("Floor")
        self.fields["is_occupied"].label = _("Is occupied")

        # show an inline hint in the input
        self.fields["contact_phone"].widget.attrs.setdefault(
            "placeholder", "+359...")

    def _resolve_building(self):
        if self._resolved_building is not None:
            return self._resolved_building
        candidate = self._building or self.cleaned_data.get("building") or getattr(self.instance, "building", None)
        if candidate and not isinstance(candidate, Building):
            try:
                candidate = Building.objects.get(pk=candidate)
            except (Building.DoesNotExist, TypeError, ValueError):
                candidate = None
        self._resolved_building = candidate
        return self._resolved_building

    def clean_number(self):
        number = (self.cleaned_data.get("number") or "").strip()

        # Decide which building to validate against without touching a missing relation
        building = self._resolve_building()
        building_id = getattr(building, "pk", None)

        # Enforce uniqueness only when we know the building
        if building_id and number:
            qs = Unit.objects.filter(building_id=building_id, number__iexact=number)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError(
                    _("Apartment number must be unique within this building.")
                )
        return number

    def clean(self):
        cleaned = super().clean()
        building = self._resolve_building()

        if self._user and not (self._user.is_staff or self._user.is_superuser):
            if building and building.owner_id != self._user.id:
                self.add_error(
                    None,
                    _("You don't have permission to edit this unit."),
                )
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)

        # Ensure the unit is bound to the building coming from the view
        resolved_building = self._resolve_building()
        if resolved_building is not None:
            obj.building = resolved_building

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

    @staticmethod
    def _coerce_building(b) -> Building | None:
        if isinstance(b, Building):
            return b
        if b in (None, ""):
            return None
        try:
            return Building.objects.get(pk=int(b))
        except (Building.DoesNotExist, TypeError, ValueError):
            return None

    # ---------- init ----------
    def __init__(self, *args, user=None, building=None, **kwargs):
        self._user = user
        self._building = building
        self._locked_building_obj = self._coerce_building(building)
        self._effective_building = None
        self._existing_attachments = []
        self._attachment_lookup: dict[str, WorkOrderAttachment] = {}
        super().__init__(*args, **kwargs)

        # 1) Building choices / lock + hide if provided
        if self._building is not None:
            b_id = self._resolve_building_id(self._building)
            self.fields["building"].queryset = Building.objects.filter(pk=b_id)
            self.fields["building"].initial = b_id
            self.fields["building"].widget = forms.HiddenInput()
            self.fields["building"].required = False
        else:
            bqs = Building.objects.all()
            if user and not (user.is_staff or user.is_superuser):
                # Use your visibility helper if available
                try:
                    bqs = Building.objects.visible_to(user)
                except AttributeError:
                    bqs = bqs.filter(owner=user)
            self.fields["building"].queryset = bqs

        self.fields["building"].label = _("Building")

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
        if b_id:
            self.fields["unit"].widget.attrs.pop("disabled", None)
        else:
            self.fields["unit"].widget.attrs["disabled"] = "disabled"
            self.fields["unit"].empty_label = _("Select a building first")

        self.fields["unit"].label = _("Unit")
        self.fields["priority"].label = _("Priority")
        self.fields["status"].label = _("Status")
        self.fields["deadline"].label = _("Deadline")

        # Attachments (upload)
        self.fields["new_attachments"] = MultipleFileField(
            required=False,
            label=_("Add attachments"),
            help_text=_("Upload one or more images or documents related to this work order."),
        )

        # Attachments (delete existing)
        if self.instance.pk:
            attachments = list(
                self.instance.attachments.order_by("-created_at").select_related(None)
            )
            if attachments:
                self._existing_attachments = attachments
                choices = []
                for attachment in attachments:
                    key = str(attachment.pk)
                    self._attachment_lookup[key] = attachment
                    name = attachment.original_name or Path(attachment.file.name).name
                    url = ""
                    if attachment.file:
                        try:
                            url = attachment.file.url
                        except ValueError:
                            url = ""
                    size_label = filesizeformat(attachment.size or 0)
                    label_html = format_html(
                        '<span class="flex items-center gap-2">'
                        '<a href="{url}" target="_blank" rel="noopener noreferrer" class="link">{name}</a>'
                        '<span class="text-xs text-slate-500 dark:text-slate-400">({size})</span>'
                        '</span>',
                        url=url,
                        name=name,
                        size=size_label,
                    )
                    choices.append((key, label_html))
                self.fields["remove_attachments"] = forms.MultipleChoiceField(
                    required=False,
                    label=_("Remove existing attachments"),
                    choices=choices,
                    widget=forms.CheckboxSelectMultiple,
                    help_text=_("Select files to delete when saving this work order."),
                )

    # ---------- validation ----------
    def clean(self):
        cleaned = super().clean()

        # Prefer locked building when provided
        building = cleaned.get("building")
        if building is None and self._building is not None:
            b_id = self._resolve_building_id(self._building)
            building = self._coerce_building(self._building)
        elif building is None and getattr(self.instance, "building_id", None):
            building = getattr(self.instance, "building")

        unit = cleaned.get("unit")

        # Unit must belong to building
        if building and unit and unit.building_id != building.id:
            self.add_error("unit", _("Selected unit does not belong to the chosen building."))

        # Non-staff must own the building
        if self._user and not (self._user.is_staff or self._user.is_superuser) and building:
            if building.owner_id != self._user.id:
                self.add_error("building", _("You cannot create work orders for buildings you do not own."))

        deadline = cleaned.get("deadline")
        if deadline and deadline < timezone.localdate():
            self.add_error("deadline", _("Deadline cannot be in the past."))

        self._effective_building = building
        return cleaned

    def clean_remove_attachments(self):
        ids = self.cleaned_data.get("remove_attachments")
        if not ids:
            return []
        invalid = [attachment_id for attachment_id in ids if attachment_id not in self._attachment_lookup]
        if invalid:
            raise forms.ValidationError(_("One or more attachments could not be found."))
        return ids

    # ---------- save ----------
    def save(self, commit: bool = True):
        obj: WorkOrder = super().save(commit=False)

        # Force building when form initialized with one
        if self._locked_building_obj is not None:
            obj.building = self._locked_building_obj

        if commit:
            obj.save()
        return obj

    # ---------- attachments helpers ----------
    def save_attachments(self, work_order: WorkOrder):
        """
        Persist uploaded attachments and delete any that were flagged for removal.
        """
        remove_ids = self.cleaned_data.get("remove_attachments", []) if hasattr(self, "cleaned_data") else []
        if remove_ids:
            to_delete = [
                self._attachment_lookup[pk] for pk in remove_ids if pk in self._attachment_lookup
            ]
            for attachment in to_delete:
                attachment.delete()

        new_files = self.cleaned_data.get("new_attachments", []) if hasattr(self, "cleaned_data") else []
        for uploaded in new_files:
            attachment = WorkOrderAttachment(
                work_order=work_order,
                file=uploaded,
                original_name=getattr(uploaded, "name", ""),
            )
            attachment.save()


class MassAssignWorkOrdersForm(forms.Form):
    title = forms.CharField(
        max_length=255,
        label=_("Title"),
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    description = forms.CharField(
        required=False,
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 6, "class": "input"}),
    )
    buildings = forms.ModelMultipleChoiceField(
        queryset=Building.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        label=_("Buildings to include"),
        required=False,
    )

    def __init__(self, *args, buildings_queryset=None, **kwargs):
        super().__init__(*args, **kwargs)

        qs = buildings_queryset or Building.objects.none()
        field = self.fields["buildings"]
        field.queryset = qs
        field.widget.attrs.setdefault("class", "space-y-2")

        if qs.exists():
            if not self.is_bound:
                field.initial = list(qs.values_list("pk", flat=True))
        else:
            field.disabled = True

        def _label_from_instance(building: Building) -> str:
            owner = getattr(building, "owner", None)
            if owner:
                owner_label = owner.get_full_name() or owner.username
                return _("%(building)s — owner %(owner)s") % {
                    "building": building.name,
                    "owner": owner_label,
                }
            return building.name

        field.label_from_instance = _label_from_instance

    def clean_buildings(self):
        buildings = self.cleaned_data.get("buildings")
        field = self.fields["buildings"]
        if field.queryset.exists() and not buildings:
            raise forms.ValidationError(_("Select at least one building."))
        return buildings


# -----------------------------
# Users (admin area)
# -----------------------------

_USER_IS_ACTIVE_FIELD = User._meta.get_field("is_active")
_USER_SUPERUSER_FIELD = User._meta.get_field("is_superuser")


class AdminUserCreateForm(UserCreationForm):
    email = forms.EmailField(required=False, label=_("Email"))
    first_name = forms.CharField(required=False, label=_("First name"))
    last_name = forms.CharField(required=False, label=_("Last name"))
    is_active = forms.BooleanField(required=False, initial=True)
    is_superuser = forms.BooleanField(required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_superuser",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "checkbox")
            else:
                widget.attrs.setdefault("class", "input")
        if "email" in self.fields:
            self.fields["email"].help_text = _("Optional, used for contact and password resets.")
        self.fields["is_active"].label = capfirst(_USER_IS_ACTIVE_FIELD.verbose_name)
        self.fields["is_active"].help_text = _USER_IS_ACTIVE_FIELD.help_text
        self.fields["is_superuser"].label = capfirst(_USER_SUPERUSER_FIELD.verbose_name)
        self.fields["is_superuser"].help_text = _USER_SUPERUSER_FIELD.help_text

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get("email", "")
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.is_active = self.cleaned_data.get("is_active", False)
        user.is_superuser = self.cleaned_data.get("is_superuser", False)
        user.is_staff = user.is_superuser
        if commit:
            user.save()
            self.save_m2m()
            profile, created = UserSecurityProfile.objects.get_or_create(user=user)
            profile.reset()
        return user


class AdminUserUpdateForm(UserChangeForm):
    password = None  # hide the unusable password hash field

    class Meta(UserChangeForm.Meta):
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_superuser",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "checkbox")
            else:
                widget.attrs.setdefault("class", "input")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = user.is_superuser
        if commit:
            user.save()
            self.save_m2m()
            profile, created = UserSecurityProfile.objects.get_or_create(user=user)
            if user.is_active:
                profile.reset()
        return user


class AdminUserPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")
