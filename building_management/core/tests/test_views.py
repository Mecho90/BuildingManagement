from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.auth_views import RoleAwareLoginView
from core.models import (
    Building,
    BuildingMembership,
    MembershipRole,
    Notification,
    RoleAuditLog,
    Unit,
    WorkOrder,
    WorkOrderAuditLog,
    UserSecurityProfile,
)
from core.services import NotificationService


class WorkOrderArchiveViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="archive-owner",
            email="archive@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.user,
            name="Archive Plaza",
            address="123 Archive St",
        )
        self.unit = Unit.objects.create(
            building=self.building,
            number="A-1",
        )

    def test_archive_done_work_order(self):
        work_order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Ready for archive",
            status=WorkOrder.Status.DONE,
            priority=WorkOrder.Priority.LOW,
            deadline=timezone.localdate(),
        )

        self.client.login(username="archive-owner", password="pass1234")
        response = self.client.post(
            reverse("core:work_order_archive", args=[work_order.pk]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        work_order.refresh_from_db()
        self.assertIsNotNone(work_order.archived_at)

    def test_archive_rejects_non_done_work_order(self):
        work_order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Still open",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate(),
        )

        self.client.login(username="archive-owner", password="pass1234")
        response = self.client.post(reverse("core:work_order_archive", args=[work_order.pk]))
        self.assertEqual(response.status_code, 404)
        work_order.refresh_from_db()
        self.assertIsNone(work_order.archived_at)

    def test_archive_approved_work_order(self):
        work_order = WorkOrder.objects.create(
            building=self.building,
            unit=self.unit,
            title="Approved order",
            status=WorkOrder.Status.APPROVED,
            priority=WorkOrder.Priority.LOW,
            deadline=timezone.localdate(),
        )

        self.client.login(username="archive-owner", password="pass1234")
        response = self.client.post(
            reverse("core:work_order_archive", args=[work_order.pk]),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        work_order.refresh_from_db()
        self.assertIsNotNone(work_order.archived_at)


class WorkOrderApprovalDecisionViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(
            username="approvals-owner",
            email="approvals-owner@example.com",
            password="pass1234",
        )
        self.backoffice = User.objects.create_user(
            username="approvals-bo",
            email="approvals-bo@example.com",
            password="pass1234",
        )
        self.tech = User.objects.create_user(
            username="approvals-tech",
            email="approvals-tech@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.owner,
            name="Approval Plaza",
            address="789 Approve St",
        )
        BuildingMembership.objects.create(
            user=self.backoffice,
            building=self.building,
            role=MembershipRole.BACKOFFICE,
        )
        BuildingMembership.objects.create(
            user=self.tech,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        self.work_order = WorkOrder.objects.create(
            building=self.building,
            title="Need approval",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=2),
            replacement_request_note="Need budget approval",
        )

    def _url(self):
        return reverse("core:work_order_approval_decide", args=[self.work_order.pk])

    def test_requires_login(self):
        response = self.client.post(self._url(), {"decision": "approve"})
        self.assertEqual(response.status_code, 302)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.status, WorkOrder.Status.AWAITING_APPROVAL)

    def test_backoffice_can_approve(self):
        self.client.login(username="approvals-bo", password="pass1234")
        response = self.client.post(
            self._url(),
            {"decision": "approve", "next": reverse("core:dashboard")},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.status, WorkOrder.Status.APPROVED)
        log = WorkOrderAuditLog.objects.filter(
            work_order=self.work_order,
            action=WorkOrderAuditLog.Action.APPROVAL,
        ).first()
        self.assertIsNotNone(log)

    def test_backoffice_can_reject(self):
        self.client.login(username="approvals-bo", password="pass1234")
        response = self.client.post(
            self._url(),
            {"decision": "reject", "next": reverse("core:dashboard")},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.status, WorkOrder.Status.REJECTED)
        log = WorkOrderAuditLog.objects.filter(
            work_order=self.work_order,
            action=WorkOrderAuditLog.Action.STATUS_CHANGED,
        ).first()
        self.assertIsNotNone(log)

    def test_invalid_decision_shows_error(self):
        self.client.login(username="approvals-bo", password="pass1234")
        response = self.client.post(
            self._url(),
            {"decision": "maybe", "next": reverse("core:dashboard")},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.status, WorkOrder.Status.AWAITING_APPROVAL)

    def test_requires_capability(self):
        self.client.login(username="approvals-tech", password="pass1234")
        response = self.client.post(
            self._url(),
            {"decision": "approve", "next": reverse("core:dashboard")},
        )
        self.assertEqual(response.status_code, 404)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.status, WorkOrder.Status.AWAITING_APPROVAL)

    def test_requires_awaiting_status(self):
        self.work_order.status = WorkOrder.Status.OPEN
        self.work_order.save(update_fields=["status"])
        self.client.login(username="approvals-bo", password="pass1234")
        response = self.client.post(
            self._url(),
            {"decision": "approve", "next": reverse("core:dashboard")},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.status, WorkOrder.Status.OPEN)


class LoginLockoutTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.username = "lockout-user"
        self.password = "lock-me"
        self.user = User.objects.create_user(
            username=self.username,
            email="lockout@example.com",
            password=self.password,
            is_active=True,
        )

    def test_user_locked_after_threshold(self):
        login_url = reverse("login")
        for _ in range(RoleAwareLoginView.lock_threshold):
            response = self.client.post(
                login_url, {"username": self.username, "password": "wrong"}
            )
            self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        profile = UserSecurityProfile.objects.get(user=self.user)
        self.assertFalse(self.user.is_active)
        self.assertEqual(profile.lock_reason, UserSecurityProfile.LockReason.FAILED_ATTEMPTS)
        self.assertIsNotNone(profile.locked_at)

        response = self.client.post(
            login_url, {"username": self.username, "password": "wrong-again"}
        )
        self.assertContains(
            response,
            "Your account has been locked after too many failed attempts.",
            status_code=200,
        )


class NotificationSnoozeViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="notify-owner",
            email="notify@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.user,
            name="Notify Plaza",
            address="456 Notify St",
        )
        self.today = timezone.localdate()
        self.work_order = WorkOrder.objects.create(
            building=self.building,
            title="Inspect pumps",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.HIGH,
            deadline=self.today + timedelta(days=1),
        )
        service = NotificationService(self.user)
        service.sync_work_order_deadlines(today=self.today)
        self.note_key = f"wo-deadline-{self.work_order.pk}"

    def test_requires_auth(self):
        response = self.client.post(reverse("core:notification_snooze", args=[self.note_key]))
        self.assertEqual(response.status_code, 302)

    def test_snoozes_until_tomorrow(self):
        self.client.login(username="notify-owner", password="pass1234")
        url = reverse("core:notification_snooze", args=[self.note_key])
        response = self.client.post(url, follow=False)
        self.assertEqual(response.status_code, 302)
        note = Notification.objects.get(user=self.user, key=self.note_key)
        self.assertEqual(note.snoozed_until, self.today + timedelta(days=1))

    def test_not_found_returns_404(self):
        self.client.login(username="notify-owner", password="pass1234")
        response = self.client.post(reverse("core:notification_snooze", args=["missing-key"]))
        self.assertEqual(response.status_code, 404)

    def test_mass_assign_dismiss_acknowledges(self):
        mass_order = WorkOrder.objects.create(
            building=self.building,
            title="Mass alert",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=self.today + timedelta(days=3),
            mass_assigned=True,
        )
        service = NotificationService(self.user)
        service.sync_recent_mass_assign(today=self.today)
        key = f"wo-mass-{mass_order.pk}"

        self.client.login(username="notify-owner", password="pass1234")
        response = self.client.post(reverse("core:notification_snooze", args=[key]), follow=False)
        self.assertEqual(response.status_code, 302)

        note = Notification.objects.get(user=self.user, key=key)
        self.assertIsNotNone(note.acknowledged_at)
        self.assertFalse(note.is_active(on=self.today))


class AuditLogViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="audit-admin",
            email="audit-admin@example.com",
            password="pass1234",
        )
        BuildingMembership.objects.create(
            user=self.admin,
            building=None,
            role=MembershipRole.ADMINISTRATOR,
        )
        self.other_user = User.objects.create_user(
            username="audit-target",
            email="audit-target@example.com",
            password="pass1234",
        )
        self.entry = RoleAuditLog.objects.create(
            actor=self.admin,
            target_user=self.other_user,
            role=MembershipRole.TECHNICIAN,
            action=RoleAuditLog.Action.ROLE_ADDED,
            payload={"reason": "test"},
        )

    def test_admin_can_view_audit_log(self):
        self.client.login(username="audit-admin", password="pass1234")
        response = self.client.get(reverse("core:role_audit_log"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Role Audit Log")
        self.assertContains(response, "audit-target")

    def test_non_privileged_user_denied(self):
        self.client.login(username="audit-target", password="pass1234")
        response = self.client.get(reverse("core:role_audit_log"))
        self.assertEqual(response.status_code, 403)


class CapabilityEnforcementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.technician = User.objects.create_user(
            username="tech-user",
            email="tech@example.com",
            password="pass1234",
        )
        self.backoffice = User.objects.create_user(
            username="backoffice-user",
            email="backoffice@example.com",
            password="pass1234",
        )
        self.building = Building.objects.create(
            owner=self.backoffice,
            name="Capability Plaza",
            address="77 Capability Rd",
        )
        self.work_order = WorkOrder.objects.create(
            building=self.building,
            title="Fix plumbing",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=3),
        )
        BuildingMembership.objects.create(
            user=self.technician,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
        )
        BuildingMembership.objects.create(
            user=self.backoffice,
            building=None,
            role=MembershipRole.BACKOFFICE,
        )

    def test_building_create_requires_manage_capability(self):
        self.client.login(username="tech-user", password="pass1234")
        response = self.client.get(reverse("core:building_create"))
        self.assertEqual(response.status_code, 403)

        self.client.login(username="backoffice-user", password="pass1234")
        response = self.client.get(reverse("core:building_create"))
        self.assertEqual(response.status_code, 200)

    def test_mass_assign_requires_capability(self):
        self.client.login(username="tech-user", password="pass1234")
        response = self.client.get(reverse("core:work_orders_mass_assign"))
        self.assertEqual(response.status_code, 403)

        self.client.login(username="backoffice-user", password="pass1234")
        response = self.client.get(reverse("core:work_orders_mass_assign"))
        self.assertEqual(response.status_code, 200)

    def test_archive_requires_admin(self):
        WorkOrder.objects.create(
            building=self.building,
            title="Archived",
            status=WorkOrder.Status.DONE,
            priority=WorkOrder.Priority.LOW,
            deadline=timezone.localdate(),
            archived_at=timezone.now(),
        )
        self.client.login(username="backoffice-user", password="pass1234")
        response = self.client.get(reverse("core:work_orders_archive"))
        self.assertEqual(response.status_code, 403)

        admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "pass1234")
        self.client.login(username="admin", password="pass1234")
        response = self.client.get(reverse("core:work_orders_archive"))
        self.assertEqual(response.status_code, 200)


class DashboardViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.building = Building.objects.create(
            owner=User.objects.create_user("dash-owner", "dash-owner@example.com", "pass1234"),
            name="Dash Plaza",
            address="Dash St",
        )
        self.tech = User.objects.create_user("dash-tech", "dash-tech@example.com", "pass1234")
        BuildingMembership.objects.create(user=self.tech, building=self.building, role=MembershipRole.TECHNICIAN)
        self.backoffice = User.objects.create_user("dash-back", "dash-back@example.com", "pass1234")
        BuildingMembership.objects.create(user=self.backoffice, building=self.building, role=MembershipRole.BACKOFFICE)

    def test_technician_cards_present(self):
        WorkOrder.objects.create(
            building=self.building,
            title="Due today",
            status=WorkOrder.Status.OPEN,
            deadline=timezone.localdate(),
        )
        self.client.login(username="dash-tech", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["technician_cards"])

    def test_backoffice_sees_awaiting(self):
        WorkOrder.objects.create(
            building=self.building,
            title="Awaiting",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            deadline=timezone.localdate() + timedelta(days=2),
            replacement_request_note="Need bulbs",
        )
        WorkOrder.objects.create(
            building=self.building,
            title="Open ticket",
            status=WorkOrder.Status.OPEN,
            deadline=timezone.localdate(),
        )
        self.client.login(username="dash-back", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        cards = response.context["backoffice_cards"]
        load = response.context["assignment_load"]
        self.assertGreaterEqual(len(cards), 1)
        self.assertEqual(load, 1)

    def test_assignment_load_for_technician(self):
        WorkOrder.objects.create(
            building=self.building,
            title="Tech open",
            status=WorkOrder.Status.OPEN,
            deadline=timezone.localdate(),
        )
        self.client.login(username="dash-tech", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["assignment_load"], 1)

    def test_assignment_load_excludes_future_tasks(self):
        WorkOrder.objects.create(
            building=self.building,
            title="Future",
            status=WorkOrder.Status.OPEN,
            deadline=timezone.localdate() + timedelta(days=2),
        )
        self.client.login(username="dash-tech", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.context["assignment_load"], 0)

    def test_admin_my_jobs_scoped_to_memberships(self):
        User = get_user_model()
        admin = User.objects.create_user("dash-admin", "dash-admin@example.com", "pass1234")
        BuildingMembership.objects.create(user=admin, building=None, role=MembershipRole.ADMINISTRATOR)
        BuildingMembership.objects.create(user=admin, building=self.building, role=MembershipRole.TECHNICIAN)
        other_building = Building.objects.create(
            owner=self.building.owner,
            name="Other Plaza",
            address="Elsewhere",
        )
        WorkOrder.objects.create(
            building=self.building,
            title="Allowed",
            status=WorkOrder.Status.OPEN,
            deadline=timezone.localdate(),
        )
        WorkOrder.objects.create(
            building=other_building,
            title="Hidden",
            status=WorkOrder.Status.OPEN,
            deadline=timezone.localdate(),
        )
        self.client.login(username="dash-admin", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        cards = response.context["technician_cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Allowed")

    def test_backoffice_membership_allows_jobs(self):
        User = get_user_model()
        manager = User.objects.create_user("dash-manager", "dash-manager@example.com", "pass1234")
        BuildingMembership.objects.create(user=manager, building=None, role=MembershipRole.ADMINISTRATOR)
        BuildingMembership.objects.create(user=manager, building=self.building, role=MembershipRole.BACKOFFICE)
        WorkOrder.objects.create(
            building=self.building,
            title="Manager should not see",
            status=WorkOrder.Status.OPEN,
            deadline=timezone.localdate(),
        )
        self.client.login(username="dash-manager", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        cards = response.context["technician_cards"]
        self.assertEqual(len(cards), 1)

    def test_admin_sees_owned_buildings(self):
        User = get_user_model()
        owner_admin = User.objects.create_user("owner-admin", "owner-admin@example.com", "pass1234")
        BuildingMembership.objects.create(user=owner_admin, building=None, role=MembershipRole.ADMINISTRATOR)
        owned_building = Building.objects.create(
            owner=owner_admin,
            name="Owned Plaza",
            address="Owned St",
        )
        WorkOrder.objects.create(
            building=owned_building,
            title="Owner task",
            status=WorkOrder.Status.OPEN,
            deadline=timezone.localdate(),
        )
        self.client.login(username="owner-admin", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        cards = response.context["technician_cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Owner task")

    def test_my_jobs_include_pending_approvals(self):
        User = get_user_model()
        approver = User.objects.create_user("dash-approver", "dash-approver@example.com", "pass1234")
        BuildingMembership.objects.create(user=approver, building=None, role=MembershipRole.ADMINISTRATOR)
        other_building = Building.objects.create(
            owner=self.building.owner,
            name="Approval Plaza",
            address="Approve St",
        )
        WorkOrder.objects.create(
            building=other_building,
            title="Needs approval",
            status=WorkOrder.Status.AWAITING_APPROVAL,
            awaiting_approval_by=approver,
            deadline=timezone.localdate() + timedelta(days=2),
        )
        self.client.login(username="dash-approver", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        cards = response.context["technician_cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Needs approval")

    def test_dashboard_shows_notifications(self):
        WorkOrder.objects.create(
            building=self.building,
            title="Deadline soon",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.HIGH,
            deadline=timezone.localdate() + timedelta(days=1),
        )
        self.client.login(username="dash-back", password="pass1234")
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["notifications"])


class MassAssignEnhancedTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.backoffice = User.objects.create_user("ma-back", "ma-back@example.com", "pass1234")
        self.tech = User.objects.create_user("ma-tech", "ma-tech@example.com", "pass1234")
        self.building = Building.objects.create(
            owner=self.backoffice,
            name="Mass One",
            address="1 Mass",
        )
        BuildingMembership.objects.create(user=self.backoffice, building=self.building, role=MembershipRole.BACKOFFICE)
        BuildingMembership.objects.create(user=self.tech, building=self.building, role=MembershipRole.TECHNICIAN)

    def test_mass_assign_custom_deadline(self):
        deadline = timezone.localdate() + timedelta(days=5)
        self.client.login(username="ma-back", password="pass1234")
        response = self.client.post(
            reverse("core:work_orders_mass_assign"),
            data={
                "title": "Bulk",
                "description": "Do tasks",
                "buildings": [self.building.pk],
                "priority": WorkOrder.Priority.HIGH,
                "deadline": deadline.isoformat(),
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        order = WorkOrder.objects.get(title="Bulk")
        self.assertEqual(order.deadline, deadline)
        self.assertEqual(order.priority, WorkOrder.Priority.HIGH)
        self.assertIsNone(order.unit)
        self.assertTrue(Notification.objects.filter(user=self.tech, key=f"wo-mass-{order.pk}").exists())


class BuildingDeletePermissionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.tech = User.objects.create_user("bd-tech", "bd-tech@example.com", "pass1234")
        self.backoffice = User.objects.create_user("bd-back", "bd-back@example.com", "pass1234")
        self.building = Building.objects.create(
            owner=self.tech,
            name="DeleteMe",
            address="123 Delete",
        )
        BuildingMembership.objects.create(user=self.tech, building=self.building, role=MembershipRole.TECHNICIAN)
        BuildingMembership.objects.create(user=self.backoffice, building=self.building, role=MembershipRole.BACKOFFICE)

    def test_technician_cannot_delete_building(self):
        self.client.login(username="bd-tech", password="pass1234")
        response = self.client.post(reverse("core:building_delete", args=[self.building.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Building.objects.filter(pk=self.building.pk).exists())

    def test_backoffice_can_delete_building(self):
        self.client.login(username="bd-back", password="pass1234")
        response = self.client.post(reverse("core:building_delete", args=[self.building.pk]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Building.objects.filter(pk=self.building.pk).exists())


class BuildingMembershipManageViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("bm-admin", "bm-admin@example.com", "pass1234")
        self.target = User.objects.create_user("bm-target", "bm-target@example.com", "pass1234")
        self.building = Building.objects.create(
            owner=self.admin,
            name="BM Complex",
            address="BM St",
        )
        BuildingMembership.objects.create(user=self.admin, building=None, role=MembershipRole.ADMINISTRATOR)

    def test_add_and_remove_membership(self):
        self.client.login(username="bm-admin", password="pass1234")
        url = reverse("core:building_memberships", args=[self.building.pk])
        response = self.client.post(
            url,
            data={"action": "add", "user": self.target.pk, "role": MembershipRole.TECHNICIAN},
        )
        self.assertEqual(response.status_code, 302)
        membership = BuildingMembership.objects.get(user=self.target, building=self.building)

        response = self.client.post(url, data={"action": "delete", "membership_id": membership.pk})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(BuildingMembership.objects.filter(user=self.target, building=self.building).exists())

    def test_forbidden_without_capability(self):
        outsider = get_user_model().objects.create_user("bm-out", "bm-out@example.com", "pass1234")
        self.client.login(username="bm-out", password="pass1234")
        response = self.client.get(reverse("core:building_memberships", args=[self.building.pk]))
        self.assertEqual(response.status_code, 403)

    def test_technician_updates_subrole(self):
        tech = get_user_model().objects.create_user("bm-tech", "bm-tech@example.com", "pass1234")
        membership = BuildingMembership.objects.create(
            user=tech,
            building=self.building,
            role=MembershipRole.TECHNICIAN,
            technician_subrole=Building.Role.TECH_SUPPORT,
        )
        self.client.login(username="bm-tech", password="pass1234")
        url = reverse("core:technician_subrole", args=[self.building.pk])
        response = self.client.post(
            url,
            data={"technician_subrole": Building.Role.PROPERTY_MANAGER},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        membership.refresh_from_db()
        self.assertEqual(membership.technician_subrole, Building.Role.PROPERTY_MANAGER)


class WorkOrderAuditLogTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.backoffice = User.objects.create_user("log-back", "log-back@example.com", "pass1234")
        self.building = Building.objects.create(
            owner=self.backoffice,
            name="Log Tower",
            address="Log St",
        )
        BuildingMembership.objects.create(user=self.backoffice, building=self.building, role=MembershipRole.BACKOFFICE)
        self.order = WorkOrder.objects.create(
            building=self.building,
            title="Needs approval",
            status=WorkOrder.Status.IN_PROGRESS,
            priority=WorkOrder.Priority.MEDIUM,
            deadline=timezone.localdate() + timedelta(days=5),
        )

    def test_status_change_creates_log(self):
        self.client.login(username="log-back", password="pass1234")
        url = reverse("core:work_order_update", args=[self.order.pk])
        payload = {
            "title": self.order.title,
            "building": self.building.pk,
            "unit": "",
            "priority": self.order.priority,
            "status": WorkOrder.Status.AWAITING_APPROVAL,
            "deadline": self.order.deadline,
            "description": self.order.description,
            "replacement_request_note": "Need pump",
        }
        response = self.client.post(url, data=payload, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            WorkOrderAuditLog.objects.filter(work_order=self.order, action=WorkOrderAuditLog.Action.STATUS_CHANGED).exists()
        )


class AuditTrailViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user("audit-admin", "audit-admin@example.com", "pass1234")
        BuildingMembership.objects.create(user=self.admin, building=None, role=MembershipRole.ADMINISTRATOR)
        self.building = Building.objects.create(owner=self.admin, name="Audit HQ", address="Audit")
        self.order = WorkOrder.objects.create(
            building=self.building,
            title="Audit job",
            status=WorkOrder.Status.OPEN,
            priority=WorkOrder.Priority.LOW,
            deadline=timezone.localdate() + timedelta(days=2),
        )
        WorkOrderAuditLog.objects.create(
            actor=self.admin,
            work_order=self.order,
            building=self.building,
            action=WorkOrderAuditLog.Action.STATUS_CHANGED,
            payload={"from": "OPEN", "to": "IN_PROGRESS"},
        )

    def test_audit_trail_access(self):
        self.client.login(username="audit-admin", password="pass1234")
        response = self.client.get(reverse("core:audit_trail"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["workorder_entries"])

    def test_export_workorders(self):
        self.client.login(username="audit-admin", password="pass1234")
        response = self.client.get(reverse("core:audit_trail") + "?export=workorder")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

    def test_forbidden_without_capability(self):
        outsider = get_user_model().objects.create_user("audit-out", "audit-out@example.com", "pass1234")
        self.client.login(username="audit-out", password="pass1234")
        response = self.client.get(reverse("core:audit_trail"))
        self.assertEqual(response.status_code, 403)
