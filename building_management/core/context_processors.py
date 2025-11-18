from __future__ import annotations

from django.urls import NoReverseMatch, reverse


def theme(request):
    data = {
        "theme": request.session.get("theme", "light"),
        "work_orders_enabled": False,
        "work_orders_url": "",
        "work_orders_archive_enabled": False,
        "work_orders_archive_url": "",
        "mass_assign_work_orders_enabled": False,
        "mass_assign_work_orders_url": "",
    }

    user = getattr(request, "user", None)
    if user and user.is_authenticated:
        try:
            data["work_orders_url"] = reverse("core:work_orders_list")
            data["work_orders_enabled"] = True
        except NoReverseMatch:
            pass

        if user.is_staff or user.is_superuser:
            try:
                data["work_orders_archive_url"] = reverse("core:work_orders_archive")
                data["work_orders_archive_enabled"] = True
            except NoReverseMatch:
                pass
        if user.is_superuser:
            try:
                data["mass_assign_work_orders_url"] = reverse("core:work_orders_mass_assign")
                data["mass_assign_work_orders_enabled"] = True
            except NoReverseMatch:
                pass
    return data
