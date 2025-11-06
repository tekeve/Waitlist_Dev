from django.shortcuts import redirect, resolve_url
# --- THE FIX ---
# Import the tools we need
from django.contrib.auth import logout, login
# Import the built-in User model
from django.contrib.auth.models import User
# Import your EveCharacter model
from waitlist.models import EveCharacter
# --- END FIX ---
from django.conf import settings
from urllib.parse import urlencode
import secrets 

# Import the CallbackRedirect model from the esi library
from esi.models import CallbackRedirect, Token


try:
    # Import the real callback view from the esi library
    from esi.views import receive_callback as esi_callback
except ImportError:
    # Fallback for different library versions
    from esi.views import receive_callback as esi_callback

def esi_login(request):
    """
    Redirects the user to the EVE SSO login page.
    This is Step 1 of the OAuth flow.
    """
    
    # 1. Force Django to save the session. This guarantees
    #    request.session.session_key is not None.
    if not request.session.session_key:
        request.session.save()

    # 2. Generate a unique state
    state = secrets.token_urlsafe(16)
    
    # 3. Define where the user should land *after* the ESI callback.
    #    This now points to our new "Step 3" view.
    redirect_url = resolve_url('esi_auth:sso_complete') 

    # 4. Create or Update the CallbackRedirect object
    #    This prevents a crash if a user clicks "login" twice.
    callback, created = CallbackRedirect.objects.update_or_create(
        session_key=request.session.session_key,  # This is the key to look up
        defaults={                                # These are the values to set/update
            'url': redirect_url,
            'state': state
        }
    )

    # 5. Build the EVE SSO redirect URL
    authorize_url = "https://login.eveonline.com/v2/oauth/authorize/"
    
    # --- THE FIX: Dynamically choose scope list ---
    scope_type = request.GET.get('scopes', 'regular')

    scopes_to_request = []
    if scope_type == 'fc':
        # User is requesting FC scopes
        scopes_to_request = settings.ESI_SSO_SCOPES_FC
    else:
        # Default to regular scopes
        scopes_to_request = settings.ESI_SSO_SCOPES_REGULAR
    # --- END FIX ---

    params = {
        'response_type': 'code',
        'redirect_uri': settings.ESI_SSO_CALLBACK_URL,
        'client_id': settings.ESI_SSO_CLIENT_ID,
        'scope': ' '.join(scopes_to_request), # Use the dynamic list
        'state': state, # Use the state from the database object
    }
    
    # 6. Redirect the user to EVE's login page
    return redirect(f"{authorize_url}?{urlencode(params)}")


# --- THE FIX ---
# This is our new "Step 3" view
def sso_complete_login(request):
    """
    Handles the final step of logging the user into Django.
    The 'esi_callback' (Step 2) view redirects here.
    """
    try:
        # 1. Find the CallbackRedirect object for this session.
        #    The 'esi_callback' view has already used it and
        #    populated its 'token' field.
        callback_redirect = CallbackRedirect.objects.get(
            session_key=request.session.session_key
        )
    except CallbackRedirect.DoesNotExist:
        # This shouldn't happen, but if it does, send to homepage.
        return redirect('home')

    # 2. Get the ESI token from the object.
    esi_token = callback_redirect.token
    if not esi_token:
        # Callback happened but didn't result in a token.
        return redirect('home')
        
    # 3. --- THIS IS THE NEW LOGIC ---
    #    The 'esi_callback' does NOT create a user, so we do it.
    
    # We assume the token object has these fields.
    try:
        char_id = esi_token.character_id
        char_name = esi_token.character_name
    except AttributeError:
        # This will fail if the token model fields are named differently.
        # If so, we can't log in, so just clean up and go home.
        callback_redirect.delete()
        return redirect('home')
        
    if not char_id or not char_name:
        # Token is missing key info
        callback_redirect.delete()
        return redirect('home')


    # --- THE FIX: Handle 'Add Alt' vs 'First Login' ---
    
    # This variable will hold the Django account
    user_account = None 
    is_new_user = False

    if request.user.is_authenticated:
        # CASE 1: USER IS ALREADY LOGGED IN (Adding an Alt)
        # Use the currently logged-in user as the account.
        user_account = request.user
    else:
        # CASE 2: USER IS NOT LOGGED IN (First-time login)
        # We need to find or create a new user account.
        # We'll use the character name as the account username.
        user_account, created = User.objects.get_or_create(
            username=char_name
        )
        is_new_user = created # Flag this as a new user

        if created:
            user_account.is_active = True
            # If this is the very first user, make them an admin
            if User.objects.count() == 1:
                user_account.is_staff = True
                user_account.is_superuser = True
            user_account.save()
            
    # --- END FIX ---


    # 5. Link the token to this user account.
    if esi_token.user is None:
        esi_token.user = user_account
        esi_token.save()
        
    # --- FINAL FIX: Create the EveCharacter object ---
    # 6. Find or create the EveCharacter link.
    #    This now correctly uses 'user_account'
    eve_char, char_created = EveCharacter.objects.get_or_create(
        character_id=char_id,
        defaults={
            'user': user_account, # Link to the correct account
            'character_name': char_name,
            # We're just saving the basics, we'll update
            # tokens later if we need to.
            'access_token': esi_token.access_token,
            'refresh_token': esi_token.refresh_token,
            'token_expiry': esi_token.expires
        }
    )
    
    # If the character record already existed, update its user link
    # This handles "re-linking" a character to a different account if needed
    if not char_created and eve_char.user != user_account:
        eve_char.user = user_account
        eve_char.save()
    # --- END FINAL FIX ---

    # 7. We have the user object in memory, so we can
    #    now safely delete the redirect object.
    callback_redirect.delete()

    # 8. Now, we perform the login logic.
    if user_account: # This user object is now guaranteed to exist
        
        # Only log in if it was a new user.
        # If they were just adding an alt, they are already logged in.
        if is_new_user:
            if not user_account.is_active: # This is a good safety check
                user_account.is_active = True
                user_account.save()
                
            # 9. If a user is associated, log them in!
            login(request, user_account)
            
            # 10. Force Django to save the session *after* logging in
            #    but *before* redirecting.
            request.session.save()
    
    # 11. Send the now-logged-in user to the homepage.
    return redirect('home')
# --- END FIX ---


def esi_logout(request):
    """
    Logs the user out of the Django application.
    """
    logout(request)
    return redirect('home')