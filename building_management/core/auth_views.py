from django.contrib.auth import logout, get_user_model
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from .models import UserSecurityProfile


class RoleAwareLoginView(LoginView):
    redirect_authenticated_user = False
    lock_threshold = 5

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("core:buildings_list")
        return super().get(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        profile, created = UserSecurityProfile.objects.get_or_create(user=user)
        if profile.failed_login_attempts or profile.lock_reason:
            profile.reset()
        return super().form_valid(form)

    def form_invalid(self, form):
        self._handle_failed_attempt(form)
        return super().form_invalid(form)

    def _handle_failed_attempt(self, form):
        username = (form.data.get("username") or "").strip()
        if not username:
            return
        UserModel = get_user_model()
        username_field = UserModel.USERNAME_FIELD
        try:
            user = UserModel.objects.get(**{f"{username_field}__iexact": username})
        except UserModel.DoesNotExist:
            return

        profile, created = UserSecurityProfile.objects.get_or_create(user=user)

        if not user.is_active:
            if profile.lock_reason == UserSecurityProfile.LockReason.FAILED_ATTEMPTS:
                form.add_error(
                    None,
                    _(
                        "Your account has been locked after too many failed attempts. Contact an administrator."
                    ),
                )
            elif not profile.lock_reason:
                profile.lock_reason = UserSecurityProfile.LockReason.MANUAL
                profile.save(update_fields=["lock_reason"])
            return

        profile.failed_login_attempts += 1
        updates = ["failed_login_attempts"]
        locked_now = False

        if profile.failed_login_attempts >= self.lock_threshold:
            user.is_active = False
            user.save(update_fields=["is_active"])
            profile.locked_at = timezone.now()
            profile.lock_reason = UserSecurityProfile.LockReason.FAILED_ATTEMPTS
            updates.extend(["locked_at", "lock_reason"])
            locked_now = True

        profile.save(update_fields=updates)

        if locked_now:
            form.add_error(
                None,
                _(
                    "Your account has been locked after too many failed attempts. Contact an administrator."
                ),
            )

    def get_success_url(self):
        url = self.get_redirect_url()
        if url:
            return url
        return reverse("core:buildings_list")

@require_http_methods(["GET", "POST"])  # allow link-clicks and form posts
def logout_to_login(request):
    logout(request)
    return redirect("login")
