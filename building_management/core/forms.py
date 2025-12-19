from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm, UserChangeForm, SetPasswordForm
from django.utils import timezone
from django.utils.html import format_html
from django.utils.text import capfirst
from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext_lazy as _
from django.db.models import Q
from django.db.models.functions import Lower

from .authz import Capability, CapabilityResolver
from .models import (
    Building,
    BuildingMembership,
    MembershipRole,
    Unit,
    WorkOrder,
    WorkOrderAttachment,
    UserSecurityProfile,
)
from .utils.roles import user_can_approve_work_orders, user_is_lawyer

ROLE_DESCRIPTIONS = {
    MembershipRole.TECHNICIAN: _("Technician – access to assigned buildings."),
    MembershipRole.BACKOFFICE: _("Backoffice – manage assignments for their buildings."),
    MembershipRole.LAWYER: _("Lawyer – read-only access to all buildings; can create legal work orders."),
    MembershipRole.ADMINISTRATOR: _("Administrator – full system access."),
}


class RoleSelectionMixin:
    role_descriptions = ROLE_DESCRIPTIONS

    def role_description(self, value):
        return ROLE_DESCRIPTIONS.get(value, "")
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


class RoleAwareCheckboxSelect(forms.CheckboxSelectMultiple):
    """Checkbox widget that carries the role metadata for each option."""

    def __init__(self, *args, role_map=None, role_labels=None, **kwargs):
        self.role_map = {str(key): value for key, value in (role_map or {}).items()}
        self.role_labels = {str(key): value for key, value in (role_labels or {}).items()}
        kwargs.setdefault("attrs", {})
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        role_codes = self.role_map.get(str(option["value"]))
        role_labels = self.role_labels.get(str(option["value"]))
        option_attrs = option.setdefault("attrs", {})
        base_classes = option_attrs.get("class", "")
        option_attrs["class"] = (
            f"{base_classes} user-checkbox h-4 w-4 rounded border border-slate-300 text-emerald-600 focus:ring-emerald-500"
        ).strip()
        if role_codes:
            if isinstance(role_codes, (list, tuple, set)):
                option_attrs["data-role"] = ",".join(role_codes)
            else:
                option_attrs["data-role"] = str(role_codes)
        return option


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
        fields = ["name", "owner", "role", "address", "description"]  # 'owner' shown only to staff
        widgets = {
            "description": forms.Textarea(attrs={"rows": 6}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._user = user
        self._resolver = CapabilityResolver(user) if user and user.is_authenticated else None
        self._is_admin = self._user_is_admin(user)
        self._can_manage_buildings = (
            self._resolver.has(Capability.MANAGE_BUILDINGS)
            if self._resolver
            else False
        )

        if self._can_manage_buildings:
            owner_queryset = (
                User.objects.filter(
                    memberships__building__isnull=True,
                    memberships__role=MembershipRole.TECHNICIAN,
                )
                .order_by("username")
                .distinct()
            )
            self.fields["owner"].queryset = owner_queryset
            owner_ids = list(owner_queryset.values_list("pk", flat=True))
            technician_ids = set(
                BuildingMembership.objects.filter(
                    user_id__in=owner_ids,
                    building__isnull=True,
                    role=MembershipRole.TECHNICIAN,
                ).values_list("user_id", flat=True)
            )
            self._owner_technician_ids = technician_ids
            self.fields["owner"].widget.attrs["data-technician-users"] = ",".join(
                str(pk) for pk in sorted(technician_ids)
            )
        else:
            self.fields.pop("owner", None)

        owner_user = self._determine_owner_candidate()
        owner_is_technician = False
        if owner_user and getattr(owner_user, "pk", None):
            tech_ids = getattr(self, "_owner_technician_ids", None)
            if tech_ids is not None:
                owner_is_technician = owner_user.pk in tech_ids
            else:
                owner_is_technician = BuildingMembership.objects.filter(
                    user=owner_user,
                    building__isnull=True,
                    role=MembershipRole.TECHNICIAN,
                ).exists()
        is_editing_existing = bool(getattr(self.instance, "pk", None))
        role_field = self.fields["role"]
        if is_editing_existing and not self._is_admin:
            role_field.disabled = True
            role_field.help_text = _(
                "Only administrators can change the building role once it has been set."
            )
        elif not owner_is_technician:
            role_field.disabled = True
            role_field.help_text = _(
                "Role is editable only when the assigned user has the Technician role."
            )

    def save(self, commit: bool = True):
        obj: Building = super().save(commit=False)

        # Safety net: users without manage permission cannot set arbitrary owners
        if not self._can_manage_buildings and self._user is not None:
            obj.owner = self._user

        if commit:
            obj.save()
        return obj

    # helper methods -----------------------------------------------------

    def _determine_owner_candidate(self):
        owner_user = getattr(self.instance, "owner", None)
        if self._can_manage_buildings:
            owner_field_name = self.add_prefix("owner")
            owner_value = None
            if self.data:
                owner_value = self.data.get(owner_field_name)
            owner_value = owner_value or self.initial.get("owner")
            if owner_value:
                try:
                    owner_user = User.objects.get(pk=owner_value)
                except (User.DoesNotExist, ValueError, TypeError):
                    owner_user = owner_user
        elif self._user is not None:
            owner_user = self._user
        return owner_user

    def _user_is_admin(self, user):
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False):
            return True
        memberships = BuildingMembership.objects.filter(
            user=user,
            role=MembershipRole.ADMINISTRATOR,
        )
        if getattr(self.instance, "pk", None):
            memberships = memberships.filter(Q(building__isnull=True) | Q(building=self.instance))
        else:
            memberships = memberships.filter(building__isnull=True)
        return memberships.exists()


# -----------------------------
# Units
# -----------------------------
class UnitForm(forms.ModelForm):
    class Meta:
        model = Unit
        fields = ("number", "floor", "owner_name", "contact_phone", "description")
        # If your form includes "building", keep it in fields and this class will lock it.

    def __init__(self, *args, user=None, building=None, **kwargs):
        # ALWAYS set these attributes so save() never fails
        self._user = user
        self._resolver = CapabilityResolver(user) if user and user.is_authenticated else None
        self._building = building
        self._resolved_building = None
        super().__init__(*args, **kwargs)

        # If your form exposes the building field, lock it to the provided building
        if "building" in self.fields:
            if self._building is not None:
                self.fields["building"].initial = self._building
                self.fields["building"].disabled = True

        self.fields["floor"].label = _("Floor")

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

        if building and self._user:
            building_id = getattr(building, "pk", None)
            allowed = False
            if self._resolver:
                allowed = self._resolver.has(Capability.MANAGE_BUILDINGS, building_id=building_id) or self._resolver.has(
                    Capability.CREATE_UNITS, building_id=building_id
                )
            if not allowed:
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
        fields = [
            "title",
            "building",
            "unit",
            "priority",
            "status",
            "deadline",
            "description",
            "replacement_request_note",
        ]
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
        self._resolver = CapabilityResolver(user) if user and user.is_authenticated else None
        super().__init__(*args, **kwargs)

        # 1) Building choices / lock + hide if provided
        if self._building is not None:
            b_id = self._resolve_building_id(self._building)
            self.fields["building"].queryset = Building.objects.filter(pk=b_id)
            self.fields["building"].initial = b_id
            self.fields["building"].widget = forms.HiddenInput()
            self.fields["building"].required = False
        else:
            if user:
                bqs = Building.objects.visible_to(user)
            else:
                bqs = Building.objects.none()
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
        effective_building_obj = self._coerce_building(effective_b)
        b_id = effective_building_obj.pk if effective_building_obj else self._resolve_building_id(effective_b)
        self._effective_building = effective_building_obj
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
        self._current_status = self.instance.status if self.instance.pk else WorkOrder.Status.OPEN
        self._can_user_approve = user_can_approve_work_orders(self._user, b_id)
        self._allowed_status_values = self._compute_allowed_statuses(
            b_id, current_status=self._current_status, can_user_approve=self._can_user_approve
        )
        if self._current_status == WorkOrder.Status.AWAITING_APPROVAL and self._can_user_approve:
            self._allowed_status_values = {WorkOrder.Status.APPROVED, WorkOrder.Status.REJECTED}
            self.fields["status"].choices = [
                (WorkOrder.Status.REJECTED, WorkOrder.Status.REJECTED.label),
                (WorkOrder.Status.APPROVED, WorkOrder.Status.APPROVED.label),
            ]
        else:
            self.fields["status"].choices = [
                choice for choice in WorkOrder.Status.choices if choice[0] in self._allowed_status_values
            ]
            if self._current_status == WorkOrder.Status.AWAITING_APPROVAL:
                self.fields["status"].disabled = True
                self.fields["status"].widget.attrs["class"] = (
                    self.fields["status"].widget.attrs.get("class", "") + " cursor-not-allowed opacity-70"
                ).strip()
                self.fields["status"].help_text = _(
                    "Awaiting approval. Only backoffice users can change this status."
                )
        self.fields["replacement_request_note"].label = "Заявка за подмяна"
        self.fields["replacement_request_note"].widget = forms.Textarea(attrs={"rows": 3})
        self._approver_queryset = self._build_approver_queryset(self._effective_building)
        self._approvers_available = self._approver_queryset.exists()

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
        building_id = getattr(building, "pk", None)

        unit = cleaned.get("unit")

        # Unit must belong to building
        if building and unit and unit.building_id != building.id:
            self.add_error("unit", _("Selected unit does not belong to the chosen building."))

        # Non-staff must own the building
        if self._user and building:
            allowed = False
            if self._resolver:
                allowed = self._resolver.has(Capability.MANAGE_BUILDINGS, building_id=building_id) or self._resolver.has(
                    Capability.CREATE_WORK_ORDERS, building_id=building_id
                )
            if not allowed:
                self.add_error("building", _("You cannot create work orders for buildings you do not have access to."))

        deadline = cleaned.get("deadline")
        if deadline and deadline < timezone.localdate():
            self.add_error("deadline", _("Deadline cannot be in the past."))

        status_value = cleaned.get("status")
        allowed_statuses = self._compute_allowed_statuses(
            building_id,
            current_status=self._current_status,
            can_user_approve=self._can_user_approve,
        )
        self._allowed_status_values = allowed_statuses
        if status_value and status_value not in allowed_statuses:
            self.add_error("status", _("You cannot select this status."))

        original_status = self.instance.status if self.instance.pk else WorkOrder.Status.OPEN
        if status_value == WorkOrder.Status.AWAITING_APPROVAL and not self._approvers_available:
            self.add_error("status", _("No approvers are available for this building."))

        if (
            original_status == WorkOrder.Status.AWAITING_APPROVAL
            and status_value in {WorkOrder.Status.DONE, WorkOrder.Status.APPROVED, WorkOrder.Status.REJECTED}
        ):
            if not self._can_user_approve:
                self.add_error("status", _("You do not have permission to complete this approval."))

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
        original_status = self.instance.status if self.instance.pk else WorkOrder.Status.OPEN

        # Force building when form initialized with one
        if self._locked_building_obj is not None:
            obj.building = self._locked_building_obj

        if not obj.pk:
            if self._user_is_lawyer():
                obj.lawyer_only = True
            if self._user and getattr(self._user, "is_authenticated", False) and not obj.created_by_id:
                obj.created_by = self._user

        if obj.status == WorkOrder.Status.AWAITING_APPROVAL:
            if self._user and getattr(self._user, "is_authenticated", False):
                obj.awaiting_approval_by = self._user
            else:
                obj.awaiting_approval_by = None
        elif obj.status != WorkOrder.Status.AWAITING_APPROVAL and original_status == WorkOrder.Status.AWAITING_APPROVAL:
            obj.awaiting_approval_by = None

        if commit:
            obj.save()
        return obj

    def _user_is_lawyer(self) -> bool:
        if user_is_lawyer(self._user):
            return True
        if not self._resolver:
            return False
        can_view_confidential = self._resolver.has(Capability.VIEW_CONFIDENTIAL_WORK_ORDERS)
        can_manage_buildings = self._resolver.has(Capability.MANAGE_BUILDINGS)
        return can_view_confidential and not can_manage_buildings

    # ---------- attachments helpers ----------
    def save_attachments(self, work_order: WorkOrder):
        """
        Persist uploaded attachments and delete any that were flagged for removal.
        Returns a dict describing added/removed filenames for audit logging.
        """
        change_log = {"added": [], "removed": []}
        remove_ids = self.cleaned_data.get("remove_attachments", []) if hasattr(self, "cleaned_data") else []
        if remove_ids:
            to_delete = [
                self._attachment_lookup[pk] for pk in remove_ids if pk in self._attachment_lookup
            ]
            for attachment in to_delete:
                name = (attachment.original_name or "").strip()
                if not name and attachment.file:
                    name = Path(attachment.file.name).name
                if name:
                    change_log["removed"].append(name)
                attachment.delete()

        new_files = self.cleaned_data.get("new_attachments", []) if hasattr(self, "cleaned_data") else []
        for uploaded in new_files:
            attachment = WorkOrderAttachment(
                work_order=work_order,
                file=uploaded,
                original_name=getattr(uploaded, "name", ""),
            )
            attachment.save()
            name = (attachment.original_name or "").strip()
            if not name and attachment.file:
                name = Path(attachment.file.name).name
            if name:
                change_log["added"].append(name)
        return change_log

    def _compute_allowed_statuses(self, building_id, *, current_status=None, can_user_approve: bool = False):
        current = current_status or (self.instance.status if self.instance.pk else WorkOrder.Status.OPEN)
        if building_id is None:
            return {WorkOrder.Status.OPEN}
        can_manage = False
        can_create = False
        if self._resolver:
            can_manage = self._resolver.has(Capability.MANAGE_BUILDINGS, building_id=building_id)
            can_create = self._resolver.has(Capability.CREATE_WORK_ORDERS, building_id=building_id)
        if not (can_manage or can_create or can_user_approve):
            return {current}

        transitions = {
            WorkOrder.Status.OPEN: {WorkOrder.Status.OPEN, WorkOrder.Status.IN_PROGRESS, WorkOrder.Status.DONE},
            WorkOrder.Status.IN_PROGRESS: {
                WorkOrder.Status.IN_PROGRESS,
                WorkOrder.Status.AWAITING_APPROVAL,
                WorkOrder.Status.DONE,
            },
            WorkOrder.Status.AWAITING_APPROVAL: {WorkOrder.Status.AWAITING_APPROVAL},
            WorkOrder.Status.DONE: {WorkOrder.Status.DONE},
            WorkOrder.Status.APPROVED: {
                WorkOrder.Status.APPROVED,
                WorkOrder.Status.OPEN,
                WorkOrder.Status.IN_PROGRESS,
                WorkOrder.Status.AWAITING_APPROVAL,
                WorkOrder.Status.DONE,
            },
            WorkOrder.Status.REJECTED: {
                WorkOrder.Status.REJECTED,
                WorkOrder.Status.OPEN,
                WorkOrder.Status.IN_PROGRESS,
                WorkOrder.Status.AWAITING_APPROVAL,
                WorkOrder.Status.DONE,
            },
        }
        allowed = transitions.get(current, {current})
        if current == WorkOrder.Status.AWAITING_APPROVAL:
            if can_user_approve:
                return {WorkOrder.Status.APPROVED, WorkOrder.Status.REJECTED}
            return allowed
        if can_user_approve:
            allowed |= {
                WorkOrder.Status.OPEN,
                WorkOrder.Status.IN_PROGRESS,
                WorkOrder.Status.AWAITING_APPROVAL,
                WorkOrder.Status.DONE,
            }
        return allowed

    def _build_approver_queryset(self, building: Building | None):
        if not building:
            return User.objects.none()
        user_ids: set[int] = set()
        user_ids.update(
            BuildingMembership.objects.filter(
                building=building,
                role=MembershipRole.BACKOFFICE,
            ).values_list("user_id", flat=True)
        )
        user_ids.update(
            BuildingMembership.objects.filter(
                building__isnull=True,
                role=MembershipRole.ADMINISTRATOR,
            ).values_list("user_id", flat=True)
        )
        # include owner only if they have an approver role (non-technician)
        if building.owner_id:
            owner_roles = list(
                BuildingMembership.objects.filter(
                    building=building,
                    user_id=building.owner_id,
                ).values_list("role", flat=True)
            )
            if owner_roles and MembershipRole.TECHNICIAN not in owner_roles:
                user_ids.add(building.owner_id)
        user_ids.discard(None)
        if not user_ids:
            return User.objects.none()
        return User.objects.filter(pk__in=user_ids, is_active=True).order_by(Lower("username"))


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
    priority = forms.ChoiceField(
        choices=WorkOrder.Priority.choices,
        initial=WorkOrder.Priority.LOW,
        label=_("Priority"),
        widget=forms.Select(attrs={"class": "input"}),
    )
    deadline = forms.DateField(
        label=_("Deadline"),
        widget=forms.DateInput(attrs={"type": "date", "class": "input"}),
    )
    def __init__(self, *args, buildings_queryset=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._user = user

        qs = buildings_queryset or Building.objects.none()
        field = self.fields["buildings"]
        field.queryset = qs
        field.widget.attrs.setdefault("class", "space-y-2")

        if qs.exists():
            if not self.is_bound:
                field.initial = list(qs.values_list("pk", flat=True))
        else:
            field.disabled = True

        if not self.is_bound:
            default_deadline = timezone.localdate() + timedelta(days=14)
            self.fields["deadline"].initial = default_deadline

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


class BuildingMembershipForm(forms.Form):
    allowed_roles = {
        MembershipRole.TECHNICIAN,
        MembershipRole.BACKOFFICE,
        MembershipRole.LAWYER,
    }

    def __init__(self, *args, building=None, **kwargs):
        self._building = building
        super().__init__(*args, **kwargs)

        role_choices = [
            (value, label)
            for value, label in MembershipRole.choices
            if value in self.allowed_roles
        ]
        self.fields["role"] = forms.ChoiceField(
            choices=[("", _("Select a role"))] + role_choices,
            label=_("Role"),
            required=True,
        )
        role_field = self.fields["role"]

        eligible_users_qs = (
            User.objects.filter(
                memberships__role__in=self.allowed_roles,
                is_active=True,
            )
            .distinct()
            .order_by(Lower("username"))
        )
        self._eligible_user_ids = list(eligible_users_qs.values_list("pk", flat=True))
        role_display_lookup = dict(MembershipRole.choices)
        user_roles_qs = BuildingMembership.objects.filter(
            user_id__in=self._eligible_user_ids,
            building__isnull=True,
            role__in=self.allowed_roles,
        ).order_by("user_id")
        self._user_role_map: dict[str, list[str]] = {}
        self._user_role_labels: dict[str, list[str]] = {}
        for membership in user_roles_qs:
            key = str(membership.user_id)
            role_code = membership.role
            role_label = role_display_lookup.get(role_code, role_code)
            self._user_role_map.setdefault(key, [])
            self._user_role_labels.setdefault(key, [])
            if role_code not in self._user_role_map[key]:
                self._user_role_map[key].append(role_code)
            if role_label not in self._user_role_labels[key]:
                self._user_role_labels[key].append(role_label)

        user_field = forms.ModelMultipleChoiceField(
            queryset=eligible_users_qs,
            label=_("Users"),
            widget=RoleAwareCheckboxSelect(
                role_map=self._user_role_map,
                role_labels=self._user_role_labels,
            ),
            required=False,
        )

        if self._eligible_user_ids:
            def _label(user):
                full_name = user.get_full_name()
                if full_name:
                    return f"{full_name} ({user.username})"
                return user.username

            user_field.label_from_instance = _label
            if building is not None:
                user_field.help_text = _(
                    "Select one or more users after choosing a role above."
                )
        else:
            user_field.disabled = True
            user_field.help_text = _(
                "No technician, backoffice, or lawyer users available. Create them first from the Users section."
            )
        self.fields["user"] = user_field

        self.technician_subrole_choices = list(Building.Role.choices)
        self.selected_subroles: dict[str, str] = {}
        if self.is_bound:
            for user_id in self._eligible_user_ids:
                key = str(user_id)
                field_name = self.subrole_field_name(user_id)
                value = (self.data.get(field_name) or "").strip()
                if value:
                    self.selected_subroles[key] = value

    def subrole_field_name(self, user_id: int) -> str:
        return self.add_prefix(f"subrole_user_{user_id}")

    def clean(self):
        cleaned = super().clean()
        users = cleaned.get("user")
        role = cleaned.get("role")

        if not role or role not in self.allowed_roles:
            self.add_error("role", _("Select a role."))
            return cleaned

        if not users:
            self.add_error("user", _("Select at least one user."))
            return cleaned

        role_label_lookup = dict(MembershipRole.choices)
        for user in users:
            key = str(user.pk)
            user_roles = self._user_role_map.get(key, [])
            if role not in (user_roles or []):
                self.add_error(
                    "user",
                    _("All selected users must have the %(role)s role.") % {
                        "role": role_label_lookup.get(role, role)
                    },
                )
                return cleaned

        subrole_map: dict[int, str] = {}
        if role == MembershipRole.TECHNICIAN:
            valid_values = {value for value, _ in self.technician_subrole_choices}
            for user in users:
                key = str(user.pk)
                field_name = self.subrole_field_name(user.pk)
                subrole_value = (self.data.get(field_name) or "").strip()
                if subrole_value not in valid_values:
                    self.add_error(
                        "user",
                        _("Select a sub-role for %(user)s.") % {
                            "user": user.get_full_name() or user.username
                        },
                    )
                else:
                    subrole_map[user.pk] = subrole_value
        cleaned["technician_subroles_map"] = subrole_map

        if self._building and users:
            duplicates = BuildingMembership.objects.filter(
                building=self._building,
                user__in=users,
                role=role,
            ).select_related("user")
            if duplicates.exists():
                names = ", ".join(
                    membership.user.get_full_name() or membership.user.username
                    for membership in duplicates
                )
                self.add_error(
                    "user",
                    _("The following users already have this role: %(names)s") % {"names": names},
                )
        return cleaned

    def save(self, commit: bool = True):
        if not hasattr(self, "cleaned_data"):
            raise ValueError("Form must be validated before calling save().")
        memberships: list[BuildingMembership] = []
        users = self.cleaned_data.get("user") or []
        if hasattr(users, "all"):
            users = list(users)
        role = self.cleaned_data.get("role")
        subrole_map: dict[int, str] = self.cleaned_data.get("technician_subroles_map", {})
        building = self._building

        for user in users:
            membership = BuildingMembership(
                user=user,
                building=building,
                role=role,
                technician_subrole=subrole_map.get(user.pk, ""),
            )
            if commit:
                membership.save()
            memberships.append(membership)
        return memberships


class TechnicianSubroleForm(forms.ModelForm):
    class Meta:
        model = BuildingMembership
        fields = ("technician_subrole",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["technician_subrole"].choices = Building.Role.choices
        self.fields["technician_subrole"].label = _("Sub-role")

    def clean(self):
        cleaned = super().clean()
        if self.instance.role != MembershipRole.TECHNICIAN:
            raise forms.ValidationError(_("Only technician memberships can set a sub-role."))
        if not cleaned.get("technician_subrole"):
            self.add_error("technician_subrole", _("Select a sub-role."))
        return cleaned


# -----------------------------
# Users (admin area)
# -----------------------------

_USER_IS_ACTIVE_FIELD = User._meta.get_field("is_active")
_USER_SUPERUSER_FIELD = User._meta.get_field("is_superuser")


def _global_membership_for(user):
    if not getattr(user, "pk", None):
        return None
    return BuildingMembership.objects.filter(user=user, building__isnull=True).first()


def _initial_roles_for(user):
    memberships = (
        BuildingMembership.objects.filter(user=user, building__isnull=True)
        if getattr(user, "pk", None)
        else []
    )
    roles = [membership.role for membership in memberships]
    if roles:
        return roles
    if getattr(user, "is_superuser", False):
        return [MembershipRole.ADMINISTRATOR]
    return [MembershipRole.BACKOFFICE]


def _apply_user_roles(user, roles: list[str]):
    roles = sorted(set(roles or []))
    if not roles:
        roles = [MembershipRole.BACKOFFICE]
    user.is_superuser = MembershipRole.ADMINISTRATOR in roles
    user.is_staff = user.is_superuser
    user.save(update_fields=["is_superuser", "is_staff"])
    existing = BuildingMembership.objects.filter(user=user, building__isnull=True)
    existing_roles = {membership.role: membership for membership in existing}
    for role in roles:
        if role in existing_roles:
            continue
        BuildingMembership.objects.create(user=user, building=None, role=role)
    existing.exclude(role__in=roles).delete()


class AdminUserCreateForm(RoleSelectionMixin, UserCreationForm):
    email = forms.EmailField(required=False, label=_("Email"))
    first_name = forms.CharField(required=False, label=_("First name"))
    last_name = forms.CharField(required=False, label=_("Last name"))
    is_active = forms.BooleanField(required=False, initial=True)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("is_superuser", None)
        self.fields["roles"] = forms.MultipleChoiceField(
            choices=MembershipRole.choices,
            label=_("Roles"),
            initial=_initial_roles_for(self.instance),
            widget=forms.CheckboxSelectMultiple(attrs={"class": "space-y-2"}),
        )
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

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get("email", "")
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.is_active = self.cleaned_data.get("is_active", False)
        if commit:
            user.save()
            self.save_m2m()
            profile, created = UserSecurityProfile.objects.get_or_create(user=user)
            profile.reset()
            _apply_user_roles(user, self.cleaned_data.get("roles", []))
        return user


class AdminUserUpdateForm(RoleSelectionMixin, UserChangeForm):
    password = None  # hide the unusable password hash field

    class Meta(UserChangeForm.Meta):
        model = User
        fields = (
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("is_superuser", None)
        self.fields["roles"] = forms.MultipleChoiceField(
            choices=MembershipRole.choices,
            label=_("Roles"),
            initial=_initial_roles_for(self.instance),
            widget=forms.CheckboxSelectMultiple(attrs={"class": "space-y-2"}),
        )
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "checkbox")
            else:
                widget.attrs.setdefault("class", "input")

    def save(self, commit=True):
        user = super().save(commit=False)
        roles = self.cleaned_data.get("roles", _initial_roles_for(user))
        user.is_staff = user.is_superuser
        if commit:
            user.save()
            self.save_m2m()
            profile, created = UserSecurityProfile.objects.get_or_create(user=user)
            if user.is_active:
                profile.reset()
            _apply_user_roles(user, roles)
        return user


class AdminUserPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "input")
