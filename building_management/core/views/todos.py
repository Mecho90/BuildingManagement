from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView, CreateView, UpdateView, DeleteView

from ..models import TodoActivity, TodoItem, start_of_week
from ..forms import TodoItemForm
from .common import _safe_next_url


def _user_can_filter_owner(user):
    # To-Do planner is personal for all roles.
    return False


class TodoListPageView(LoginRequiredMixin, TemplateView):
    template_name = "core/todos.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        week_start = start_of_week()
        next_week = week_start + timedelta(days=7)
        today = timezone.localdate()
        page_sizes = [25, 50, 100, 200]
        owner_filter_enabled = _user_can_filter_owner(self.request.user)
        owner_filter_default = str(self.request.user.pk) if owner_filter_enabled else ""
        owner_filter_options: list[dict[str, str]] = []
        config = {
            "apiUrl": reverse("core:api_todos"),
            "detailUrl": reverse("core:api_todo_detail", args=[0]).replace("/0/", "/{id}/"),
            "editUrl": reverse("core:todo_edit", args=[0]).replace("/0/", "/{id}/"),
            "deleteUrl": reverse("core:todo_delete", args=[0]).replace("/0/", "/{id}/"),
            "listUrl": reverse("core:todo_list"),
            "icsUrl": reverse("core:todo_ics_feed"),
            "calendarUrl": reverse("core:api_todo_calendar"),
            "completedClearUrl": reverse("core:api_todo_completed_clear"),
            "summaryUrl": reverse("core:api_todo_summary"),
            "currentWeek": week_start.isoformat(),
            "nextWeek": next_week.isoformat(),
            "today": today.isoformat(),
            "statusLabels": {key: label for key, label in TodoItem.Status.choices},
            "locale": translation.get_language() or "en",
            "createUrl": reverse("core:todo_create"),
            "defaultPageSize": page_sizes[0],
            "pageSizeOptions": page_sizes,
            "currentUserId": self.request.user.pk,
            "hideCompletedTab": False,
            "ownerFilterDefault": owner_filter_default,
            "ownerOptions": owner_filter_options,
            "canAssignOwner": owner_filter_enabled,
        }
        task_qs = TodoItem.objects.visible_to(self.request.user)
        task_qs = task_qs.filter(user=self.request.user).exclude(status=TodoItem.Status.DONE)

        ctx.update(
            {
                "page_title": _("Weekly To-Do Planner"),
                "current_week": week_start,
                "next_week": next_week,
                "status_choices": TodoItem.Status.choices,
                "todo_config": config,
                "total_tasks": task_qs.count(),
                "page_size_options": page_sizes,
                "show_completed_tab": True,
                "owner_filter_options": owner_filter_options,
                "owner_filter_default": owner_filter_default,
            }
        )
        return ctx


class TodoCreateView(LoginRequiredMixin, CreateView):
    model = TodoItem
    form_class = TodoItemForm
    template_name = "core/todo_form.html"

    def get_initial(self):
        initial = super().get_initial()
        due = self.request.GET.get("due")
        if due:
            initial["due_date"] = due
        return initial

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("Task created."))
        return response

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        return next_url or reverse("core:todo_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        default_target = reverse("core:todo_list")
        next_target = _safe_next_url(self.request) or default_target
        ctx["cancel_url"] = next_target
        ctx["next_url"] = next_target
        ctx["form_title"] = _("New Task")
        return ctx


class TodoUpdateView(LoginRequiredMixin, UpdateView):
    model = TodoItem
    form_class = TodoItemForm
    template_name = "core/todo_form.html"

    def get_queryset(self):
        return TodoItem.objects.visible_to(self.request.user).filter(user=self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        return next_url or reverse("core:todo_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        default_target = reverse("core:todo_list")
        next_target = _safe_next_url(self.request) or default_target
        ctx["cancel_url"] = next_target
        ctx["next_url"] = next_target
        ctx["form_title"] = _("Edit Task")
        return ctx

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.warning(self.request, _("Task updated."))
        return response


class TodoDeleteView(LoginRequiredMixin, DeleteView):
    model = TodoItem
    template_name = "core/todo_confirm_delete.html"

    def get_queryset(self):
        return TodoItem.objects.visible_to(self.request.user).filter(user=self.request.user)

    def get_success_url(self):
        return reverse("core:todo_list")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        default_target = reverse("core:todo_list")
        next_target = _safe_next_url(self.request) or default_target
        ctx["cancel_url"] = next_target
        ctx["next_url"] = next_target
        return ctx

    def post(self, request, *args, **kwargs):
        messages.error(request, _("Task deleted."))
        return super().post(request, *args, **kwargs)
