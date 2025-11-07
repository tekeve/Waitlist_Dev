from django.shortcuts import render, redirect, get_object_or_404, resolve_url
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse, Http404
from django.contrib import messages
# --- MODIFIED: Import new model ---
from .models import EveCharacter, ShipFit, Fleet, FleetWaitlist, DoctrineFit, FitSubstitutionGroup
# --- END MODIFIED ---
# --- MODIFIED: Import EveGroup as well ---
from pilot.models import EveType, EveGroup
# --- NEW: Import from our new fit_parser.py ---
from .fit_parser import parse_eft_fit, check_fit_against_doctrines
# --- END NEW ---
from django.utils import timezone # Import timezone
import random
# --- REMOVED: re, json, Counter ---
import json # --- MODIFIED: Keep json for api_get_fit_details ---
from collections import Counter # --- MODIFIED: Keep Counter for api_get_fit_details ---

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


# --- REMOVED: SDE CACHING HELPER FUNCTIONS ---
# (get_or_cache_eve_group and get_or_cache_eve_type are now in fit_parser.py)
# --- END REMOVAL ---

# --- REMOVED: AUTO-APPROVAL HELPER ---
# (check_fit_against_doctrines is now in fit_parser.py)
# --- END REMOVAL ---


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
    # --- UPDATED: Consolidate MAR categories ---
    xup_fits = all_fits.filter(status='PENDING') if open_waitlist else []
    dps_fits = all_fits.filter(status='APPROVED', category__in=['DPS', 'MAR_DPS']) if open_waitlist else []
    logi_fits = all_fits.filter(status='APPROVED', category='LOGI') if open_waitlist else []
    sniper_fits = all_fits.filter(status='APPROVED', category__in=['SNIPER', 'MAR_SNIPER']) if open_waitlist else []
    other_fits = all_fits.filter(status='APPROVED', category='OTHER') if open_waitlist else []
    
    is_fc = request.user.groups.filter(name='Fleet Commander').exists()
    
    context = {
        'xup_fits': xup_fits,
        'dps_fits': dps_fits,
        'logi_fits': logi_fits,
        'sniper_fits': sniper_fits,
        'other_fits': other_fits,
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
    
    --- HEAVILY MODIFIED: Now uses fit_parser.py ---
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
    # --- NEW REFACTORED PARSING LOGIC ---
    # ---
    try:
        # 1. Call the centralized parser
        ship_type, parsed_fit_list, fit_summary_counter = parse_eft_fit(raw_fit_original)
        ship_type_id = ship_type.type_id

        # 2. Check for Auto-Approval
        doctrine, new_status, new_category = check_fit_against_doctrines(
            ship_type_id,
            dict(fit_summary_counter)
        )

        # 3. Save to database
        fit, created = ShipFit.objects.update_or_create(
            character=character,
            waitlist=open_waitlist,
            status__in=['PENDING', 'APPROVED', 'IN_FLEET'], # Find any existing fit
            defaults={
                'raw_fit': raw_fit_original,  # Save the *original* fit
                'parsed_fit_json': json.dumps(parsed_fit_list), # Save the parsed data
                'status': new_status, # 'PENDING' or 'APPROVED'
                'waitlist': open_waitlist,
                'ship_name': ship_type.name,
                'ship_type_id': ship_type_id,
                'tank_type': 'Shield',        # <-- Placeholder
                'fit_issues': None,           # <-- Placeholder
                'category': new_category,     # 'NONE' or from doctrine
                'submitted_at': timezone.now()
            }
        )
        
        if created:
            return JsonResponse({"status": "success", "message": f"Fit for {character.character_name} submitted!"})
        else:
            return JsonResponse({"status": "success", "message": f"Fit for {character.character_name} updated."})

    except ValueError as e:
        # Catch parsing errors raised from parse_eft_fit
        return JsonResponse({"status": "error", "message": str(e)}, status=400)
    except Exception as e:
        # Catch other unexpected issues
        return JsonResponse({"status": "error", "message": f"An unexpected error occurred: {str(e)}"}, status=500)
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
        
        # --- UPDATED: Assign to 'OTHER' instead of random ---
        # We only do this if it wasn't auto-assigned
        if fit.category == ShipFit.FitCategory.NONE:
            fit.category = ShipFit.FitCategory.OTHER # <-- MODIFIED
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
    # --- UPDATED: Consolidate MAR categories ---
    xup_fits = all_fits.filter(status='PENDING')
    dps_fits = all_fits.filter(status='APPROVED', category__in=['DPS', 'MAR_DPS'])
    logi_fits = all_fits.filter(status='APPROVED', category='LOGI')
    sniper_fits = all_fits.filter(status='APPROVED', category__in=['SNIPER', 'MAR_SNIPER'])
    other_fits = all_fits.filter(status='APPROVED', category='OTHER')
    
    is_fc = request.user.groups.filter(name='Fleet Commander').exists()

    context = {
        'xup_fits': xup_fits,
        'dps_fits': dps_fits,
        'logi_fits': logi_fits,
        'sniper_fits': sniper_fits,
        'other_fits': other_fits,
        'is_fc': is_fc,
    }
    
    return render(request, '_waitlist_columns.html', context)


# --- NEW API VIEW: Get Fit Details for Modal ---
@login_required
def api_get_fit_details(request):
    """
    Returns the parsed fit JSON for the FC's inspection modal.
    
    --- MODIFIED TO DO COMPARISON AND RETURN FULL FIT ---
    """
    if not is_fleet_commander(request.user):
        return JsonResponse({"status": "error", "message": "Not authorized"}, status=403)
        
    fit_id = request.GET.get('fit_id')
    if not fit_id:
        return HttpResponseBadRequest("Missing fit_id")
        
    try:
        fit = get_object_or_404(ShipFit, id=fit_id)
        
        # 1. Get the pilot's submitted fit list and summary
        try:
            full_fit_list = json.loads(fit.parsed_fit_json) if fit.parsed_fit_json else []
        except json.JSONDecodeError:
            full_fit_list = [] # Handle corrupted JSON
            
        submitted_items_to_check = Counter(fit.get_parsed_fit_summary())
        
        # 2. Get the best matching doctrine
        doctrine = DoctrineFit.objects.filter(ship_type__type_id=fit.ship_type_id).first()
        
        # --- MODIFIED: Create new list for frontend ---
        full_fit_list_with_status = []
        
        if not doctrine:
            # No doctrine found, mark all items (except hull) as problems
            hull_id_str = str(fit.ship_type_id)
            for item in full_fit_list:
                item_id_str = str(item.get('type_id'))
                if not item_id_str or item_id_str == 'None' or item_id_str == hull_id_str:
                    item['status'] = 'doctrine' # Treat hull/empty slots as fine
                else:
                    item['status'] = 'problem'
                    item['potential_matches'] = [] # No doctrine, so no matches
                full_fit_list_with_status.append(item)
            
            return JsonResponse({
                "full_fit_with_status": full_fit_list_with_status,
                "missing_items": [],
                "doctrine_name": "No Doctrine Found"
            })

        # 3. Get doctrine items and substitution maps
        doctrine_items_to_fill = Counter(doctrine.get_fit_items())
        sub_groups = FitSubstitutionGroup.objects.prefetch_related('substitutes').all()
        
        # sub_map: {'base_id': {base_id, sub_id1, sub_id2}}
        sub_map = {}
        # reverse_sub_map: {'sub_id': 'base_id'}
        reverse_sub_map = {}
        
        for group in sub_groups:
            base_id_str = str(group.base_item_id)
            allowed_ids = {str(sub.type_id) for sub in group.substitutes.all()}
            allowed_ids.add(base_id_str) # The base item is always allowed
            sub_map[base_id_str] = allowed_ids
            
            for sub_id_str in allowed_ids:
                if sub_id_str != base_id_str:
                    reverse_sub_map[sub_id_str] = base_id_str

        # 4. Perform the comparison
        
        # --- Create a copy to track remaining needed items
        doctrine_items_to_fill_copy = doctrine_items_to_fill.copy()
        
        # --- Lists to gather IDs for SDE enrichment
        problem_potential_match_ids = set()
        accepted_sub_base_ids = set()

        # --- Pass 1 & 2: Mark Exact Matches and Accepted Subs
        for item in full_fit_list:
            item_id_str = str(item.get('type_id'))
            if not item_id_str or item_id_str == 'None':
                item['status'] = 'doctrine' # Empty slots are fine
                full_fit_list_with_status.append(item)
                continue
            
            qty_in_fit = item.get('quantity', 1) # Get qty from the parsed list item

            # Check for exact match
            if item_id_str in doctrine_items_to_fill_copy and doctrine_items_to_fill_copy[item_id_str] > 0:
                item['status'] = 'doctrine'
                doctrine_items_to_fill_copy[item_id_str] -= qty_in_fit
            
            # Check for accepted substitute
            elif item_id_str in reverse_sub_map:
                base_item_id = reverse_sub_map[item_id_str]
                if base_item_id in doctrine_items_to_fill_copy and doctrine_items_to_fill_copy[base_item_id] > 0:
                    item['status'] = 'accepted_sub'
                    item['substitutes_for_id'] = base_item_id # Store ID
                    accepted_sub_base_ids.add(base_item_id)
                    doctrine_items_to_fill_copy[base_item_id] -= qty_in_fit
            
            full_fit_list_with_status.append(item)

        # --- Pass 3: Mark Problems
        problem_types_map = {} # {p_id: p_type_obj}
        missing_types_map = {} # {m_id: m_type_obj}
        
        for item in full_fit_list_with_status:
            if 'status' in item: # Already processed
                continue
                
            item_id_str = str(item.get('type_id'))
            item['status'] = 'problem'
            item['potential_matches'] = [] # Default
            
            # Find potential matches from the same group
            try:
                p_type = EveType.objects.get(type_id=item_id_str)
                problem_types_map[p_type.type_id] = p_type
                
                # Find missing doctrine items from the same group
                missing_ids_in_group = {
                    m_id_str for m_id_str, qty in doctrine_items_to_fill_copy.items() 
                    if qty > 0
                }
                
                if missing_ids_in_group:
                    missing_in_group = EveType.objects.filter(
                        type_id__in=missing_ids_in_group, 
                        group=p_type.group
                    )
                    for m_type in missing_in_group:
                        # --- FIX: Ensure we only suggest matches that are still needed ---
                        if doctrine_items_to_fill_copy.get(str(m_type.type_id), 0) > 0:
                            missing_types_map[m_type.type_id] = m_type
                            problem_potential_match_ids.add(m_type.type_id)
                            item['potential_matches'].append(m_type.type_id) # Store ID
            except EveType.DoesNotExist:
                pass # Unknown item, no potential matches


        # 5. Enrich the lists
        
        # --- Get EveType info for all referenced IDs
        all_referenced_ids = problem_potential_match_ids | accepted_sub_base_ids
        referenced_types = {
            str(t.type_id): t for t in EveType.objects.filter(type_id__in=all_referenced_ids)
        }

        # --- Loop back and populate names/icons
        for item in full_fit_list_with_status:
            if item.get('status') == 'accepted_sub':
                base_id = str(item['substitutes_for_id'])
                base_type = referenced_types.get(base_id)
                if base_type:
                    item['substitutes_for'] = [{ # Store as list
                        "name": base_type.name, 
                        "type_id": base_type.type_id, 
                        "icon_url": base_type.icon_url,
                        "quantity": doctrine_items_to_fill.get(base_id, 0) 
                    }]
            
            elif item.get('status') == 'problem':
                matches = []
                for match_id in item.get('potential_matches', []):
                    match_type = referenced_types.get(str(match_id))
                    # --- FIX: Check match_type and that it's still needed ---
                    if match_type and doctrine_items_to_fill_copy.get(str(match_id), 0) > 0:
                        matches.append({
                            "name": match_type.name, 
                            "type_id": match_type.type_id, 
                            "icon_url": match_type.icon_url,
                            "quantity": doctrine_items_to_fill_copy.get(str(match_id), 0)
                        })
                item['potential_matches'] = matches

        # 6. Get EveType info for the "Missing Items" column
        final_missing_ids = {
            m_id_str for m_id_str, qty in doctrine_items_to_fill_copy.items() 
            if qty > 0 and str(m_id_str) != str(fit.ship_type_id)
        }
        missing_types = EveType.objects.filter(type_id__in=final_missing_ids)
        missing_items = [{
            "type_id": t.type_id, 
            "name": t.name, 
            "icon_url": t.icon_url, 
            "quantity": doctrine_items_to_fill_copy[str(t.type_id)]
        } for t in missing_types]

        return JsonResponse({
            "full_fit_with_status": full_fit_list_with_status,
            "missing_items": missing_items, # Still useful for the "Make Sub" dropdown
            "doctrine_name": doctrine.name
        })

    except Http404:
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)
# --- END MODIFICATION ---


# --- NEW API VIEW: Add Substitution ---
@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_add_substitution(request):
    """
    Handles an FC's request to add a new substitution.
    """
    base_item_id = request.POST.get('base_item_id')
    substitute_item_id = request.POST.get('substitute_item_id')
    
    if not base_item_id or not substitute_item_id:
        return JsonResponse({"status": "error", "message": "Missing item IDs."}, status=400)
        
    try:
        base_item = EveType.objects.get(type_id=base_item_id)
        sub_item = EveType.objects.get(type_id=substitute_item_id)
        
        # Find or create the substitution group for this base item
        group, created = FitSubstitutionGroup.objects.get_or_create(
            base_item=base_item,
            defaults={'name': f"Substitutes for {base_item.name}"}
        )
        
        # Add the new item to the group
        group.substitutes.add(sub_item)
        
        return JsonResponse({
            "status": "success",
            "message": f"Added '{sub_item.name}' as a substitute for '{base_item.name}'."
        })

    except EveType.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Item not found in database."}, status=404)
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)
# --- END NEW VIEW ---


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