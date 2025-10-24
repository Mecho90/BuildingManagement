# path: building_mgmt/urls.py
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # Admin
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),

    # Auth
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            redirect_authenticated_user=True,  # if already logged in, skip to buildings
        ),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(next_page="login"),
        name="logout",
    ),

    # App routes (no namespace so plain names work, but not required by this file)
    path("", include("core.urls")),

    # Root -> literal URL instead of reversing a name (avoids NoReverseMatch)
    path("", RedirectView.as_view(url="/buildings/", permanent=False)),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
