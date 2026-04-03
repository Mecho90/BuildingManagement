from django.test import RequestFactory, SimpleTestCase

from core.templatetags.nav import active_nav, active_nav_next_prefix


class ActiveNavTagTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _active_css(self, path: str, *patterns: str) -> str:
        request = self.factory.get(path)
        return active_nav(
            {"request": request},
            *patterns,
            css_class="nav-link--active",
        )

    def test_dashboard_button_uses_exact_root_match(self):
        self.assertEqual(self._active_css("/", "/"), "nav-link--active")
        self.assertEqual(self._active_css("/buildings/", "/"), "")

    def test_todos_button_covers_subpages(self):
        self.assertEqual(self._active_css("/todos/new/", "/todos/"), "nav-link--active")
        self.assertEqual(self._active_css("/todos/42/edit/", "/todos/"), "nav-link--active")

    def test_buildings_button_covers_subpages_and_mass_delete(self):
        self.assertEqual(self._active_css("/buildings/new/", "/buildings/"), "nav-link--active")
        self.assertEqual(
            self._active_css("/manage/mass-delete/buildings/", "/buildings/", "/manage/mass-delete/buildings/$"),
            "nav-link--active",
        )

    def test_work_orders_button_covers_subpages_and_exclusions(self):
        patterns = (
            "/work-orders/",
            "!/work-orders/archive/",
            "!/work-orders/lawyer/",
            "/manage/mass-delete/work-orders/$",
            "/manage/mass-archive/work-orders/$",
        )
        self.assertEqual(self._active_css("/work-orders/18/edit/", *patterns), "nav-link--active")
        self.assertEqual(self._active_css("/work-orders/new/", *patterns), "nav-link--active")
        self.assertEqual(self._active_css("/manage/mass-delete/work-orders/", *patterns), "nav-link--active")
        self.assertEqual(self._active_css("/manage/mass-archive/work-orders/", *patterns), "nav-link--active")
        self.assertEqual(self._active_css("/work-orders/archive/", *patterns), "")
        self.assertEqual(self._active_css("/work-orders/archive/purge/", *patterns), "")
        self.assertEqual(self._active_css("/work-orders/lawyer/", *patterns), "")

    def test_work_orders_button_respects_next_prefix(self):
        request = self.factory.get("/work-orders/18/edit/?next=%2Fwork-orders%2F")
        css = active_nav_next_prefix({"request": request}, "/work-orders/", css_class="nav-link--active")
        self.assertEqual(css, "nav-link--active")

    def test_lawyer_orders_button_covers_subpages_and_mass_delete(self):
        self.assertEqual(self._active_css("/work-orders/lawyer/", "/work-orders/lawyer/"), "nav-link--active")
        self.assertEqual(
            self._active_css(
                "/manage/mass-delete/lawyer-work-orders/",
                "/work-orders/lawyer/",
                "/manage/mass-delete/lawyer-work-orders/$",
            ),
            "nav-link--active",
        )

    def test_budgets_button_covers_subpages(self):
        self.assertEqual(self._active_css("/budgets/new/", "/budgets/"), "nav-link--active")
        self.assertEqual(self._active_css("/budgets/18/review/", "/budgets/"), "nav-link--active")
        self.assertEqual(self._active_css("/budgets/archived/", "/budgets/"), "nav-link--active")

    def test_archive_button_covers_subpages(self):
        self.assertEqual(self._active_css("/work-orders/archive/", "/work-orders/archive/"), "nav-link--active")
        self.assertEqual(
            self._active_css("/work-orders/archive/buildings/delete/", "/work-orders/archive/"),
            "nav-link--active",
        )

    def test_user_management_button_covers_subpages_and_mass_delete(self):
        self.assertEqual(self._active_css("/manage/users/7/edit/", "/manage/users/"), "nav-link--active")
        self.assertEqual(
            self._active_css("/manage/mass-delete/users/", "/manage/users/", "/manage/mass-delete/users/$"),
            "nav-link--active",
        )
