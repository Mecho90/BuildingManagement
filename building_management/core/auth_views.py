from django.contrib.auth.views import LoginView
from django.contrib.auth import logout
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

class RoleAwareLoginView(LoginView):
    redirect_authenticated_user = False
    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("buildings_list")
        return super().get(request, *args, **kwargs)
    def get_success_url(self):
        url = self.get_redirect_url()
        if url:
            return url
        if self.request.user.is_superuser:
            return reverse("admin:index")
        return reverse("buildings_list")

@require_http_methods(["GET", "POST"])  # allow link-clicks and form posts
def logout_to_login(request):
    logout(request)
    return redirect("login")