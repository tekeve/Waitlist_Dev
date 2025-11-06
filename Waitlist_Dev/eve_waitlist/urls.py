"""
URL configuration for eve_waitlist project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.views.generic import TemplateView # Make sure this is imported

urlpatterns = [
    # Point the homepage to our new, unique template name
    path('', TemplateView.as_view(template_name="homepage.html"), name='home'),
    
    path('admin/', admin.site.urls),
    
    # ESI SSO URLs (login, callback)
    # We are explicitly defining the namespace 'esi' here.
    #
    # --- THE FIX ---
    # We are REMOVING the 'sso/' path. It's not resolving correctly.
    # We will move the callback into the 'esi_auth' app.
    # path('sso/', include('esi.urls', namespace='esi')),
    
    # Our custom auth views (login, logout, and now callback)
    #
    # THE FIX IS HERE:
    # We are also explicitly defining the namespace 'esi_auth' here...
    path('auth/', include('esi_auth.urls', namespace='esi_auth')),
]