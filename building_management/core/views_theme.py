from __future__ import annotations

from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse


def toggle_theme(request: HttpRequest):
    current = request.session.get("theme", "light")
    request.session["theme"] = "dark" if current == "light" else "light"
    referer = request.META.get("HTTP_REFERER")
    if referer:
        return HttpResponseRedirect(referer)
    return redirect("buildings_list")
