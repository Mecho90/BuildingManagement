from __future__ import annotations

from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def toggle_theme(request: HttpRequest):
    current = request.session.get("theme", "light")
    request.session["theme"] = "dark" if current == "light" else "light"
    referer = request.META.get("HTTP_REFERER")
    if referer and url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return HttpResponseRedirect(referer)
    return redirect("buildings_list")
