from __future__ import annotations

from collections import defaultdict

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views.generic import CreateView, DeleteView, FormView, ListView, UpdateView

from ..forms import AdminUserCreateForm, AdminUserPasswordForm, AdminUserUpdateForm
from ..models import WorkOrder
from .common import AdminRequiredMixin, _querystring_without

User = get_user_model()


__all__ = [
    "AdminUserListView",
    "AdminUserCreateView",
    "AdminUserUpdateView",
    "AdminUserPasswordView",
    "AdminUserDeleteView",
]

class AdminUserListView(LoginRequiredMixin, AdminRequiredMixin, ListView):
    model = User
    template_name = "core/users_list.html"
    context_object_name = "users"
    paginate_by = 20

    def get_queryset(self):
        qs = User.objects.all().order_by("username")
        search = (self.request.GET.get("q") or "").strip()
        if search:
            qs = qs.filter(
                Q(username__icontains=search)
                | Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        self._search = search
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = getattr(self, "_search", "")
        ctx["pagination_query"] = _querystring_without(self.request, "page")
        object_list = list(ctx.get("object_list") or [])
        overview_data = self._build_overview(object_list)
        if overview_data:
            ctx["overview_rows"] = overview_data["rows"]
            ctx["overview_totals"] = overview_data["totals"]
        paginator = ctx.get("paginator")
        if paginator is not None:
            ctx["users_total"] = paginator.count
        else:
            ctx["users_total"] = len(object_list)
        return ctx

    def _build_overview(self, users) -> dict[str, object] | None:
        if not users:
            return None

        user_ids = [user.pk for user in users if user.pk is not None]
        if not user_ids:
            return None

        def _empty_stats():
            return {
                "buildings": 0,
                "units": 0,
                "priority_high": 0,
                "priority_medium": 0,
                "priority_low": 0,
                "archived": 0,
            }

        aggregated_stats = (
            User.objects.filter(pk__in=user_ids)
            .annotate(
                buildings_total=Count("buildings", distinct=True),
                units_total=Count("buildings__units", distinct=True),
                priority_high_total=Count(
                    "buildings__work_orders",
                    filter=Q(
                        buildings__work_orders__archived_at__isnull=True,
                        buildings__work_orders__priority=WorkOrder.Priority.HIGH,
                    ),
                    distinct=True,
                ),
                priority_medium_total=Count(
                    "buildings__work_orders",
                    filter=Q(
                        buildings__work_orders__archived_at__isnull=True,
                        buildings__work_orders__priority=WorkOrder.Priority.MEDIUM,
                    ),
                    distinct=True,
                ),
                priority_low_total=Count(
                    "buildings__work_orders",
                    filter=Q(
                        buildings__work_orders__archived_at__isnull=True,
                        buildings__work_orders__priority=WorkOrder.Priority.LOW,
                    ),
                    distinct=True,
                ),
                archived_total=Count(
                    "buildings__work_orders",
                    filter=Q(
                        buildings__work_orders__archived_at__isnull=False,
                    ),
                    distinct=True,
                ),
            )
            .values(
                "pk",
                "buildings_total",
                "units_total",
                "priority_high_total",
                "priority_medium_total",
                "priority_low_total",
                "archived_total",
            )
        )

        stats = {
            row["pk"]: {
                "buildings": row["buildings_total"],
                "units": row["units_total"],
                "priority_high": row["priority_high_total"],
                "priority_medium": row["priority_medium_total"],
                "priority_low": row["priority_low_total"],
                "archived": row["archived_total"],
            }
            for row in aggregated_stats
        }

        totals = defaultdict(int)
        overview_rows: list[dict[str, object]] = []
        for user in users:
            user_stats = stats.get(user.pk)
            if not user_stats:
                user_stats = _empty_stats()
            for key, value in user_stats.items():
                totals[key] += value
            overview_rows.append(
                {
                    "user": user,
                    "is_admin": user.is_superuser,
                    **user_stats,
                }
            )

        return {"rows": overview_rows, "totals": dict(totals)}


class AdminUserCreateView(LoginRequiredMixin, AdminRequiredMixin, CreateView):
    model = User
    form_class = AdminUserCreateForm
    template_name = "core/user_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("User created."))
        return response

    def get_success_url(self):
        return reverse("core:users_list")


class AdminUserUpdateView(LoginRequiredMixin, AdminRequiredMixin, UpdateView):
    model = User
    form_class = AdminUserUpdateForm
    template_name = "core/user_form.html"
    context_object_name = "managed_user"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.warning(self.request, _("User updated."))
        return response

    def get_success_url(self):
        return reverse("core:users_list")


class AdminUserPasswordView(LoginRequiredMixin, AdminRequiredMixin, FormView):
    template_name = "core/user_password_form.html"
    form_class = AdminUserPasswordForm

    def dispatch(self, request, *args, **kwargs):
        self.user_obj = get_object_or_404(User, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.user_obj
        return kwargs

    def form_valid(self, form):
        form.save()
        messages.success(self.request, _("Password reset successfully."))
        return HttpResponseRedirect(self.get_success_url())

    def get_success_url(self):
        return reverse("core:users_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["managed_user"] = self.user_obj
        return ctx


class AdminUserDeleteView(LoginRequiredMixin, AdminRequiredMixin, DeleteView):
    model = User
    template_name = "core/user_confirm_delete.html"
    success_url = reverse_lazy("core:users_list")

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.pk == request.user.pk:
            messages.error(request, _("You cannot delete your own account."))
            return HttpResponseRedirect(self.success_url)
        messages.error(request, _("User deleted."))
        return super().post(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        # Ensure we destroy the object after messaging in post()
        return super().delete(request, *args, **kwargs)
