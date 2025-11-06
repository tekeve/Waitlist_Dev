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
]