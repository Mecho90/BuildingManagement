# path: core/urls.py
from django.urls import path
from . import views

urlpatterns = [
    # Buildings
    path("buildings/", views.BuildingListView.as_view(), name="buildings_list"),
    path("buildings/new/", views.BuildingCreateView.as_view(), name="building_create"),
    path("buildings/<int:pk>/", views.BuildingDetailView.as_view(), name="building_detail"),
    path("buildings/<int:pk>/edit/", views.BuildingUpdateView.as_view(), name="building_update"),
    path("buildings/<int:pk>/delete/", views.BuildingDeleteView.as_view(), name="building_delete"),

    # Units
    path("buildings/<int:building_id>/units/new/", views.UnitCreateView.as_view(), name="unit_create"),
    path("units/<int:pk>/", views.DetailView.as_view(), name="unit_detail"),
    path("units/<int:pk>/edit/", views.UnitUpdateView.as_view(), name="unit_update"),
    path("units/<int:pk>/delete/", views.UnitDeleteView.as_view(), name="unit_delete"),

    # APIs (optional)
    path("api/buildings/", views.api_buildings, name="api_buildings"),
    path("api/units/", views.api_units, name="api_units"),
]
