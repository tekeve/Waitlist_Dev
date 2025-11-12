"""
URL configuration for eve_waitlist project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
"""
from django.contrib import admin
from django.urls import path, include
# --- ADD THESE IMPORTS ---
from django.conf import settings
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
# --- END ADDED IMPORTS ---

urlpatterns = [
    # Point the root URL to our waitlist app
    path('', include('waitlist.urls')),
    
    path('admin/', admin.site.urls),
    
    # Our custom auth views (login, logout, and callback)
    path('auth/', include('esi_auth.urls', namespace='esi_auth')),
    
    # URLs for the pilot app
    path('pilot/', include('pilot.urls', namespace='pilot')),

    # Add the django_eventstream URLs
    # This is what our server-sent events (SSE) connect to
    path('events/', include('django_eventstream.urls')),
]

# --- ADD THIS BLOCK AT THE END ---
# This will automatically serve static files (like the admin's CSS)
# when DEBUG=True and we are using an ASGI server like daphne.
if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()
# --- END ADDED BLOCK ---