from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from core.models import Building


class OfficeSingletonCacheTests(TestCase):
    def setUp(self):
        Building.clear_system_default_cache()
        self.owner = get_user_model().objects.create_user(username="owner", password="pass")

    def tearDown(self):
        Building.clear_system_default_cache()

    def test_system_default_id_is_memoized(self):
        Building.objects.create(owner=self.owner, name="Office", is_system_default=True)
        with self.assertNumQueries(1):
            first = Building.system_default_id()
            second = Building.system_default_id()
        self.assertEqual(first, second)

    def test_returns_none_when_missing(self):
        with self.assertNumQueries(1):
            first = Building.system_default_id()
            second = Building.system_default_id()
        self.assertIsNone(first)
        self.assertIsNone(second)

    def test_force_refresh_busts_cache(self):
        building = Building.objects.create(owner=self.owner, name="Office", is_system_default=True)
        cached = Building.system_default_id()
        self.assertEqual(cached, building.pk)
        with self.assertNumQueries(1):
            refreshed = Building.system_default_id(force_refresh=True)
        self.assertEqual(refreshed, building.pk)

    @override_settings(SYSTEM_DEFAULT_BUILDING_CACHE_TIMEOUT=5)
    def test_multi_worker_stale_cache_refreshes_after_ttl(self):
        primary = Building.objects.create(owner=self.owner, name="Office", is_system_default=True)
        backup = Building.objects.create(owner=self.owner, name="Backup")
        cached = Building.system_default_id()
        self.assertEqual(cached, primary.pk)
        Building.objects.filter(pk=primary.pk).update(is_system_default=False)
        Building.objects.filter(pk=backup.pk).update(is_system_default=True)
        cache.delete(Building._system_default_cache_key)
        Building._system_default_cache_timestamp -= 10
        with self.assertNumQueries(1):
            refreshed = Building.system_default_id()
        self.assertEqual(refreshed, backup.pk)

    @override_settings(SYSTEM_DEFAULT_BUILDING_CACHE_TIMEOUT=5)
    def test_manual_update_without_signals_still_recovers(self):
        office = Building.objects.create(owner=self.owner, name="Office", is_system_default=True)
        emergency = Building.objects.create(owner=self.owner, name="Emergency")
        cached = Building.system_default_id()
        self.assertEqual(cached, office.pk)
        Building.objects.filter(pk=office.pk).update(is_system_default=False)
        Building.objects.filter(pk=emergency.pk).update(is_system_default=True)
        self.assertEqual(
            cache.get(Building._system_default_cache_key),
            office.pk,
        )  # no signals ran, value is stale until TTL expiry
        Building._system_default_cache_timestamp -= 10
        with self.assertNumQueries(1):
            refreshed = Building.system_default_id()
        self.assertEqual(refreshed, emergency.pk)
