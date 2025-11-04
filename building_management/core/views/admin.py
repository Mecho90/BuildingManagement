from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views.generic import CreateView, DeleteView, FormView, ListView, UpdateView

from ..forms import AdminUserCreateForm, AdminUserPasswordForm, AdminUserUpdateForm
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
        paginator = ctx.get("paginator")
        if paginator is not None:
            total_users = paginator.count
        else:
            object_list = ctx.get("object_list")
            total_users = len(object_list) if object_list is not None else 0
        ctx["users_total"] = total_users
        return ctx


class AdminUserCreateView(LoginRequiredMixin, AdminRequiredMixin, CreateView):
    model = User
    form_class = AdminUserCreateForm
    template_name = "core/user_form.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("User created."))
        return response

    def get_success_url(self):
        return reverse("users_list")


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
        return reverse("users_list")


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
        return reverse("users_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["managed_user"] = self.user_obj
        return ctx


class AdminUserDeleteView(LoginRequiredMixin, AdminRequiredMixin, DeleteView):
    model = User
    template_name = "core/user_confirm_delete.html"
    success_url = reverse_lazy("users_list")

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
