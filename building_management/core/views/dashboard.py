from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View


class DashboardView(LoginRequiredMixin, View):
    """
    Root landing page for authenticated users.

    Right now this simply redirects to the buildings list, but keeping it as a
    dedicated view leaves room for role-based dashboards later.
    """

    default_redirect_name = "core:buildings_list"

    def get(self, request, *args, **kwargs):
        return redirect(self.get_redirect_url())

    def get_redirect_url(self) -> str:
        return reverse(self.default_redirect_name)
