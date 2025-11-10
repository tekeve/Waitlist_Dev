from django.shortcuts import render, redirect, get_object_or_404, resolve_url
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse, Http404
from django.contrib import messages
# --- MODIFIED: Import new models ---
from .models import (
    EveCharacter, ShipFit, Fleet, FleetWaitlist, DoctrineFit,
    FitSubstitutionGroup, FleetWing, FleetSquad,
    # --- *** NEW: Import new rule/data models *** ---
    ItemComparisonRule, EveTypeDogmaAttribute
)
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
# --- MODIFIED: Import ESI exceptions ---
from bravado.exception import HTTPNotFound
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


# ---
# --- NEW: HELPER FUNCTION (Moved from api_fc_manage_waitlist)
# ---
def _update_fleet_structure(esi, fc_character, token, fleet_id, fleet_obj):
    """
    Pulls ESI fleet structure and saves it to the DB.
    *** This preserves existing category mappings. ***
    """
    # 1. Get wings from ESI
    wings = esi.client.Fleets.get_fleets_fleet_id_wings(
        fleet_id=fleet_id,
        token=token.access_token
    ).results()
    
    # 2. Get all *existing* category mappings from the DB before clearing
    existing_mappings = {
        s.squad_id: s.assigned_category
        for s in FleetSquad.objects.filter(wing__fleet=fleet_obj)
        if s.assigned_category is not None
    }

    # 3. Clear old structure
    FleetWing.objects.filter(fleet=fleet_obj).delete() # This cascades and deletes squads
    
    # 4. Create new wings
    for wing in wings:
        new_wing = FleetWing.objects.create(
            fleet=fleet_obj,
            wing_id=wing['id'],
            name=wing['name']
        )
        
        # 5. Create new squads
        for squad in wing['squads']:
            # Restore category if this squad_id existed before
            restored_category = existing_mappings.get(squad['id'])
            
            FleetSquad.objects.create(
                wing=new_wing,
                squad_id=squad['id'],
                name=squad['name'], # Use the name from ESI
                assigned_category=restored_category # Restore the mapping
            )
# ---
# --- END HELPER FUNCTION
# ---


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
            # --- MODIFIED: Don't show IN_FLEET pilots ---
            status__in=['PENDING', 'APPROVED']
            # --- END MODIFIED ---
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
    

# --- NEW: Fittings View ---
@login_required
def fittings_view(request):
    """
    Displays all available doctrine fits for all users to see.
    """
    # --- MODIFICATION: Group fits by category ---
    
    # 1. Define the category order and display names
    categories_map = {
        'LOGI': {'name': 'Logi', 'fits': []},
        'DPS': {'name': 'DPS', 'fits': []},
        'SNIPER': {'name': 'Sniper', 'fits': []},
        'MAR_DPS': {'name': 'MAR DPS', 'fits': []},
        'MAR_SNIPER': {'name': 'MAR Sniper', 'fits': []},
        'OTHER': {'name': 'Other', 'fits': []},
    }

    # 2. Get all fits, ordered correctly
    all_fits_list = DoctrineFit.objects.all().select_related('ship_type').order_by('category', 'name')
    
    # 3. Sort fits into the map
    for fit in all_fits_list:
        if fit.category in categories_map:
            categories_map[fit.category]['fits'].append(fit)
        elif fit.category != 'NONE':
            # Fallback for any other categories
            if 'OTHER' not in categories_map:
                categories_map['OTHER'] = {'name': 'Other', 'fits': []}
            categories_map['OTHER']['fits'].append(fit)

    # 4. Create a final list, filtering out empty categories
    grouped_fits = [data for data in categories_map.values() if data['fits']]
    # --- END MODIFICATION ---

    # 5. Get context variables needed by base.html
    is_fc = request.user.groups.filter(name='Fleet Commander').exists()
    user_characters = EveCharacter.objects.filter(user=request.user)
    
    context = {
        'grouped_fits': grouped_fits, # Pass the new grouped list
        'is_fc': is_fc,
        'user_characters': user_characters,
    }
    
    return render(request, 'fittings_view.html', context)
# --- END NEW VIEW ---


# ---
# --- THIS IS THE FIX: This function now trusts the parser's 'final_slot'
# ---
def _build_slotted_fit_context(ship_eve_type, parsed_fit_list):
    """
    Takes a ship's EveType and a parsed fit list (from JSON)
    and returns a fully slotted fit dictionary.
    
    This now trusts the 'final_slot' provided by the parser.
    """
    
    # 1. Get base slot counts from the ship's EveType (for display)
    slot_counts = {
        'high': int(ship_eve_type.hi_slots or 0),
        'mid': int(ship_eve_type.med_slots or 0),
        'low': int(ship_eve_type.low_slots or 0),
        'rig': int(ship_eve_type.rig_slots or 0),
        'subsystem': int(ship_eve_type.subsystem_slots or 0)
    }
    
    # 2. Check if this is a T3 Cruiser
    is_t3c = slot_counts['subsystem'] > 0
    
    # 3. Get all item EveTypes from the DB in one query
    item_ids = [item['type_id'] for item in parsed_fit_list if item.get('type_id')]
    item_types_map = {t.type_id: t for t in EveType.objects.filter(type_id__in=item_ids)}

    # 4. Create bins for all fitted items
    item_bins = {
        'high': [], 'mid': [], 'low': [], 'rig': [], 
        'subsystem': [], 'drone': [], 'cargo': [],
        'ship': [], 'BLANK_LINE': []
    }
    
    for item in parsed_fit_list:
        # ---
        # --- THIS IS THE FIX ---
        # ---
        # Trust the parser's 'final_slot' designation
        final_slot = item.get('final_slot')
        if not final_slot or final_slot not in item_bins:
            final_slot = 'cargo' # Fallback
        # ---
        # --- END THE FIX ---
        # ---
            
        type_id = item.get('type_id')
        item_type = item_types_map.get(type_id) if type_id else None
        
        # Build the item object
        item_obj = {
            "type_id": type_id,
            "name": item.get('name', 'Unknown'),
            "icon_url": item.get('icon_url'), # <-- Get icon_url from the parsed JSON
            "quantity": item.get('quantity', 1),
            "raw_line": item.get('raw_line', item.get('name', 'Unknown')),
            # An item is "empty" if it's a fittable slot and has no type_id
            "is_empty": (final_slot in ['high','mid','low','rig','subsystem'] and not type_id)
        }

        if item_type:
            # Overwrite with canonical data from DB
            item_obj['name'] = item_type.name
            # --- *** THIS IS THE FIX: This line is removed *** ---
            # item_obj['icon_url'] = item_type.icon_url # <-- BUG! This field no longer exists.
            # --- *** END THE FIX *** ---

        if final_slot == 'BLANK_LINE':
            # We don't add blank lines to the final display
            continue
        
        # Add to the correct bin
        item_bins[final_slot].append(item_obj)


    # 5. Create the final slotted structure
    final_slots = {}
    
    # ---
    # --- THIS IS THE FIX: Re-introduce padding logic, remove overflow logic
    # ---
    if is_t3c:
        # T3Cs don't get padded, just show what's fitted
        final_slots = {
            'high': item_bins['high'],
            'mid': item_bins['mid'],
            'low': item_bins['low'],
            'rig': item_bins['rig'],
            'subsystem': item_bins['subsystem'],
        }
        # Update slot_counts to match fitted count for T3Cs
        slot_counts['high'] = len(item_bins['high'])
        slot_counts['mid'] = len(item_bins['mid'])
        slot_counts['low'] = len(item_bins['low'])
        slot_counts['rig'] = len(item_bins['rig'])
    else:
        # Regular ships get padded with empty slots
        for slot_key in ['high', 'mid', 'low', 'rig', 'subsystem']:
            total_slots = slot_counts[slot_key]
            # ---
            # --- THIS IS THE FIX: Build the slot_list from the bin FIRST
            # ---
            slot_list = []
            
            # Add all items the parser put in this bin
            # (This includes fitted items AND empty slots from the fit)
            fitted_items = item_bins[slot_key]
            for item in fitted_items:
                slot_list.append(item)
            
            # Now, pad with default empty slots if needed
            empty_slot_name = f"[Empty {slot_key.capitalize()} Slot]"
            while len(slot_list) < total_slots:
                slot_list.append({
                    "name": empty_slot_name, 
                    "is_empty": True,
                    "raw_line": empty_slot_name,
                    "type_id": None
                })
            # ---
            # --- END THE FIX
            # ---
            
            final_slots[slot_key] = slot_list

    # Drones and cargo are just lists
    final_slots['drone'] = item_bins['drone']
    final_slots['cargo'] = item_bins['cargo']
    # ---
    # --- END THE FIX ---
    # ---

    return {
        "ship": {
            "type_id": ship_eve_type.type_id,
            "name": ship_eve_type.name,
            # --- *** THIS IS THE FIX: Build URL from type_id *** ---
            "icon_url": f"https://images.evetech.net/types/{ship_eve_type.type_id}/render?size=128"
            # --- *** END THE FIX *** ---
        },
        "slots": final_slots,
        "slot_counts": slot_counts,
        "is_t3c": is_t3c
    }
# ---
# --- END NEW HELPER
# ---


# ---
# --- *** NEW HELPER: Attribute value getter *** ---
# ---
def _get_attribute_value_from_item(item_type: EveType, attribute_id: int) -> float:
    """
    Safely gets a single attribute value from an EveType's cached attribute dict.
    Returns 0 if the attribute is not found.
    
    This helper assumes a cache `_attribute_cache` is pre-populated
    on the EveType object by the calling function.
    """
    if not hasattr(item_type, '_attribute_cache'):
        # This is a fallback, but should not be hit in production
        try:
            attr_obj = EveTypeDogmaAttribute.objects.get(type=item_type, attribute_id=attribute_id)
            return attr_obj.value or 0
        except EveTypeDogmaAttribute.DoesNotExist:
            return 0
                
    # Return the value from the pre-populated cache
    return item_type._attribute_cache.get(attribute_id, 0)
# ---
# --- *** END NEW HELPER ***
# ---


# --- NEW API VIEW for Doctrine Fit Modal ---
@login_required
def api_get_doctrine_fit_details(request):
    """
    Returns the details for a specific doctrine fit.
    This is for the public fittings page modal.
    
    --- MODIFIED TO USE NEW SLOTTED STRUCTURE ---
    """
    fit_id = request.GET.get('fit_id')
    if not fit_id:
        return HttpResponseBadRequest("Missing fit_id")
        
    try:
        doctrine = get_object_or_404(DoctrineFit, id=fit_id)
        
        # 1. Get the ship's EveType
        ship_eve_type = doctrine.ship_type
        if not ship_eve_type:
            raise Http404("Doctrine fit is missing a ship type.")
            
        # 2. Get the parsed list of items
        parsed_list = doctrine.get_parsed_fit_list()
        if not parsed_list:
            # Fallback: re-parse from raw EFT
            if doctrine.raw_fit_eft:
                _, parsed_list, _ = parse_eft_fit(doctrine.raw_fit_eft)
            else:
                parsed_list = [] # No data

        # 3. Build the slotted context
        # ---
        # --- THIS IS THE FIX: Call the corrected helper function
        # ---
        slotted_context = _build_slotted_fit_context(ship_eve_type, parsed_list)
        # ---
        # --- END THE FIX ---
        # ---

        # 4. Return the new structure + the raw EFT for copying
        return JsonResponse({
            "status": "success",
            "name": doctrine.name,
            "raw_eft": doctrine.raw_fit_eft,
            "slotted_fit": slotted_context # <-- NEW
        })
        
    except Http404:
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)
# --- END NEW API VIEW ---


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
            # --- MODIFIED: Find PENDING or APPROVED fits to update ---
            status__in=['PENDING', 'APPROVED'], # Find any existing fit
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
                'submitted_at': timezone.now(),
                'last_updated': timezone.now(), # --- ADDED: Force update timestamp ---
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
        # --- MODIFIED: Don't show IN_FLEET pilots ---
        status__in=['PENDING', 'APPROVED']
        # --- END MODIFIED ---
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


# ---
# --- THIS IS THE FIX: This function now trusts the parser's 'final_slot'
# ---
@login_required
def api_get_fit_details(request):
    """
    Returns the parsed fit JSON for the FC's inspection modal.
    
    --- HEAVILY MODIFIED TO TRUST THE PARSER'S 'final_slot' ---
    """
    if not is_fleet_commander(request.user):
        return JsonResponse({"status": "error", "message": "Not authorized"}, status=403)
        
    fit_id = request.GET.get('fit_id')
    if not fit_id:
        return HttpResponseBadRequest("Missing fit_id")
        
    try:
        fit = get_object_or_404(ShipFit, id=fit_id)
        
        # 1. Get the ship's EveType and base slot counts
        ship_eve_type = EveType.objects.filter(type_id=fit.ship_type_id).first()
        if not ship_eve_type:
            return JsonResponse({"status": "error", "message": "Ship hull not found in SDE cache."}, status=404)
            
        slot_counts = {
            'high': int(ship_eve_type.hi_slots or 0),
            'mid': int(ship_eve_type.med_slots or 0),
            'low': int(ship_eve_type.low_slots or 0),
            'rig': int(ship_eve_type.rig_slots or 0),
            'subsystem': int(ship_eve_type.subsystem_slots or 0)
        }
        is_t3c = slot_counts['subsystem'] > 0

        # 2. Get the pilot's submitted fit list
        try:
            full_fit_list = json.loads(fit.parsed_fit_json) if fit.parsed_fit_json else []
        except json.JSONDecodeError:
            full_fit_list = [] # Handle corrupted JSON
            
        # 3. Get the best matching doctrine
        doctrine = DoctrineFit.objects.filter(ship_type__type_id=fit.ship_type_id).first()
        
        # 4. --- START NEW COMPARISON LOGIC ---
        
        # 4a. Get all EveTypes for items in the fit
        item_ids = [item['type_id'] for item in full_fit_list if item.get('type_id')]
        # --- *** NEW: Add all doctrine items to this map *** ---
        if doctrine:
            item_ids.extend(int(k) for k in doctrine.get_fit_items().keys())
        item_types_map = {
            t.type_id: t 
            for t in EveType.objects.filter(type_id__in=set(item_ids)).select_related('group', 'group__category')
        }
        # --- *** END NEW *** ---

        # 4b. Get doctrine items and substitution maps
        doctrine_items_to_fill = Counter()
        doctrine_name = "No Doctrine Found"
        if doctrine:
            doctrine_name = doctrine.name
            doctrine_items_to_fill = Counter(doctrine.get_fit_items())
            
        sub_groups = FitSubstitutionGroup.objects.prefetch_related('substitutes').all()
        sub_map = {} # { 'base_id': {set of allowed_ids} }
        reverse_sub_map = {} # { 'sub_id': 'base_id' }
        
        for group in sub_groups:
            base_id_str = str(group.base_item_id)
            allowed_ids = {sub.type_id for sub in group.substitutes.all()}
            allowed_ids.add(group.base_item_id)
            sub_map[base_id_str] = allowed_ids
            for sub_id in allowed_ids:
                if sub_id != group.base_item_id:
                    reverse_sub_map[str(sub_id)] = base_id_str

        # --- *** NEW: Pre-cache all attributes for these types *** ---
        attribute_values_by_type = {}
        dogma_attrs = EveTypeDogmaAttribute.objects.filter(type_id__in=item_types_map.keys()).values('type_id', 'attribute_id', 'value')
        
        for attr in dogma_attrs:
            type_id = attr['type_id']
            if type_id not in attribute_values_by_type:
                attribute_values_by_type[type_id] = {}
            attribute_values_by_type[type_id][attr['attribute_id']] = attr['value'] or 0

        # Now, attach this cache to each EveType object
        for type_id, item_type in item_types_map.items():
            item_type._attribute_cache = attribute_values_by_type.get(type_id, {})
        # --- *** END NEW *** ---

        # --- *** NEW: Get all ItemComparisonRules in one query *** ---
        all_rules = ItemComparisonRule.objects.select_related('attribute').all()
        rules_by_group = {}
        for rule in all_rules:
            if rule.group_id not in rules_by_group:
                rules_by_group[rule.group_id] = []
            rules_by_group[rule.group_id].append(rule)
        # --- *** END NEW *** ---

        # 4c. Create bins to sort items into
        item_bins = {
            'high': [], 'mid': [], 'low': [], 'rig': [], 
            'subsystem': [], 'drone': [], 'cargo': [],
            'ship': [], 'BLANK_LINE': []
        }
        
        # 4d. Create a copy of doctrine items to "consume" as we find matches
        doctrine_items_to_fill_copy = doctrine_items_to_fill.copy()
        
        # ---
        # --- THIS IS THE FIX: A new single loop that trusts 'final_slot'
        # ---
        for item in full_fit_list:
            final_slot = item.get('final_slot')
            if not final_slot or final_slot == 'ship':
                continue # Skip hull

            type_id = item.get('type_id')
            item_type = item_types_map.get(type_id) if type_id else None
            
            # Create the item object
            item_obj = {
                "type_id": type_id,
                "name": item.get('name', 'Unknown'),
                "icon_url": item.get('icon_url'), # <-- Get icon_url from the parsed JSON
                "quantity": item.get('quantity', 1),
                "raw_line": item.get('raw_line', item.get('name', 'Unknown')),
                # An item is "empty" if it's a fittable slot and has no type_id
                "is_empty": (final_slot in ['high','mid','low','rig','subsystem'] and not type_id),
                "status": "doctrine", # Default
                "potential_matches": [],
                "substitutes_for": [],
                "failure_reasons": [], # --- *** NEW: Add failure list *** ---
            }
            
            if item_type:
                # Overwrite with canonical data from DB
                item_obj['name'] = item_type.name
                # --- *** THIS IS THE FIX: This line is removed *** ---
                # item_obj['icon_url'] = item_type.icon_url # <-- BUG! This field no longer exists.
                # --- *** END THE FIX *** ---

            if final_slot == 'BLANK_LINE':
                # We don't add blank lines to the final display
                continue
            
            if item_obj['is_empty']:
                item_obj['status'] = 'empty'
                item_bins[final_slot].append(item_obj)
                continue # No comparison needed

            # --- Start Comparison Logic (from old loop) ---
            if type_id:
                item_id_str = str(type_id)
                qty_in_fit = item.get('quantity', 1)

                if not doctrine:
                    # No doctrine. If it's a fittable item (not drone/cargo), mark as problem
                    if final_slot not in ['drone', 'cargo']:
                        item_obj['status'] = 'problem'
                else:
                    # We have a doctrine, check for match
                    if item_id_str in doctrine_items_to_fill_copy and doctrine_items_to_fill_copy[item_id_str] > 0:
                        # Exact Match
                        item_obj['status'] = 'doctrine'
                        doctrine_items_to_fill_copy[item_id_str] -= qty_in_fit
                    
                    elif item_id_str in reverse_sub_map:
                        # Manual Substitute Match
                        base_item_id = int(reverse_sub_map[item_id_str])
                        if str(base_item_id) in doctrine_items_to_fill_copy and doctrine_items_to_fill_copy[str(base_item_id)] > 0:
                            item_obj['status'] = 'accepted_sub'
                            base_type = item_types_map.get(base_item_id) # Get sub info
                            if base_type:
                                item_obj['substitutes_for'] = [{
                                    "name": base_type.name,
                                    "type_id": base_type.type_id,
                                    "icon_url": f"https://images.evetech.net/types/{base_type.type_id}/icon?size=32",
                                    "quantity": doctrine_items_to_fill.get(str(base_item_id), 0)
                                }]
                            doctrine_items_to_fill_copy[str(base_item_id)] -= qty_in_fit
                        else:
                            # It's a sub for an item, but not one we need (or we have enough)
                            item_obj['status'] = 'problem'
                    
                    # --- *** NEW: Automatic "Equal or Better" Check *** ---
                    elif (item_type and item_type.group_id):
                        
                        # Find a doctrine item of the same group that is missing
                        found_match = False
                        for doctrine_id_str, missing_qty in doctrine_items_to_fill_copy.items():
                            if missing_qty <= 0:
                                continue
                            
                            doctrine_item_type = item_types_map.get(int(doctrine_id_str))
                            if not doctrine_item_type or not doctrine_item_type.group:
                                continue

                            # Check group and category
                            if (doctrine_item_type.group_id == item_type.group_id and
                                doctrine_item_type.group.category_id == item_type.group.category_id):
                                
                                # Found a potential match! Now check attributes.
                                comparison_rules = rules_by_group.get(item_type.group_id, [])
                                if not comparison_rules:
                                    continue # No rules, so no auto-sub
                                
                                is_equal_or_better = True
                                failure_reasons = []
                                for rule in comparison_rules:
                                    attr_id = rule.attribute.attribute_id
                                    doctrine_val = _get_attribute_value_from_item(doctrine_item_type, attr_id)
                                    submitted_val = _get_attribute_value_from_item(item_type, attr_id)
                                    
                                    if rule.higher_is_better:
                                        if submitted_val < doctrine_val:
                                            is_equal_or_better = False
                                            # --- *** THIS IS THE FIX *** ---
                                            failure_reasons.append({
                                                "attribute_name": rule.attribute.name,
                                                "doctrine_value": doctrine_val,
                                                "submitted_value": submitted_val
                                            })
                                            # --- *** END THE FIX *** ---
                                    else: # Lower is better
                                        if submitted_val > doctrine_val:
                                            is_equal_or_better = False
                                            # --- *** THIS IS THE FIX *** ---
                                            failure_reasons.append({
                                                "attribute_name": rule.attribute.name,
                                                "doctrine_value": doctrine_val,
                                                "submitted_value": submitted_val
                                            })
                                            # --- *** END THE FIX *** ---
                                
                                if is_equal_or_better:
                                    item_obj['status'] = 'accepted_sub'
                                    item_obj['substitutes_for'] = [{
                                        "name": doctrine_item_type.name,
                                        "type_id": doctrine_item_type.type_id,
                                        "icon_url": f"https://images.evetech.net/types/{doctrine_item_type.type_id}/icon?size=32",
                                        "quantity": doctrine_items_to_fill.get(str(doctrine_item_type.type_id), 0)
                                    }]
                                    doctrine_items_to_fill_copy[doctrine_id_str] -= qty_in_fit
                                    found_match = True
                                    break # Stop checking other doctrine items
                                else:
                                    # It's a problem, and we know why
                                    item_obj['status'] = 'problem'
                                    item_obj['failure_reasons'] = failure_reasons # <-- Assign the reasons
                                    # We also add potential matches for the "Make Sub" button
                                    item_obj['potential_matches'] = [{
                                        "name": doctrine_item_type.name,
                                        "type_id": doctrine_item_type.type_id,
                                        "icon_url": f"https://images.evetech.net/types/{doctrine_item_type.type_id}/icon?size=32",
                                        "quantity": doctrine_items_to_fill_copy.get(str(doctrine_item_type.type_id), 0)
                                    }]
                                    found_match = True
                                    break # Stop checking other doctrine items
                        
                        if not found_match:
                             item_obj['status'] = 'problem' # No match found
                    # --- *** END NEW *** ---

                    else:
                        # No match, it's a problem
                        item_obj['status'] = 'problem'

                # Find potential matches for 'problem' items that *weren't* caught by the new logic
                # (e.g., completely wrong item group)
                if (item_obj['status'] == 'problem' and 
                    not item_obj['potential_matches'] and 
                    not item_obj['failure_reasons'] and 
                    item_type and item_type.group_id):
                    
                    missing_ids_in_group = {
                        int(m_id_str) for m_id_str, qty in doctrine_items_to_fill_copy.items() 
                        if qty > 0
                    }
                    if missing_ids_in_group:
                        missing_in_group = EveType.objects.filter(
                            group_id=item_type.group_id,
                            type_id__in=missing_ids_in_group
                        )
                        for m_type in missing_in_group:
                            item_obj['potential_matches'].append({
                                "name": m_type.name, 
                                "type_id": m_type.type_id, 
                                "icon_url": f"https://images.evetech.net/types/{m_type.type_id}/icon?size=32",
                                "quantity": doctrine_items_to_fill_copy.get(str(m_type.type_id), 0)
                            })
            # --- End Comparison Logic ---

            # Add to bin
            if final_slot in item_bins:
                item_bins[final_slot].append(item_obj)
            else:
                item_bins['cargo'].append(item_obj) # Fallback
        # ---
        # --- END THE NEW LOOP
        # ---
            
        # 5. Create the final slotted structure
        # ---
        # --- THIS IS THE FIX: Re-introduce padding logic
        # ---
        final_slots = {}
    
        if is_t3c:
            # T3Cs don't get padded, just show what's fitted
            final_slots = {
                'high': item_bins['high'],
                'mid': item_bins['mid'],
                'low': item_bins['low'],
                'rig': item_bins['rig'],
                'subsystem': item_bins['subsystem'],
            }
            # Update slot_counts to match fitted count for T3Cs
            slot_counts['high'] = len(item_bins['high'])
            slot_counts['mid'] = len(item_bins['mid'])
            slot_counts['low'] = len(item_bins['low'])
            slot_counts['rig'] = len(item_bins['rig'])
        else:
            # Regular ships get padded with empty slots
            for slot_key in ['high', 'mid', 'low', 'rig', 'subsystem']:
                total_slots = slot_counts[slot_key]
                # ---
                # --- THIS IS THE FIX: Build the slot_list from the bin FIRST
                # ---
                slot_list = []
                
                # Add all items the parser put in this bin
                fitted_items = item_bins[slot_key]
                for item in fitted_items:
                    slot_list.append(item)

                # Now, pad with default empty slots if needed
                empty_slot_name = f"[Empty {slot_key.capitalize()} Slot]"
                while len(slot_list) < total_slots:
                    slot_list.append({
                        "name": empty_slot_name, 
                        "is_empty": True,
                        "raw_line": empty_slot_name,
                        "type_id": None,
                        "status": "empty"
                    })
                # ---
                # --- END THE FIX
                # ---
                
                final_slots[slot_key] = slot_list

        # Drones and cargo are just lists
        final_slots['drone'] = item_bins['drone']
        final_slots['cargo'] = item_bins['cargo']
        # ---
        # --- END THE FIX ---
        # ---
            
        # 6. Find any remaining "Missing" items
        final_missing_ids = {
            int(m_id_str) for m_id_str, qty in doctrine_items_to_fill_copy.items() 
            if qty > 0 and int(m_id_str) != fit.ship_type_id
        }
        missing_types = EveType.objects.filter(type_id__in=final_missing_ids)
        missing_items = [{
            "type_id": t.type_id, 
            "name": t.name, 
            # --- *** THIS IS THE FIX: Build URL from type_id *** ---
            "icon_url": f"https://images.evetech.net/types/{t.type_id}/icon?size=32",
            # --- *** END THE FIX *** ---
            "quantity": doctrine_items_to_fill_copy[str(t.type_id)]
        } for t in missing_types]


        # 7. Return the full structure
        return JsonResponse({
            "status": "success",
            "name": f"{fit.character.character_name} vs. {doctrine_name}",
            "slotted_fit": {
                "ship": {
                    "type_id": ship_eve_type.type_id,
                    "name": ship_eve_type.name,
                    # --- *** THIS IS THE FIX: Build URL from type_id *** ---
                    "icon_url": f"https://images.evetech.net/types/{ship_eve_type.type_id}/render?size=128"
                    # --- *** END THE FIX *** ---
                },
                "slots": final_slots,
                "slot_counts": slot_counts,
                "is_t3c": is_t3c
            },
            "missing_items": missing_items, # For the 'Make Sub' dropdown
            "doctrine_name": doctrine_name
        })

    except Http404:
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)
    except Exception as e:
        # --- ADDED: Print exception for debugging ---
        import traceback
        print(traceback.format_exc())
        # --- END ADDED ---
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
    
    # ---
    # --- HELPER FUNCTION WAS MOVED OUTSIDE THIS VIEW ---
    # ---
    
    
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
            
            # --- NEW: Clear fleet structure ---
            FleetWing.objects.filter(fleet=fleet).delete()
            # --- END NEW ---
            
            # Deny all pending fits
            pending_fits = ShipFit.objects.filter(
                waitlist=open_waitlist,
                status='PENDING'
            )
            pending_fits.update(status='DENIED', denial_reason="Waitlist closed before approval.")
            
            return JsonResponse({"status": "success", "message": "Waitlist closed. All pending fits denied."})
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)

    # ---
    # --- MODIFIED: 'open' action no longer links to ESI
    # ---
    elif action == 'open':
        if open_waitlist:
            return JsonResponse({"status": "error", "message": "A waitlist is already open. Please close it first."}, status=400)

        fleet_id = request.POST.get('fleet_id')
        fleet_commander_id = request.POST.get('fleet_commander_id')

        if not all([fleet_id, fleet_commander_id]):
            return JsonResponse({"status": "error", "message": "Fleet Type and FC Character are required."}, status=400)
            
        try:
            fc_character = EveCharacter.objects.get(
                character_id=fleet_commander_id, 
                user=request.user
            )
            fleet_to_open = Fleet.objects.get(id=fleet_id, is_active=False)

            # 1. Update the selected Fleet
            fleet_to_open.fleet_commander = fc_character
            # --- REMOVED: ESI Fleet ID is no longer set here ---
            # fleet_to_open.esi_fleet_id = esi_fleet_id
            fleet_to_open.is_active = True
            fleet_to_open.save()
            
            # 2. Open its associated Waitlist
            waitlist, created = FleetWaitlist.objects.get_or_create(fleet=fleet_to_open)
            waitlist.is_open = True
            waitlist.save()
            
            return JsonResponse({"status": "success", "message": f"Waitlist '{fleet_to_open.description}' opened. Please link your in-game fleet."})
            
        except EveCharacter.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Invalid FC character selected."}, status=403)
        # --- MODIFIED: More specific error ---
        except Fleet.DoesNotExist:
            return JsonResponse({"status": "error", "message": "The fleet you selected is already open or does not exist."}, status=400)
        except Exception as e:
            # Catch other errors
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)
    # ---
    # --- END 'open' MODIFICATION
    # ---

    # ---
    # --- MODIFIED: 'takeover' action now links the fleet and pulls structure
    # ---
    elif action == 'takeover':
        if not open_waitlist:
            return JsonResponse({"status": "error", "message": "No waitlist is currently open to link a fleet to."}, status=400)
            
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

            # 2. Check for required ESI scopes
            # --- MODIFIED: Check for write scope as well ---
            required_scopes = [
                'esi-fleets.read_fleet.v1',
                'esi-fleets.write_fleet.v1'
            ]
            available_scopes = set(s.name for s in token.scopes.all())
            
            if not all(s in available_scopes for s in required_scopes):
                missing = [s for s in required_scopes if s not in available_scopes]
                return JsonResponse({
                    "status": "error", 
                    "message": f"Missing required FC scopes: {', '.join(missing)}. Please log in again using the 'Add FC Scopes' option."
                }, status=403)

            # 3. Initialize ESI client
            esi = EsiClientProvider()
            
            new_esi_fleet_id = None
            
            # 4. Make ESI call to get fleet info
            try:
                fleet_info = esi.client.Fleets.get_characters_character_id_fleet(
                    character_id=fc_character.character_id,
                    token=token.access_token
                ).results()
                
                # 5. Check if character is the fleet boss
                if fleet_info.get('role') != 'fleet_commander':
                    return JsonResponse({"status": "error", "message": "You are not the Fleet Commander (Boss) of your current fleet."}, status=403)

                # 6. Get the new ESI Fleet ID
                new_esi_fleet_id = fleet_info.get('fleet_id')

            # --- MODIFIED: Catch 404 and return error ---
            except HTTPNotFound as e:
                # 404 means user is not in a fleet.
                return JsonResponse({"status": "error", "message": "You are not in a fleet. Please create one in-game first, then link it."}, status=400)
            
            # --- End modification ---

            if not new_esi_fleet_id:
                return JsonResponse({"status": "error", "message": "Could not fetch new Fleet ID from ESI."}, status=500)

            # 7. Update the existing Fleet object
            fleet = open_waitlist.fleet
            fleet.fleet_commander = fc_character
            fleet.esi_fleet_id = new_esi_fleet_id
            fleet.save()
            
            # --- 8. NEW: Pull the fleet structure ---
            _update_fleet_structure(esi, fc_character, token, new_esi_fleet_id, fleet)
            # --- END NEW ---
            
            return JsonResponse({
                "status": "success", 
                "message": f"Waitlist successfully linked to fleet {new_esi_fleet_id} and structure updated.",
                "esi_fleet_id": new_esi_fleet_id
            })
            
        except EveCharacter.DoesNotExist:
            return JsonResponse({"status": "error", "message": "Invalid FC character selected."}, status=403)
        except Exception as e:
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)
    # ---
    # --- END 'takeover' MODIFICATION
    # ---

    # --- THIS IS THE FIX ---
    return JsonResponse({"status": "error", "message": "Invalid action."}, status=400)
    # --- END THE FIX ---


# ---
# --- NEW API VIEWS FOR FLEET MANAGEMENT
# ---
@login_required
@user_passes_test(is_fleet_commander)
def api_get_fleet_structure(request):
    """
    Returns the current fleet's wing/squad structure
    from the database.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "No waitlist is open."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id:
        return JsonResponse({"status": "error", "message": "Fleet is not linked to ESI."}, status=400)

    # 1. Get all wings and squads from our DB
    wings = FleetWing.objects.filter(fleet=fleet).prefetch_related('squads')
    
    # 2. Get available categories
    available_categories = [
        {"id": choice[0], "name": choice[1]}
        for choice in ShipFit.FitCategory.choices
        if choice[0] != 'NONE'
    ]

    # 3. Serialize the structure
    structure = {
        "wings": [],
        "available_categories": available_categories
    }
    
    # --- THIS IS THE FIX: Removed category-based sorting ---
    # --- Squads will now be sorted by their ID by default ---

    for wing in wings:
        wing_data = {
            "id": wing.wing_id,
            "name": wing.name,
            "squads": []
        }
        
        # --- THIS IS THE FIX: Order by squad_id to match in-game order ---
        for squad in wing.squads.order_by('squad_id'):
        # --- END FIX ---
            wing_data["squads"].append({
                "id": squad.squad_id,
                "name": squad.name,
                "assigned_category": squad.assigned_category
            })
        structure["wings"].append(wing_data)

    return JsonResponse({"status": "success", "structure": structure})


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_save_squad_mappings(request):
    """
    Saves the category-to-squad mappings AND new names.
    This now pushes name changes to ESI.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "No waitlist is open."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)
        
    try:
        # 1. Get FC token and ESI client
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        # 2. Parse incoming data
        data = json.loads(request.body)
        wing_data = data.get('wings', [])
        squad_data = data.get('squads', [])
        
        # 3. Get all wings/squads for this fleet from DB
        all_db_wings = {w.wing_id: w for w in fleet.wings.all()}
        all_db_squads = {s.squad_id: s for s in FleetSquad.objects.filter(wing__fleet=fleet)}
        
        # 4. Clear all existing category assignments
        FleetSquad.objects.filter(wing__fleet=fleet).update(assigned_category=None)
        
        # 5. Process Wing Name Changes
        for wing_info in wing_data:
            wing_id = int(wing_info['id'])
            new_name = wing_info['name']
            
            db_wing = all_db_wings.get(wing_id)
            if db_wing and db_wing.name != new_name:
                # Name changed, push to ESI
                esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
                    fleet_id=fleet.esi_fleet_id,
                    wing_id=wing_id,
                    naming={'name': new_name},
                    token=token.access_token
                ).results()
                # Update DB
                db_wing.name = new_name
                db_wing.save()

        # 6. Process Squad Name/Category Changes
        for squad_info in squad_data:
            squad_id = int(squad_info['id'])
            new_name = squad_info['name']
            new_category = squad_info['category']
            
            db_squad = all_db_squads.get(squad_id)
            if db_squad:
                # Check for name change
                if db_squad.name != new_name:
                    # Name changed, push to ESI
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet.esi_fleet_id,
                        squad_id=squad_id,
                        naming={'name': new_name},
                        token=token.access_token
                    ).results()
                
                # Update DB with new name and category
                db_squad.name = new_name
                db_squad.assigned_category = new_category
                db_squad.save()
        
        # ---
        # --- 7. NEW: Refresh structure from ESI and return it
        # ---
        _update_fleet_structure(
            esi, fc_character, token, 
            fleet.esi_fleet_id, fleet
        )
        
        # Get the new structure to return
        wings = FleetWing.objects.filter(fleet=fleet).prefetch_related('squads')
        available_categories = [
            {"id": choice[0], "name": choice[1]}
            for choice in ShipFit.FitCategory.choices
            if choice[0] != 'NONE'
        ]
        structure = { "wings": [], "available_categories": available_categories }
        
        # --- THIS IS THE FIX: Removed category-based sorting ---

        for wing in wings:
            # --- THIS IS THE FIX: Order by squad_id to match in-game order ---
            squads_list = wing.squads.order_by('squad_id')
            # --- END FIX ---

            wing_data = {
                "id": wing.wing_id,
                "name": wing.name,
                "squads": [{
                    "id": squad.squad_id,
                    "name": squad.name,
                    "assigned_category": squad.assigned_category
                } for squad in squads_list]
            }
            structure["wings"].append(wing_data)

        return JsonResponse({"status": "success", "structure": structure})
        
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid request data."}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_invite_pilot(request):
    """
    Invites a pilot to the fleet, placing them in the
    correct squad if one is mapped.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    fit_id = request.POST.get('fit_id')
    
    try:
        # 1. Get the fit and the pilot to be invited
        fit = ShipFit.objects.get(id=fit_id, waitlist=open_waitlist, status='APPROVED')
        pilot_to_invite = fit.character
        
        # 2. Get the FC's token
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)

        # 3. Find the correct role (squad)
        role = "squad_member" # Default role
        wing_id = None
        squad_id = None

        if fit.category != ShipFit.FitCategory.NONE:
            try:
                # Find a squad mapped to this fit's category
                mapped_squad = FleetSquad.objects.get(
                    wing__fleet=fleet,
                    assigned_category=fit.category
                )
                role = "squad_commander" if mapped_squad.name.lower().startswith("scout") else "squad_member"
                wing_id = mapped_squad.wing.wing_id
                squad_id = mapped_squad.squad_id
                
            except FleetSquad.DoesNotExist:
                # ---
                # --- THIS IS THE FIX ---
                # ---
                # No specific squad mapped.
                # Fallback: Try to find "On Grid" wing.
                on_grid_wing = fleet.wings.filter(name="On Grid").first()
                if on_grid_wing:
                    # Find the first squad in the "On Grid" wing
                    first_squad = on_grid_wing.squads.order_by('squad_id').first()
                    if first_squad:
                        wing_id = first_squad.wing.wing_id
                        squad_id = first_squad.squad_id
                
                # If "On Grid" not found or has no squads, use the absolute first wing/squad
                if not squad_id:
                    first_wing = fleet.wings.order_by('wing_id').first()
                    if first_wing:
                        first_squad = first_wing.squads.order_by('squad_id').first()
                        if first_squad:
                            wing_id = first_wing.wing_id
                            squad_id = first_squad.squad_id
                # ---
                # --- END THE FIX ---
                # ---

        if not wing_id or not squad_id:
            # Fallback if fleet has no wings/squads
            role = "fleet_commander" # Should never happen, but safe fallback
        
        # 4. Build the ESI invitation dict
        invitation = {
            "character_id": pilot_to_invite.character_id,
            "role": role
        }
        if wing_id:
            invitation["wing_id"] = wing_id
        if squad_id:
            invitation["squad_id"] = squad_id
        
        # 5. Send the invite
        esi = EsiClientProvider()
        esi.client.Fleets.post_fleets_fleet_id_members(
            fleet_id=fleet.esi_fleet_id,
            invitation=invitation,
            token=token.access_token
        ).results() # .results() raises exception on ESI error

        # 6. Update the fit status
        fit.status = ShipFit.FitStatus.IN_FLEET
        fit.save()

        return JsonResponse({"status": "success", "message": "Invite sent."})

    except ShipFit.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Fit not found or not approved."}, status=404)
    except Exception as e:
        # Catch ESI errors (e.g., pilot already in fleet)
        return JsonResponse({"status": "error", "message": f"ESI Error: {str(e)}"}, status=500)


# ---
# --- NEW API FOR CREATING DEFAULT LAYOUT
# ---
@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_create_default_layout(request):
    """
    Applies a hard-coded default squad layout to the FC's
    current in-game fleet.
    
    --- MODIFIED: "Merge/Update" logic ---
    This now reuses existing wings/squads and renames them,
    creates new ones if needed, and renames any leftovers
    to a generic "Squad X" format.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    try:
        # 1. Define our desired layout
        # --- MODIFIED: Layout now matches the image ---
        DEFAULT_LAYOUT = [
            {
                "name": "On Grid",
                "squads": [
                    {"name": "Logi", "category": "LOGI"},
                    {"name": "DPS", "category": "DPS"},
                    {"name": "Sniper", "category": "SNIPER"},
                    {"name": "Other", "category": "OTHER"},
                    {"name": "Mar DPS", "category": "MAR_DPS"},
                    {"name": "Mar Sniper", "category": "MAR_SNIPER"},
                    {"name": "Boxer 1", "category": None},
                    {"name": "Boxer 2", "category": None},
                    {"name": "Boxer 3", "category": None},
                    {"name": "Boxer 4", "category": None},
                ]
            },
            {
                "name": "Off Grid",
                "squads": [
                    {"name": "Scout 1", "category": None},
                    {"name": "Scout 2", "category": None},
                    {"name": "Scout 3", "category": None},
                    {"name": "Sponge 1", "category": None},
                    {"name": "Sponge 2", "category": None},
                    {"name": "Sponge 3", "category": None},
                ]
            }
        ]
        # --- END MODIFICATION ---
        
        # 2. Get FC character and token
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        fleet_id = fleet.esi_fleet_id
        
        # ---
        # --- NEW: FC Position Check ---
        # ---
        try:
            fleet_info = esi.client.Fleets.get_characters_character_id_fleet(
                character_id=fc_character.character_id,
                token=token.access_token
            ).results()
            
            if fleet_info.get('role') != 'fleet_commander':
                return JsonResponse({
                    "status": "error", 
                    "message": "You are in a squad. Please move yourself to the 'Fleet Command' position before creating the layout."
                }, status=400)
        except HTTPNotFound:
             return JsonResponse({"status": "error", "message": "You are not in the fleet. Please link the fleet first."}, status=400)
        # ---
        # --- END NEW CHECK ---
        # ---

        # 3. Get the *current* fleet structure from ESI
        current_wings = esi.client.Fleets.get_fleets_fleet_id_wings(
            fleet_id=fleet_id,
            token=token.access_token
        ).results()
        
        # 4. Clear our local DB structure
        FleetWing.objects.filter(fleet=fleet).delete()

        # 5. --- MERGE/PAVE ---
        # Loop through our desired layout and apply it
        
        wing_index = 0
        for wing_def in DEFAULT_LAYOUT:
            squad_index = 0
            wing_name = wing_def['name']
            
            # 5a. Find or create the wing
            esi_wing = current_wings[wing_index] if wing_index < len(current_wings) else None
            wing_id = None
            
            if esi_wing:
                # Reuse existing wing
                wing_id = esi_wing['id']
                # Rename it
                esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
                    fleet_id=fleet_id,
                    wing_id=wing_id,
                    naming={'name': wing_name}, # Use 'naming'
                    token=token.access_token
                ).results()
            else:
                # Create new wing
                new_wing_op = esi.client.Fleets.post_fleets_fleet_id_wings(
                    fleet_id=fleet_id,
                    token=token.access_token
                ).results()
                wing_id = new_wing_op['wing_id']
                # Rename it
                esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
                    fleet_id=fleet_id,
                    wing_id=wing_id,
                    naming={'name': wing_name}, # Use 'naming'
                    token=token.access_token
                ).results()
            
            # 5b. Save wing to our DB
            db_wing = FleetWing.objects.create(
                fleet=fleet, wing_id=wing_id, name=wing_name
            )
            
            # 5c. Get the list of squads that *actually* exist in this wing
            # (If we created the wing, this list is empty)
            # ---
            # --- THIS IS THE FIX ---
            # ---
            # Sort the squads by their ID to ensure positional renaming
            existing_squads = sorted(esi_wing['squads'], key=lambda s: s['id']) if esi_wing else []
            # ---
            # --- END THE FIX ---
            # ---

            # 5d. Loop through our *desired* squads for this wing
            for squad_def in wing_def['squads']:
                squad_name = squad_def['name']
                category = squad_def['category']
                squad_id = None
                
                # 5e. Find or create the squad
                esi_squad = existing_squads[squad_index] if squad_index < len(existing_squads) else None
                
                if esi_squad:
                    # Reuse existing squad
                    squad_id = esi_squad['id']
                    # Rename it
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet_id,
                        squad_id=squad_id,
                        naming={'name': squad_name}, # Use 'naming'
                        token=token.access_token
                    ).results()
                else:
                    # Create new squad
                    new_squad = esi.client.Fleets.post_fleets_fleet_id_wings_wing_id_squads(
                        fleet_id=fleet_id,
                        wing_id=wing_id,
                        token=token.access_token
                    ).results()
                    squad_id = new_squad['squad_id']
                    # Rename it
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet_id,
                        squad_id=squad_id,
                        naming={'name': squad_name}, # Use 'naming'
                        token=token.access_token
                    ).results()

                # 5f. Save squad to our DB
                FleetSquad.objects.create(
                    wing=db_wing,
                    squad_id=squad_id,
                    name=squad_name,
                    assigned_category=category
                )
                
                squad_index += 1
            
            # 5g. --- CLEANUP SQUADS ---
            # Rename any leftover squads in this wing
            if squad_index < len(existing_squads):
                for i in range(squad_index, len(existing_squads)):
                    esi_squad = existing_squads[i]
                    squad_id = esi_squad['id']
                    squad_name = f"Squad {i + 1}"
                    
                    # Rename it
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet_id,
                        squad_id=squad_id,
                        naming={'name': squad_name}, # Use 'naming'
                        token=token.access_token
                    ).results()

                    # Save to our DB
                    FleetSquad.objects.create(
                        wing=db_wing,
                        squad_id=squad_id,
                        name=squad_name,
                        assigned_category=None
                    )
            
            wing_index += 1
        
        # 6. --- CLEANUP WINGS ---
        # Rename any leftover wings
        if wing_index < len(current_wings):
            for i in range(wing_index, len(current_wings)):
                esi_wing = current_wings[i]
                wing_id = esi_wing['id']
                wing_name = f"Wing {i + 1}"
                
                # Rename it
                esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
                    fleet_id=fleet_id,
                    wing_id=wing_id,
                    naming={'name': wing_name}, # Use 'naming'
                    token=token.access_token
                ).results()
                
                # Save to our DB
                db_wing = FleetWing.objects.create(
                    fleet=fleet, wing_id=wing_id, name=wing_name
                )
                
                # 6a. --- CLEANUP SQUADS in leftover wings ---
                # Rename them all to "Squad X"
                squad_index = 0
                # ---
                # --- THIS IS THE FIX ---
                # ---
                # Sort the squads by their ID to ensure positional renaming
                squads_to_clean = sorted(esi_wing['squads'], key=lambda s: s['id'])
                for esi_squad in squads_to_clean:
                # ---
                # --- END THE FIX ---
                # ---
                    squad_id = esi_squad['id']
                    squad_name = f"Squad {squad_index + 1}"
                    
                    # Rename it
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet_id,
                        squad_id=squad_id,
                        naming={'name': squad_name}, # Use 'naming'
                        token=token.access_token
                    ).results()

                    # Save to our DB
                    FleetSquad.objects.create(
                        wing=db_wing,
                        squad_id=squad_id,
                        name=squad_name,
                        assigned_category=None
                    )
                    squad_index += 1


        return JsonResponse({"status": "success", "message": "Fleet layout successfully merged and mappings saved."})

    except Exception as e:
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)
# ---
# --- END NEW API
# ---


# ---
# --- NEW API FOR REFRESHING STRUCTURE
# ---
@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_refresh_structure(request):
    """
    Pulls the current fleet structure from ESI,
    updates the database, and returns the new structure.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    try:
        # 1. Get FC token and ESI client
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        # 2. Call the helper to update the DB
        _update_fleet_structure(
            esi, fc_character, token, 
            fleet.esi_fleet_id, fleet
        )
        
        # 3. Get the new structure to return
        wings = FleetWing.objects.filter(fleet=fleet).prefetch_related('squads')
        available_categories = [
            {"id": choice[0], "name": choice[1]}
            for choice in ShipFit.FitCategory.choices
            if choice[0] != 'NONE'
        ]
        structure = {
            "wings": [],
            "available_categories": available_categories
        }
        
        for wing in wings:
            wing_data = {
                "id": wing.wing_id,
                "name": wing.name,
                "squads": []
            }
            # --- THIS IS THE FIX: Added .order_by('squad_id') ---
            for squad in wing.squads.order_by('squad_id'):
                wing_data["squads"].append({
                    "id": squad.squad_id,
                    "name": squad.name,
                    "assigned_category": squad.assigned_category
                })
            structure["wings"].append(wing_data)

        return JsonResponse({"status": "success", "structure": structure})

    except Exception as e:
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)
# ---
# --- END NEW API
# ---


# ---
# --- NEW API FOR ADDING/DELETING SQUADS
# ---
@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_add_squad(request):
    """
    Adds a new squad to a wing in-game.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    wing_id = request.POST.get('wing_id')
    if not wing_id:
        return JsonResponse({"status": "error", "message": "Missing wing_id."}, status=400)
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        # 1. Make ESI call to create the squad
        new_squad = esi.client.Fleets.post_fleets_fleet_id_wings_wing_id_squads(
            fleet_id=fleet.esi_fleet_id,
            wing_id=wing_id,
            token=token.access_token
        ).results()
        
        # 2. Get the new squad ID
        squad_id = new_squad['squad_id']
        
        # 3. Rename it to "New Squad"
        esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
            fleet_id=fleet.esi_fleet_id,
            squad_id=squad_id,
            naming={'name': "New Squad"},
            token=token.access_token
        ).results()

        # 4. We don't need to update the DB, as the calling
        #    function will trigger a full refresh.
        
        return JsonResponse({"status": "success", "message": "New squad added."})

    except Exception as e:
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_delete_squad(request):
    """
    Deletes a squad from a wing in-game.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=4000)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    squad_id = request.POST.get('squad_id')
    if not squad_id:
        return JsonResponse({"status": "error", "message": "Missing squad_id."}, status=400)
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        # Make ESI call to delete the squad
        esi.client.Fleets.delete_fleets_fleet_id_squads_squad_id(
            fleet_id=fleet.esi_fleet_id,
            squad_id=squad_id,
            token=token.access_token
        ).results()
        
        return JsonResponse({"status": "success", "message": "Squad deleted."})

    except Exception as e:
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)
# ---
# --- END NEW API
# ---


# ---
# --- NEW API FOR ADDING/DELETING WINGS
# ---
@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_add_wing(request):
    """
    Adds a new wing to the fleet in-game.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        # 1. Make ESI call to create the wing
        new_wing = esi.client.Fleets.post_fleets_fleet_id_wings(
            fleet_id=fleet.esi_fleet_id,
            token=token.access_token
        ).results()
        
        # 2. Get the new wing ID
        wing_id = new_wing['wing_id']
        
        # 3. Rename it to "New Wing"
        esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
            fleet_id=fleet.esi_fleet_id,
            wing_id=wing_id,
            naming={'name': "New Wing"},
            token=token.access_token
        ).results()
        
        return JsonResponse({"status": "success", "message": "New wing added."})

    except Exception as e:
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_delete_wing(request):
    """
    Deletes a wing from the fleet in-game.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    wing_id = request.POST.get('wing_id')
    if not wing_id:
        return JsonResponse({"status": "error", "message": "Missing wing_id."}, status=400)
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        # Make ESI call to delete the wing
        esi.client.Fleets.delete_fleets_fleet_id_wings_wing_id(
            fleet_id=fleet.esi_fleet_id,
            wing_id=wing_id,
            token=token.access_token
        ).results()
        
        return JsonResponse({"status": "success", "message": "Wing deleted."})

    except Exception as e:
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)
# ---
# --- END NEW API
# ---