"""Todo history and archival helpers."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext as _, ngettext

from ..models import TodoItem, TodoWeekSnapshot, start_of_week
from .notifications import NotificationPayload


class TodoHistoryService:
    def __init__(self, item: TodoItem):
        self.item = item

    def _base_snapshot_data(self) -> dict:
        return {
            "user": self.item.user,
            "todo_item": self.item,
            "week_start": start_of_week(self.item.completed_at.date() if self.item.completed_at else self.item.week_start),
            "title": self.item.title,
            "description": self.item.description,
            "due_date": self.item.due_date,
            "completed_at": self.item.completed_at or timezone.now(),
            "metadata": {
                "status": self.item.status,
                "week_start": self.item.week_start.isoformat(),
            },
        }

    def ensure_snapshot(self):
        if not self.item.completed_at:
            return None
        data = self._base_snapshot_data()
        snapshot, created = TodoWeekSnapshot.objects.get_or_create(
            todo_item=self.item,
            week_start=data["week_start"],
            defaults=data,
        )
        if not created:
            for field, value in data.items():
                setattr(snapshot, field, value)
            snapshot.is_active = True
            snapshot.reopened_at = None
            snapshot.save()
        return snapshot

    def mark_reopened(self):
        snapshots = self.item.week_snapshots.filter(is_active=True)
        count = snapshots.count()
        if count:
            snapshots.update(is_active=False, reopened_at=timezone.now())
        return count

    def handle_status_change(self, previous_status: str, actor=None):
        if self.item.status == TodoItem.Status.DONE:
            self.ensure_snapshot()
        elif previous_status == TodoItem.Status.DONE:
            self.mark_reopened()


class TodoArchiveService:
    def __init__(self, *, weeks_to_keep: int = 4, today=None):
        self.weeks_to_keep = weeks_to_keep
        self.today = today or timezone.localdate()

    def _cutoff_date(self):
        return start_of_week(self.today) - timedelta(weeks=self.weeks_to_keep)

    def archive_completed(self):
        cutoff = self._cutoff_date()
        qs = TodoItem.objects.filter(
            status=TodoItem.Status.DONE,
            week_start__lt=cutoff,
        )
        updated = qs.update(status=TodoItem.Status.ARCHIVED)
        return updated

    def prune_archived_snapshots(self):
        cutoff = self._cutoff_date()
        snapshots = TodoWeekSnapshot.objects.filter(
            week_start__lt=cutoff,
            is_active=False,
        )
        deleted, _ = snapshots.delete()
        return deleted


class TodoReminderService:
    def __init__(self, user):
        self.user = user

    def _pending_queryset(self):
        return TodoItem.objects.filter(
            user=self.user,
            status__in=[TodoItem.Status.PENDING, TodoItem.Status.IN_PROGRESS],
        )

    def build_digest(self, *, today=None):
        if not self.user or not self.user.is_authenticated:
            return None
        today = today or timezone.localdate()
        week_start = start_of_week(today)
        qs = self._pending_queryset()
        due_today = qs.filter(due_date=today).count()
        overdue = qs.filter(due_date__lt=today).count()
        this_week = qs.filter(week_start=week_start).count()
        if not any([due_today, overdue, this_week]):
            return None

        parts = []
        if overdue:
            parts.append(
                ngettext(
                    "%(count)s task overdue",
                    "%(count)s tasks overdue",
                    overdue,
                )
                % {"count": overdue}
            )
        if due_today:
            parts.append(
                ngettext(
                    "%(count)s task due today",
                    "%(count)s tasks due today",
                    due_today,
                )
                % {"count": due_today}
            )
        if this_week:
            parts.append(
                ngettext(
                    "%(count)s task scheduled this week",
                    "%(count)s tasks scheduled this week",
                    this_week,
                )
                % {"count": this_week}
            )

        body = ", ".join(parts)
        return NotificationPayload(
            key="todo-digest",
            category="todo",
            title=_("To-do digest"),
            body=body,
            level="info" if not overdue else "warning",
        )
