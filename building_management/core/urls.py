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

    path("toggle-theme/", views.toggle_theme, name="toggle_theme"),

    # Units
    path("buildings/<int:pk>/units/new/", views.UnitCreateView.as_view(), name="unit_create"),
    path("units/<int:pk>/", views.UnitDetailView.as_view(), name="unit_detail"),
    path("units/<int:pk>/edit/", views.UnitUpdateView.as_view(), name="unit_update"),
    path("units/<int:pk>/delete/", views.UnitDeleteView.as_view(), name="unit_delete"),
    
    
    # Work Orders    
    path("work-orders/", views.WorkOrderListView.as_view(), name="work_orders_list"),
    path("work-orders/new/", views.WorkOrderCreateView.as_view(), name="work_order_create"),
    path("work-orders/<int:pk>/", views.WorkOrderDetailView.as_view(), name="work_order_detail"),
    path("work-orders/<int:pk>/edit/", views.WorkOrderUpdateView.as_view(), name="work_order_update"),
    path("work-orders/<int:pk>/delete/", views.WorkOrderDeleteView.as_view(), name="work_order_delete"),
    path("work-orders/<int:pk>/archive/", views.WorkOrderArchiveView.as_view(), name="work_order_archive"),
    path("work-orders/mass-assign/", views.MassAssignWorkOrdersView.as_view(), name="work_orders_mass_assign"),
    path("work-orders/archive/", views.ArchivedWorkOrderListView.as_view(), name="work_orders_archive"),

    # Admin user management (superuser-only dashboard)
    path("manage/users/", views.AdminUserListView.as_view(), name="users_list"),
    path("manage/users/new/", views.AdminUserCreateView.as_view(), name="user_create"),
    path("manage/users/<int:pk>/edit/", views.AdminUserUpdateView.as_view(), name="user_update"),
    path("manage/users/<int:pk>/password/", views.AdminUserPasswordView.as_view(), name="user_password"),
    path("manage/users/<int:pk>/delete/", views.AdminUserDeleteView.as_view(), name="user_delete"),

    # APIs (optional)
    path("api/buildings/", views.api_buildings, name="api_buildings"),
    path("api/buildings/<int:building_id>/units/", views.api_units, name="api_units"),
]
