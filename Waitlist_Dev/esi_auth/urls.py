from django.urls import path
from . import views

# THE FIX IS HERE:
# We're adding this line to tell Django what the app_name is.
# This is required when using a namespace in the main urls.py
app_name = 'esi_auth'

# These URL patterns are prefixed with '/auth/' (from the main urls.py)

urlpatterns = [
    # Full path will be /auth/login/
    path('login/', views.esi_login, name='login'),
    
    # Full path will be /auth/logout/
    path('logout/', views.esi_logout, name='logout'),
    
    # --- THE FIX ---
    # Full path will be /auth/callback/
    # We are explicitly routing the callback to the real view
    # from the 'esi' library.
    path('callback/', views.esi_callback, name='callback'),
    
    # --- THE FIX ---
    # Full path will be /auth/sso_complete/
    # This is our new "Step 3" view that performs the actual
    # Django login after the callback is done.
    path('sso_complete/', views.sso_complete_login, name='sso_complete'),
]