from django.shortcuts import render, redirect, get_object_or_404, resolve_url
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse
from django.contrib import messages
from .models import EveCharacter, ShipFit, Fleet, FleetWaitlist
# --- MODIFIED: Import EveGroup as well ---
from pilot.models import EveType, EveGroup
from django.utils import timezone # Import timezone
import random
import re # --- NEW: Import regex for header parsing ---

# --- NEW IMPORTS ---
import requests
from esi.clients import EsiClientProvider
from esi.models import Token
from django.contrib.auth import logout
# --- END NEW IMPORTS ---


# --- NEW: Helper function to check for FC status ---
def is_fleet_commander(user):
    """
    Checks if a user is in the 'Fleet Commander' group.
    """
    return user.groups.filter(name='Fleet Commander').exists()


# --- NEW: Local Token Helper ---
# This is copied from pilot/views.py and modified to NOT logout/delete,
# but instead raise exceptions that our API view can catch.
def get_refreshed_token_for_character(user, character):
    """
    Fetches and, if necessary, refreshes the ESI token for a character.
    Raises an exception on auth failure.
    """
    try:
        token = Token.objects.filter(
            user=user, 
            character_id=character.character_id
        ).order_by('-created').first()
        
        if not token:
            raise Token.DoesNotExist

        # --- FIX: Handle token_expiry being None (e.g., on first login) ---
        if not character.token_expiry or character.token_expiry < timezone.now():
            token.refresh()
            character.access_token = token.access_token
            # .expires is an in-memory attribute added by .refresh()
            character.token_expiry = token.expires 
            character.save()
            
        return token

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 400:
            # Refresh token is invalid.
            raise Exception("Your ESI token is invalid or has been revoked. Please log out and back in.")
        else:
            raise e # Re-raise other ESI errors
    except Token.DoesNotExist:
        raise Exception("Could not find a valid ESI token for this character.")
    except Exception as e:
        # Catch other errors, like TypeError if token_expiry is None
        raise Exception(f"An unexpected token error occurred: {e}")
# --- END NEW HELPER ---


# --- NEW: SDE CACHING HELPER FUNCTIONS ---
def get_or_cache_eve_group(group_id):
    """
    Tries to get an EveGroup from the local DB.
    If not found, fetches from ESI and caches it.
    """
    try:
        # Try to get from local DB
        return EveGroup.objects.get(group_id=group_id)
    except EveGroup.DoesNotExist:
        try:
            # Not found, so fetch from ESI
            esi = EsiClientProvider()
            group_data = esi.client.Universe.get_universe_groups_group_id(
                group_id=group_id
            ).results()
            
            # Create and save the new group
            new_group = EveGroup.objects.create(
                group_id=group_id,
                name=group_data['name']
            )
            return new_group
        except Exception:
            # ESI call failed or data was bad
            return None

def get_or_cache_eve_type(ship_name):
    """
    Tries to get an EveType (ship) from the local DB by name.
    If not found, searches ESI, fetches details, and caches it.
    """
    try:
        # Try to get from local DB (case-insensitive)
        return EveType.objects.get(name__iexact=ship_name)
    except EveType.DoesNotExist:
        try:
            esi = EsiClientProvider()
            
            # 1. --- THIS IS THE FIX ---
            #    Use the /universe/ids/ endpoint to convert name to ID
            id_results = esi.client.Universe.post_universe_ids(
                names=[ship_name] # Send a list with just our ship name
            ).results()
            
            # 2. Check the results
            if 'inventory_types' not in id_results or not id_results['inventory_types']:
                # ESI couldn't find it
                return None
            
            # 3. Get the type_id
            type_id = id_results['inventory_types'][0]['id']
            # --- END FIX ---
            
            # 4. Get the type's details
            type_data = esi.client.Universe.get_universe_types_type_id(
                type_id=type_id
            ).results()
            
            # 5. Get or cache its group
            group_id = type_data['group_id']
            group = get_or_cache_eve_group(group_id)
            if not group:
                # Failed to get group info, can't save type
                return None
                
            # 6. Get slot (if any)
            slot = None
            if 'dogma_attributes' in type_data:
                for attr in type_data['dogma_attributes']:
                    if attr['attribute_id'] == 300: 
                        slot = int(attr['value'])
                        break
            
            # 7. Create and save the new type
            new_type = EveType.objects.create(
                type_id=type_id,
                name=type_data['name'],
                group=group,
                slot=slot
            )
            return new_type
            
        except Exception:
            # ESI search or type call failed
            return None
# --- END SDE CACHING HELPERS ---


# Create your views here.
@login_required
def home(request):
    """
    Handles the main homepage (/).
    - If user is authenticated, shows the waitlist_view.
    - If not, shows the simple login page (homepage.html).
    """
    
    if not request.user.is_authenticated:
        # User is not logged in, show the simple homepage
        return render(request, 'homepage.html')

    # User is logged in, show the waitlist view
    
    # 1. Find the currently open waitlist (or return None)
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    
    # 2. Get all fits for the open waitlist
    all_fits = []
    if open_waitlist:
        all_fits = ShipFit.objects.filter(
            waitlist=open_waitlist,
            status__in=['PENDING', 'APPROVED', 'IN_FLEET']
        ).select_related('character').order_by('submitted_at') # Order by time

    # --- UPDATED: Sorting now uses the new 'category' field ---
    xup_fits = all_fits.filter(status='PENDING') if open_waitlist else []
    dps_fits = all_fits.filter(status='APPROVED', category='DPS') if open_waitlist else []
    logi_fits = all_fits.filter(status='APPROVED', category='LOGI') if open_waitlist else []
    sniper_fits = all_fits.filter(status='APPROVED', category='SNIPER') if open_waitlist else []
    mar_dps_fits = all_fits.filter(status='APPROVED', category='MAR_DPS') if open_waitlist else []
    mar_sniper_fits = all_fits.filter(status='APPROVED', category='MAR_SNIPER') if open_waitlist else []
    
    is_fc = request.user.groups.filter(name='Fleet Commander').exists()
    
    context = {
        'xup_fits': xup_fits,
        'dps_fits': dps_fits,
        'logi_fits': logi_fits,
        'sniper_fits': sniper_fits,
        'mar_dps_fits': mar_dps_fits,
        'mar_sniper_fits': mar_sniper_fits,
        'is_fc': is_fc, # Pass FC status to template
        'open_waitlist': open_waitlist,
        'user_characters': EveCharacter.objects.filter(user=request.user) # For the modal
    }
    return render(request, 'waitlist_view.html', context)
    

# --- NEW API VIEW for Modal Fit Submission ---
@login_required
@require_POST
def api_submit_fit(request):
    """
    Handles the fit submission from the X-Up modal.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()

    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "The waitlist is currently closed."}, status=400)

    # Get data from the form
    character_id = request.POST.get('character_id')
    raw_fit_original = request.POST.get('raw_fit') 
    
    # Validate that the character belongs to the user
    try:
        character = EveCharacter.objects.get(
            character_id=character_id, 
            user=request.user
        )
    except EveCharacter.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Invalid character selected."}, status=403)
    
    if not raw_fit_original:
        return JsonResponse({"status": "error", "message": "Fit cannot be empty."}, status=400)
    
    # ---
    # --- MODIFIED CUSTOM PARSING LOGIC ---
    # ---
    try:
        # 1. Minimal sanitization: Replace non-breaking spaces
        raw_fit_no_nbsp = raw_fit_original.replace(u'\xa0', u' ')
        
        # 2. Find the first non-empty line
        lines = [line.strip() for line in raw_fit_no_nbsp.splitlines() if line.strip()]
        if not lines:
            return JsonResponse({"status": "error", "message": "Fit is empty or contains only whitespace."}, status=400)

        # 3. Manually parse the header (first line)
        #    Regex: finds text inside [ ] separated by a comma
        header_match = re.match(r'^\[(.*?),\s*(.*?)\]$', lines[0])
        if not header_match:
            return JsonResponse({"status": "error", "message": "Could not find valid header. Fit must start with [Ship, Fit Name]."}, status=400)
            
        ship_name = header_match.group(1).strip()
        if not ship_name:
            return JsonResponse({"status": "error", "message": "Ship name in header is empty."}, status=400)

        # 4. Get the Type ID for the ship (THIS IS THE FIX)
        #    This will try to get from DB, or fetch from ESI and cache it
        ship_type = get_or_cache_eve_type(ship_name)
        
        if not ship_type:
            # This now means the ESI search *also* failed
            return JsonResponse({"status": "error", "message": f"Ship hull '{ship_name}' could not be found in ESI. Check spelling."}, status=400)
        
        ship_type_id = ship_type.type_id
        # --- END FIX ---

        # 5. Save to database
        fit, created = ShipFit.objects.update_or_create(
            character=character,
            waitlist=open_waitlist,
            status__in=['PENDING', 'APPROVED', 'IN_FLEET'],
            defaults={
                'raw_fit': raw_fit_original,  # Save the *original* fit
                'status': 'PENDING',
                'waitlist': open_waitlist,
                'ship_name': ship_type.name,  # --- Use the canonical name from DB/ESI
                'ship_type_id': ship_type_id,
                'tank_type': 'Shield',        # <-- Placeholder
                'fit_issues': None,           # <-- Placeholder
                'category': 'NONE',
                'submitted_at': timezone.now()
            }
        )
        
        if created:
            return JsonResponse({"status": "success", "message": f"Fit for {character.character_name} submitted!"})
        else:
            return JsonResponse({"status": "success", "message": f"Fit for {character.character_name} updated."})

    except Exception as e:
        # Catch regex errors or other unexpected issues
        return JsonResponse({"status": "error", "message": f"An error occurred during parsing: {str(e)}"}, status=500)
    # --- END MODIFICATION ---


@login_required
@require_POST # Ensure this can only be POSTed to
def api_update_fit_status(request):
    """
    Handles FC actions (approve/deny) from the waitlist view.
    This is called by the JavaScript 'fetch' command.
    """
    if not request.user.groups.filter(name='Fleet Commander').exists():
        return JsonResponse({"status": "error", "message": "Not authorized"}, status=403)

    fit_id = request.POST.get('fit_id')
    action = request.POST.get('action')

    try:
        fit = ShipFit.objects.get(id=fit_id)
    except ShipFit.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)

    if action == 'approve':
        fit.status = 'APPROVED'
        
        # --- UPDATED: Randomly assign to a 'category' for sorting ---
        # We no longer touch ship_name or tank_type here
        categories = ['DPS', 'LOGI', 'SNIPER', 'MAR_DPS', 'MAR_SNIPER']
        fit.category = random.choice(categories) # <-- Placeholder sorting
        # --- END UPDATE ---
        
        fit.save()
        return JsonResponse({"status": "success", "message": "Fit approved"})
        
    elif action == 'deny':
        fit.status = 'DENIED'
        fit.denial_reason = "Denied by FC from waitlist."
        fit.save()
        return JsonResponse({"status": "success", "message": "Fit denied"})

    return JsonResponse({"status": "error", "message": "Invalid action"}, status=400)


# --- NEW API VIEW ---
@login_required
def api_get_waitlist_html(request):
    """
    Returns just the HTML for the waitlist columns.
    Used by the live polling JavaScript.
    """
    
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    
    if not open_waitlist:
        return HttpResponseBadRequest("Waitlist closed")

    all_fits = ShipFit.objects.filter(
        waitlist=open_waitlist,
        status__in=['PENDING', 'APPROVED', 'IN_FLEET']
    ).select_related('character').order_by('submitted_at') # Order by time

    # --- UPDATED: Sorting now uses the new 'category' field ---
    xup_fits = all_fits.filter(status='PENDING')
    dps_fits = all_fits.filter(status='APPROVED', category='DPS')
    logi_fits = all_fits.filter(status='APPROVED', category='LOGI')
    sniper_fits = all_fits.filter(status='APPROVED', category='SNIPER')
    mar_dps_fits = all_fits.filter(status='APPROVED', category='MAR_DPS')
    mar_sniper_fits = all_fits.filter(status='APPROVED', category='MAR_SNIPER')
    
    is_fc = request.user.groups.filter(name='Fleet Commander').exists()

    context = {
        'xup_fits': xup_fits,
        'dps_fits': dps_fits,
        'logi_fits': logi_fits,
        'sniper_fits': sniper_fits,
        'mar_dps_fits': mar_dps_fits,
        'mar_sniper_fits': mar_sniper_fits,
        'is_fc': is_fc,
    }
    
    return render(request, '_waitlist_columns.html', context)


# --- NEW FC ADMIN VIEWS ---
@login_required
@user_passes_test(is_fleet_commander)
def fc_admin_view(request):
    """
    Displays the FC admin page for opening/closing waitlists.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).select_related('fleet', 'fleet__fleet_commander').first()
    
    # Get all characters for the logged-in user to populate the dropdown
    user_fc_characters = EveCharacter.objects.filter(user=request.user)
    
    # --- NEW: Get available (closed) fleets for the open-fleet dropdown ---
    available_fleets = Fleet.objects.filter(is_active=False).order_by('description')
    # --- END NEW ---

    context = {
        'open_waitlist': open_waitlist,
        'user_fc_characters': user_fc_characters,
        'available_fleets': available_fleets, # --- ADDED ---
        'is_fc': True, # We know this is true because of the decorator
    }
    return render(request, 'fc_admin.html', context)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_manage_waitlist(request):
    """
    API endpoint for FC actions (open, close, takeover).
    """
    action = request.POST.get('action')
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()

    if action == 'close':
        if not open_waitlist:
            return JsonResponse({"status": "error", "message": "Waitlist is already closed."}, status=400)
        
        try:
            # Find the related fleet and deactivate it
            fleet = open_waitlist.fleet
            fleet.is_active = False
            # --- NEW: Clear dynamic data on close ---
            fleet.fleet_commander = None
            fleet.esi_fleet_id = None
            # --- END NEW ---
            fleet.save()
            
            # Close the waitlist
            open_waitlist.is_open = False
            open_waitlist.save()
            
            # Deny all pending fits
            pending_fits = ShipFit.objects.filter(
                waitlist=open_waitlist,
                status='PENDING'
            )
            pending_fits.update(status='DENIED', denial_reason="Waitlist closed before approval.")
            
            return JsonResponse({"status": "success", "message": "Waitlist closed. All pending fits denied."})
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)

    elif action == 'open':
        if open_waitlist:
            return JsonResponse({"status": "error", "message": "A waitlist is already open. Please close it first."}, status=400)

        # --- THIS ENTIRE BLOCK IS MODIFIED ---
        # --- We now get fleet_id, not description ---
        fleet_id = request.POST.get('fleet_id')
        fleet_commander_id = request.POST.get('fleet_commander_id')

        if not all([fleet_id, fleet_commander_id]):
            return JsonResponse({"status": "error", "message": "Fleet Type and FC Character are required."}, status=400)
            
        try:
            # 1. Validate FC character and get token
            fc_character = EveCharacter.objects.get(
                character_id=fleet_commander_id, 
                user=request.user
            )
            token = get_refreshed_token_for_character(request.user, fc_character)

            # 2. Check for required ESI scope
            required_scope = 'esi-fleets.read_fleet.v1'
            available_scopes = set(s.name for s in token.scopes.all())
            if required_scope not in available_scopes:
                login_url = resolve_url('esi_auth:login')
                return JsonResponse({
                    "status": "error", 
                    "message": f"Missing required scope: {required_scope}. Please log in again using the 'FC Scopes' option."
                }, status=403)

            # 3. Initialize ESI client
            esi = EsiClientProvider()

            # 4. Make ESI call to get fleet info
            try:
                fleet_info = esi.client.Fleets.get_characters_character_id_fleet(
                    character_id=fc_character.character_id,
                    token=token.access_token
                ).results()
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    return JsonResponse({"status": "error", "message": "You are not currently in a fleet."}, status=400)
                else:
                    raise e # Let the outer try-catch handle other ESI errors

            # 5. Check if character is the fleet boss
            if fleet_info.get('role') != 'fleet_commander':
                return JsonResponse({"status": "error", "message": "You are not the Fleet Commander (Boss) of your current fleet."}, status=403)

            # 6. Get the ESI Fleet ID
            esi_fleet_id = fleet_info.get('fleet_id')
            if not esi_fleet_id:
                return JsonResponse({"status": "error", "message": "Could not fetch Fleet ID from ESI."}, status=500)

            # --- 7. Get and Update the selected Fleet ---
            try:
                fleet_to_open = Fleet.objects.get(id=fleet_id, is_active=False)
            except Fleet.DoesNotExist:
                # --- THIS IS THE FIX ---
                return JsonResponse({"status": "error", "message": "The fleet you selected is already open or does not exist."}, status=400)
                # --- END FIX ---

            fleet_to_open.fleet_commander = fc_character
            fleet_to_open.esi_fleet_id = esi_fleet_id
            fleet_to_open.is_active = True
            fleet_to_open.save()
            
            # --- 8. Open its associated Waitlist (THE FIX) ---
            # Use get_or_create in case the migration failed to create it
            waitlist, created = FleetWaitlist.objects.get_or_create(fleet=fleet_to_open)
            waitlist.is_open = True
            waitlist.save()
            # --- END FIX ---
            
            return JsonResponse({"status": "success", "message": f"Waitlist '{fleet_to_open.description}' opened (Fleet ID: {esi_fleet_id})."})
            
        except EveCharacter.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Invalid FC character selected."}, status=403)
        except Exception as e:
            # Catch token errors, ESI errors, or duplicate Fleet ID errors
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)
        # --- END MODIFICATION ---
        
    elif action == 'takeover':
        if not open_waitlist:
            return JsonResponse({"status": "error", "message": "No waitlist is currently open to take over."}, status=400)
            
        fleet_commander_id = request.POST.get('fleet_commander_id')
        if not fleet_commander_id:
            return JsonResponse({"status": "error", "message": "FC Character is required."}, status=400)
            
        try:
            # 1. Validate FC character and get token
            fc_character = EveCharacter.objects.get(
                character_id=fleet_commander_id, 
                user=request.user
            )
            token = get_refreshed_token_for_character(request.user, fc_character)

            # 2. Check for required ESI scope
            required_scope = 'esi-fleets.read_fleet.v1'
            available_scopes = set(s.name for s in token.scopes.all())
            if required_scope not in available_scopes:
                return JsonResponse({
                    "status": "error", 
                    "message": f"Missing required scope: {required_scope}. Please log in again using the 'FC Scopes' option."
                }, status=403)

            # 3. Initialize ESI client
            esi = EsiClientProvider()

            # 4. Make ESI call to get fleet info
            try:
                fleet_info = esi.client.Fleets.get_characters_character_id_fleet(
                    character_id=fc_character.character_id,
                    token=token.access_token
                ).results()
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    return JsonResponse({"status": "error", "message": "You are not currently in a fleet."}, status=400)
                else:
                    raise e

            # 5. Check if character is the fleet boss
            if fleet_info.get('role') != 'fleet_commander':
                return JsonResponse({"status": "error", "message": "You are not the Fleet Commander (Boss) of your current fleet."}, status=403)

            # 6. Get the new ESI Fleet ID
            new_esi_fleet_id = fleet_info.get('fleet_id')
            if not new_esi_fleet_id:
                return JsonResponse({"status": "error", "message": "Could not fetch new Fleet ID from ESI."}, status=500)

            # 7. Update the existing Fleet object
            fleet = open_waitlist.fleet
            fleet.fleet_commander = fc_character
            fleet.esi_fleet_id = new_esi_fleet_id
            fleet.save()
            
            return JsonResponse({"status": "success", "message": f"Waitlist successfully taken over by {fc_character.character_name} (Fleet ID: {new_esi_fleet_id})."})
            
        except EveCharacter.DoesNotExist:
            # --- THIS IS THE FIX ---
            return JsonResponse({"status": "error", "message": "Invalid FC character selected."}, status=403)
            # --- END FIX ---
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)

    return JsonResponse({"status": "error", "message": "Invalid action."}, status=400)