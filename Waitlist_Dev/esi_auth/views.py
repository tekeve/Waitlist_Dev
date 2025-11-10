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
from datetime import timezone, timedelta # Import for time calculations

# Import the CallbackRedirect model from the esi library
from esi.models import CallbackRedirect, Token

# --- NEW: Import ESI client ---
from esi.clients import EsiClientProvider
from bravado.exception import HTTPNotFound
# --- END NEW ---


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
        # --- MODIFIED ---
        return redirect('waitlist:home')

    # 2. Get the ESI token from the object.
    esi_token = callback_redirect.token
    if not esi_token:
        # Callback happened but didn't result in a token.
        callback_redirect.delete() # Clean up the failed redirect
        # --- MODIFIED ---
        return redirect('waitlist:home')
        
    # --- THIS IS THE FIX (Part 2) ---
    # The 'esi_token' is the NEW token. We must delete all
    # old tokens for this character to prevent duplicates.
    Token.objects.filter(
        character_id=esi_token.character_id
    ).exclude(pk=esi_token.pk).delete()
    # --- END FIX ---
        
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
        # --- MODIFIED ---
        return redirect('waitlist:home')
        
    if not char_id or not char_name:
        # Token is missing key info
        callback_redirect.delete()
        # --- MODIFIED ---
        return redirect('waitlist:home')


    # ---
    # --- THIS IS THE FIX: Handle 'Add Alt' vs 'First Login'
    # ---
    
    user_account = None 
    user_was_authenticated = request.user.is_authenticated
    
    # --- NEW: Get ESI client ---
    esi = EsiClientProvider()
    
    # --- NEW: Helper function to get public corp/alliance data ---
    def get_public_character_data(character_id):
        try:
            public_data = esi.client.Character.get_characters_character_id(
                character_id=character_id
            ).results()
            
            corp_id = public_data.get('corporation_id')
            alliance_id = public_data.get('alliance_id')
            
            corp_name = None
            if corp_id:
                corp_data = esi.client.Corporation.get_corporations_corporation_id(
                    corporation_id=corp_id
                ).results()
                corp_name = corp_data.get('name')
                
            alliance_name = None
            if alliance_id:
                try:
                    alliance_data = esi.client.Alliance.get_alliances_alliance_id(
                        alliance_id=alliance_id
                    ).results()
                    alliance_name = alliance_data.get('name')
                except HTTPNotFound:
                    alliance_name = "N/A" # Handle dead alliances

            return {
                "corporation_id": corp_id,
                "corporation_name": corp_name,
                "alliance_id": alliance_id,
                "alliance_name": alliance_name,
            }
        except Exception as e:
            print(f"Error fetching public data for {character_id}: {e}")
            return {} # Return empty dict on failure
    # --- END NEW HELPER ---
    
    
    # --- Get public data ---
    public_data = get_public_character_data(char_id)
    # ---


    if user_was_authenticated:
        # CASE 1: USER IS ALREADY LOGGED IN (Adding an Alt)
        user_account = request.user
    else:
        # CASE 2: USER IS NOT LOGGED IN
        
        # --- THIS IS THE FIX: Check if character exists first ---
        try:
            existing_char = EveCharacter.objects.get(character_id=char_id)
            # If found, log in as that character's user
            user_account = existing_char.user
        except EveCharacter.DoesNotExist:
            # Not found, so this is a NEW user
            user_account, created = User.objects.get_or_create(
                username=str(char_id), # Use character ID as username
                defaults={'first_name': char_name} # Set name only on creation
            )

            # If user already existed, check if their name changed
            if not created and user_account.first_name != char_name:
                user_account.first_name = char_name
                user_account.save() # Save the name change

            if created:
                user_account.is_active = True
                # If this is the very first user, make them an admin
                if User.objects.count() == 1:
                    user_account.is_staff = True
                    user_account.is_superuser = True
                user_account.save() # Save the new user (with flags)
        # --- END FIX ---


    # 5. Link the token to this user account.
    if esi_token.user is None:
        esi_token.user = user_account
        esi_token.save()
        
    # --- FINAL FIX: Create the EveCharacter object ---
    # 6. Find or create the EveCharacter link.
    
    expiry_time = esi_token.created + timedelta(seconds=1200)
    
    # --- NEW: Check if this is the first char for this user ---
    # We do this *before* creating the new one
    existing_char_count = EveCharacter.objects.filter(user=user_account).count()
    is_first_char = (existing_char_count == 0)
    # --- END NEW ---
    
    eve_char, char_created = EveCharacter.objects.get_or_create(
        character_id=char_id,
        defaults={
            'user': user_account, # Link to the correct account
            'character_name': char_name,
            'access_token': esi_token.access_token,
            'refresh_token': esi_token.refresh_token,
            'token_expiry': expiry_time, # Use our calculated time
            'is_main': is_first_char, # <-- NEW: Set is_main if first char
            **public_data # <-- NEW: Add corp/alliance data
        }
    )
    
    if not char_created:
        # Character record already existed
        
        # --- NEW: Update token and public data ---
        eve_char.access_token = esi_token.access_token
        eve_char.refresh_token = esi_token.refresh_token
        eve_char.token_expiry = expiry_time
        
        eve_char.corporation_id = public_data.get('corporation_id')
        eve_char.corporation_name = public_data.get('corporation_name')
        eve_char.alliance_id = public_data.get('alliance_id')
        eve_char.alliance_name = public_data.get('alliance_name')
        
        # This handles re-linking a character to a different account if needed
        if eve_char.user != user_account:
            eve_char.user = user_account
        
        eve_char.save()
        # --- END NEW ---
        
    elif char_created and not is_first_char:
        # This was a new character, but if the token has expired
        # we need to update the expiry time.
        # This handles the case where the EveCharacter was created
        # but the token fields were not updated.
        # A bit redundant with the defaults, but ensures freshness.
        
        # --- NEW: Make sure only one main ---
        # If we just created a new alt, ensure it's not set as main
        if eve_char.is_main:
            eve_char.is_main = False
        # --- END NEW ---

        eve_char.access_token = esi_token.access_token
        eve_char.refresh_token = esi_token.refresh_token
        eve_char.token_expiry = expiry_time # Use our calculated time
        eve_char.save()

    # --- END FINAL FIX ---

    # 7. We have the user object in memory, so we can
    #    now safely delete the redirect object.
    callback_redirect.delete()

    # 8. Now, we perform the login logic.
    if user_account: # This user object is now guaranteed to exist
        
        # --- THIS IS THE FIX ---
        # Only log the user in if they weren't *already* logged in
        # at the start of this request.
        if not user_was_authenticated:
        # --- END FIX ---
            if not user_account.is_active: # This is a good safety check
                user_account.is_active = True
                user_account.save()
                
            # 9. If a user is associated, log them in!
            login(request, user_account)
            
            # 10. Force Django to save the session *after* logging in
            #    but *before* redirecting.
            request.session.save()
    
    # 11. Send the now-logged-in user to the homepage.
    # --- MODIFIED ---
    return redirect('waitlist:home')
# --- END FIX ---


def esi_logout(request):
    """
    Logs the user out of the Django application.
    """
    logout(request)
    # --- MODIFIED ---
    return redirect('waitlist:home')