from django.urls import path
# Import all three view modules
from . import views, fc_views, api_views

app_name = 'waitlist'

urlpatterns = [
    # --- Main views (from views.py) ---
    path('', views.home, name='home'),
    path('fittings/', views.fittings_view, name='fittings_view'),
    path('api/submit_fit/', views.api_submit_fit, name='api_submit_fit'),
    path('api/update_fit_status/', views.api_update_fit_status, name='api_update_fit_status'),
    path('api/get_waitlist_html/', views.api_get_waitlist_html, name='api_get_waitlist_html'),

    # --- FC Admin views (from fc_views.py) ---
    path('fc_admin/', fc_views.fc_admin_view, name='fc_admin'),
    path('api/fc_manage_waitlist/', fc_views.api_fc_manage_waitlist, name='api_fc_manage_waitlist'),
    path('api/get_fleet_structure/', fc_views.api_get_fleet_structure, name='api_get_fleet_structure'),
    # --- NEW: API for fleet overview ---
    path('api/get_fleet_members/', fc_views.api_get_fleet_members, name='api_get_fleet_members'),
    # --- END NEW ---
    path('api/save_squad_mappings/', fc_views.api_save_squad_mappings, name='api_save_squad_mappings'),
    path('api/fc_invite_pilot/', fc_views.api_fc_invite_pilot, name='api_fc_invite_pilot'),
    path('api/fc_create_default_layout/', fc_views.api_fc_create_default_layout, name='api_fc_create_default_layout'),
    path('api/fc_add_squad/', fc_views.api_fc_add_squad, name='api_fc_add_squad'),
    path('api/fc_delete_squad/', fc_views.api_fc_delete_squad, name='api_fc_delete_squad'),
    path('api/fc_add_wing/', fc_views.api_fc_add_wing, name='api_fc_add_wing'),
    path('api/fc_delete_wing/', fc_views.api_fc_delete_wing, name='api_fc_delete_wing'),
    path('api/fc_refresh_structure/', fc_views.api_fc_refresh_structure, name='api_fc_refresh_structure'),

    # --- NEW: Rule Helper Page ---
    path('fc_admin/rule_helper/', fc_views.fc_rule_helper_view, name='fc_rule_helper'),
    path('api/fc_save_comparison_rules/', fc_views.api_fc_save_comparison_rules, name='api_fc_save_comparison_rules'),
    # --- NEW: Ignore Group API ---
    path('api/fc_ignore_rule_group/', fc_views.api_fc_ignore_rule_group, name='api_fc_ignore_rule_group'),
    # --- END NEW ---

    # --- API / Fit views (from api_views.py) ---
    path('api/get_fit_details/', api_views.api_get_fit_details, name='api_get_fit_details'),
    path('api/get_doctrine_fit_details/', api_views.api_get_doctrine_fit_details, name='api_get_doctrine_fit_details'),
]