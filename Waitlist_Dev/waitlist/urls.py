from django.urls import path
from . import views

app_name = 'waitlist'

urlpatterns = [
    # Point the root URL to our new dynamic view
    path('', views.home, name='home'),
    
    # --- NEW FC ADMIN URLS ---
    path('fc_admin/', views.fc_admin_view, name='fc_admin'),
    path('api/fc_manage_waitlist/', views.api_fc_manage_waitlist, name='api_fc_manage_waitlist'),
    
    # --- EXISTING API URLs ---
    path('api/submit_fit/', views.api_submit_fit, name='api_submit_fit'),
    path('api/update_fit_status/', views.api_update_fit_status, name='api_update_fit_status'),
    path('api/get_waitlist_html/', views.api_get_waitlist_html, name='api_get_waitlist_html'),
    
    # --- API for FC Fit Modal ---
    path('api/get_fit_details/', views.api_get_fit_details, name='api_get_fit_details'),
    
    # --- NEW API FOR ADDING SUBSTITUTIONS ---
    path('api/add_substitution/', views.api_add_substitution, name='api_add_substitution'),
    
    # ---
    # --- NEW API URLS FOR FLEET MANAGEMENT ---
    # ---
    path('api/get_fleet_structure/', views.api_get_fleet_structure, name='api_get_fleet_structure'),
    path('api/save_squad_mappings/', views.api_save_squad_mappings, name='api_save_squad_mappings'),
    path('api/fc_invite_pilot/', views.api_fc_invite_pilot, name='api_fc_invite_pilot'),
    # --- NEW: API for creating the layout ---
    path('api/fc_create_default_layout/', views.api_fc_create_default_layout, name='api_fc_create_default_layout'),
    # ---
    # --- END NEW API URLS ---
    # ---
]