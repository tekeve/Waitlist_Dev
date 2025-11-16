import logging
import json
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse, Http404
from django.utils import timezone
from django_eventstream import send_event
# --- MODIFICATION: Import DoctrineFit and Fleet ---
from .models import EveCharacter, ShipFit, FleetWaitlist, DoctrineFit, Fleet
from pilot.models import EveType
from .fit_parser import parse_eft_fit
from .helpers import is_fleet_commander

# Get a logger for this specific Python file
logger = logging.getLogger(__name__)


@login_required
def home(request):
    """
    Handles the main homepage (/).
    - If user is authenticated, shows the waitlist_view.
    - If not, shows the simple login page (homepage.html).
    """
    
    logger.debug(f"User {request.user.username} accessing home view")
    
    if not request.user.is_authenticated:
        # --- THIS IS THE PUBLIC LOGIC ---
        # User is not logged in, show the public homepage.
        # This template contains the "Log In" button.
        # No redirect happens.
        logger.debug("User is not authenticated, showing public homepage.html")
        return render(request, 'homepage.html')
        # --- END PUBLIC LOGIC ---

    logger.debug("User is authenticated, preparing waitlist_view.html")
    
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    
    all_fits = []
    if open_waitlist:
        logger.debug(f"Open waitlist found: {open_waitlist.fleet.description}")
        all_fits = ShipFit.objects.filter(
            waitlist=open_waitlist,
            status__in=['PENDING', 'APPROVED']
        ).select_related('character').order_by('submitted_at')
    else:
        logger.debug("No open waitlist found.")
        # --- NOTE: open_waitlist is None, waitlist_view.html ---
        # --- will handle this and show "waitlist closed" ---

    xup_fits = all_fits.filter(status='PENDING') if open_waitlist else []
    dps_fits = all_fits.filter(status='APPROVED', category__in=['DPS', 'MAR_DPS']) if open_waitlist else []
    logi_fits = all_fits.filter(status='APPROVED', category='LOGI') if open_waitlist else []
    sniper_fits = all_fits.filter(status='APPROVED', category__in=['SNIPER', 'MAR_SNIPER']) if open_waitlist else []
    other_fits = all_fits.filter(status='APPROVED', category='OTHER') if open_waitlist else []
    
    is_fc = is_fleet_commander(request.user)
    
    all_user_chars = request.user.eve_characters.all().order_by('character_name')
    main_char = all_user_chars.filter(is_main=True).first()
    if not main_char:
        main_char = all_user_chars.first()
    
    context = {
        'xup_fits': xup_fits,
        'dps_fits': dps_fits,
        'logi_fits': logi_fits,
        'sniper_fits': sniper_fits,
        'other_fits': other_fits,
        'is_fc': is_fc,
        'open_waitlist': open_waitlist,
        'user_characters': all_user_chars,
        'all_chars_for_header': all_user_chars,
        'main_char_for_header': main_char,
    }
    return render(request, 'waitlist_view.html', context)

# ---
# --- NEW: PUBLIC DOCTRINE VIEW
# ---
def doctrine_view(request):
    """
    Displays the public doctrine fittings page.
    This view does NOT require login.
    --- MODIFIED: Now groups by Fleet Type, then Category ---
    """
    logger.debug("Serving public doctrine_view")
    
    # 1. Get all categories
    category_choices = {
        cat[0]: cat[1] for cat in ShipFit.FitCategory.choices 
        if cat[0] != 'NONE'
    }
    
    # 2. Get all DoctrineFit-enabled fleets
    # We find all distinct Fleet objects that are referenced by at least one DoctrineFit
    fleet_ids_with_fits = DoctrineFit.objects.filter(
        fleet_type__isnull=False,
        parsed_fit_json__isnull=False,
        ship_type__isnull=False
    ).values_list('fleet_type_id', flat=True).distinct()
    
    fleets = Fleet.objects.filter(id__in=fleet_ids_with_fits).order_by('description')

    # 3. Get all relevant doctrine fits, ordered correctly
    # --- MODIFICATION: Added ship_type__name to the sorting ---
    all_fits = DoctrineFit.objects.filter(
        fleet_type_id__in=fleet_ids_with_fits,
        parsed_fit_json__isnull=False,
        ship_type__isnull=False
    ).select_related('ship_type').order_by(
        'category', 
        'ship_type__name', # <-- NEW: Group by ship name
        '-hull_tier',  # Sorts 3_OPTIMAL -> 1_ENTRY (Optimal to Entry)
        'fit_tier',    # Sorts 1_STARTER -> 3_OPTIMAL (Starter to Optimal)
        'name'
    )
    # --- END MODIFICATION ---
    
    # 4. Build the nested structure
    grouped_fleets = []
    
    for fleet in fleets:
        fleet_data = {
            'fleet_name': fleet.description,
            'categories': []
        }
        
        fits_for_this_fleet = all_fits.filter(fleet_type=fleet)
        
        # Get all unique category IDs for *this fleet*
        category_ids_in_fleet = fits_for_this_fleet.values_list('category', flat=True).distinct()
        
        # We iterate over the *master* category list to maintain order
        for cat_id, cat_name in category_choices.items():
            if cat_id in category_ids_in_fleet:
                # This fleet has fits for this category
                fits_in_category = fits_for_this_fleet.filter(category=cat_id)
                
                # --- MODIFICATION: Group fits by hull type ---
                category_data = {
                    'name': cat_name,
                    'hull_groups': [] # This will be a list of hull groups
                }

                # Use a dictionary to group fits by hull
                hulls_dict = {}
                for fit in fits_in_category:
                    ship_type_id = fit.ship_type.type_id
                    if ship_type_id not in hulls_dict:
                        # Create a new entry for this hull
                        hulls_dict[ship_type_id] = {
                            'ship_name': fit.ship_type.name,
                            'ship_type_id': fit.ship_type.type_id,
                            'fits': []
                        }
                    # Add the fit to this hull's list
                    hulls_dict[ship_type_id]['fits'].append(fit)

                # Convert the dictionary to a list for the template
                category_data['hull_groups'] = list(hulls_dict.values())
                
                fleet_data['categories'].append(category_data)
                # --- END MODIFICATION ---
        
        if fleet_data['categories']:
            grouped_fleets.append(fleet_data)
            
    # 5. Get header context (for logged-in users)
    # This logic is needed so the header displays correctly
    # even on a public page if the user *is* logged in.
    is_fc = False
    all_user_chars = None
    main_char = None
    
    if request.user.is_authenticated:
        is_fc = is_fleet_commander(request.user)
        all_user_chars = request.user.eve_characters.all().order_by('character_name')
        main_char = all_user_chars.filter(is_main=True).first()
        if not main_char:
            main_char = all_user_chars.first()

    context = {
        # --- MODIFICATION: Use new grouped_fleets structure ---
        'grouped_fleets': grouped_fleets,
        # --- END MODIFICATION ---
        'is_fc': is_fc,
        'user_characters': all_user_chars,
        'all_chars_for_header': all_user_chars,
        'main_char_for_header': main_char,
    }
    
    # We rename the old 'fittings_view.html' to 'doctrine_view.html'
    return render(request, 'doctrine_view.html', context)
# ---
# --- END NEW VIEW
# ---

# ---
# --- NEW: PUBLIC API FOR DOCTRINE FITS
# ---
def api_get_doctrine_fit_details(request):
    """
    Returns the parsed fit JSON for a public doctrine fit.
    This view does NOT require login.
    """
    fit_id = request.GET.get('fit_id')
    logger.debug(f"Public request for doctrine fit details: {fit_id}")
    if not fit_id:
        return HttpResponseBadRequest("Missing fit_id")
        
    try:
        fit = get_object_or_404(DoctrineFit, id=fit_id)
    except Http404:
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)

    # This is the same logic used in api_views.py to build the slotted fit
    try:
        ship_eve_type = fit.ship_type
        if not ship_eve_type:
             return JsonResponse({"status": "error", "message": "Fit is missing ship type."}, status=404)
            
        slot_counts = {
            'high': int(ship_eve_type.hi_slots or 0),
            'mid': int(ship_eve_type.med_slots or 0),
            'low': int(ship_eve_type.low_slots or 0),
            'rig': int(ship_eve_type.rig_slots or 0),
            'subsystem': int(ship_eve_type.subsystem_slots or 0)
        }
        is_t3c = slot_counts['subsystem'] > 0

        try:
            full_fit_list = json.loads(fit.parsed_fit_json) if fit.parsed_fit_json else []
        except json.JSONDecodeError:
            full_fit_list = []
        
        item_ids = [item['type_id'] for item in full_fit_list if item.get('type_id')]
        item_types_map = {
            t.type_id: t 
            for t in EveType.objects.filter(type_id__in=set(item_ids)).select_related('group', 'group__category')
        }

        item_bins = {
            'high': [], 'mid': [], 'low': [], 'rig': [], 
            'subsystem': [], 'drone': [], 'cargo': [],
            'ship': [], 'BLANK_LINE': []
        }
        
        for item in full_fit_list:
            final_slot = item.get('final_slot')
            if not final_slot or final_slot == 'ship':
                continue

            type_id = item.get('type_id')
            item_type = item_types_map.get(type_id) if type_id else None
            
            item_obj = {
                "type_id": type_id,
                "name": item.get('name', 'Unknown'),
                "icon_url": item.get('icon_url'),
                "quantity": item.get('quantity', 1),
                "raw_line": item.get('raw_line', item.get('name', 'Unknown')),
                "is_empty": (final_slot in ['high','mid','low','rig','subsystem'] and not type_id),
            }
            
            if item_type:
                item_obj['name'] = item_type.name

            if final_slot == 'BLANK_LINE':
                continue
            
            if item_obj['is_empty']:
                item_bins[final_slot].append(item_obj)
                continue

            if final_slot in item_bins:
                item_bins[final_slot].append(item_obj)
            else:
                item_bins['cargo'].append(item_obj)
            
        final_slots = {}
    
        if is_t3c:
            final_slots = {
                'high': item_bins['high'],
                'mid': item_bins['mid'],
                'low': item_bins['low'],
                'rig': item_bins['rig'],
                'subsystem': item_bins['subsystem'],
            }
            slot_counts['high'] = len(item_bins['high'])
            slot_counts['mid'] = len(item_bins['mid'])
            slot_counts['low'] = len(item_bins['low'])
            slot_counts['rig'] = len(item_bins['rig'])
        else:
            for slot_key in ['high', 'mid', 'low', 'rig', 'subsystem']:
                total_slots = slot_counts[slot_key]
                slot_list = []
                
                fitted_items = item_bins[slot_key]
                for item in fitted_items:
                    slot_list.append(item)

                empty_slot_name = f"[Empty {slot_key.capitalize()} Slot]"
                while len(slot_list) < total_slots:
                    slot_list.append({
                        "name": empty_slot_name, 
                        "is_empty": True,
                        "raw_line": empty_slot_name,
                        "type_id": None,
                        "status": "empty"
                    })
                
                final_slots[slot_key] = slot_list

        final_slots['drone'] = item_bins['drone']
        final_slots['cargo'] = item_bins['cargo']
            
        logger.info(f"Successfully served doctrine fit details for fit {fit.id} ({fit.name})")
        return JsonResponse({
            "status": "success",
            "name": fit.name,
            "raw_fit": fit.raw_fit_eft, # Send the EFT fit for copying
            "slotted_fit": {
                "ship": {
                    "type_id": ship_eve_type.type_id,
                    "name": ship_eve_type.name,
                    "icon_url": f"https://images.evetech.net/types/{ship_eve_type.type_id}/render?size=128"
                },
                "slots": final_slots,
                "slot_counts": slot_counts,
                "is_t3c": is_t3c
            },
        })

    except Exception as e:
        logger.error(f"Error in api_get_doctrine_fit_details for fit_id {fit_id}: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"An unexpected error occurred: {str(e)}"}, status=500)
# ---
# --- END NEW API
# ---


@login_required
@require_POST
def api_submit_fit(request):
    """
    Handles the fit submission from the X-Up modal.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    logger.debug(f"User {request.user.username} attempting fit submission")

    if not open_waitlist:
        logger.warning(f"Fit submission failed for {request.user.username}: Waitlist is closed")
        return JsonResponse({"status": "error", "message": "The waitlist is currently closed."}, status=400)

    character_id = request.POST.get('character_id')
    raw_fit_original = request.POST.get('raw_fit') 
    
    try:
        character = EveCharacter.objects.get(
            character_id=character_id, 
            user=request.user
        )
    except EveCharacter.DoesNotExist:
        logger.warning(f"Fit submission failed: User {request.user.username} submitted for char {character_id} which they don't own")
        return JsonResponse({"status": "error", "message": "Invalid character selected."}, status=403)
    
    if not raw_fit_original:
        logger.warning(f"Fit submission failed for {character.character_name}: Fit was empty")
        return JsonResponse({"status": "error", "message": "Fit cannot be empty."}, status=400)
    
    try:
        logger.debug(f"Parsing fit for {character.character_name}")
        ship_type, parsed_fit_list, fit_summary_counter = parse_eft_fit(raw_fit_original)
        ship_type_id = ship_type.type_id
        logger.debug(f"Fit parsed successfully: {ship_type.name}")

        new_status = 'PENDING'
        new_category = ShipFit.FitCategory.NONE
        logger.info(f"Fit for {character.character_name} submitted. Status set to PENDING.")

        fit, created = ShipFit.objects.update_or_create(
            character=character,
            waitlist=open_waitlist,
            status__in=['PENDING', 'APPROVED'],
            defaults={
                'raw_fit': raw_fit_original,
                'parsed_fit_json': json.dumps(parsed_fit_list),
                'status': new_status,
                'waitlist': open_waitlist,
                'ship_name': ship_type.name,
                'ship_type_id': ship_type_id,
                'tank_type': None,
                'fit_issues': None,
                'category': new_category,
                'submitted_at': timezone.now(),
                'last_updated': timezone.now(),
            }
        )
        
        logger.debug("Sending 'waitlist-updates' event")
        send_event('waitlist-updates', 'update', {
            'fit_id': fit.id,
            'action': 'submit'
        })
        
        if created:
            logger.info(f"New fit {fit.id} created for {character.character_name}")
            return JsonResponse({"status": "success", "message": f"Fit for {character.character_name} submitted!"})
        else:
            logger.info(f"Fit {fit.id} updated for {character.character_name}")
            return JsonResponse({"status": "success", "message": f"Fit for {character.character_name} updated."})

    except ValueError as e:
        logger.warning(f"Fit parsing failed for {character.character_name}: {e}")
        return JsonResponse({"status": "error", "message": str(e)}, status=400)
    except Exception as e:
        logger.error(f"Unexpected error in api_submit_fit for {character.character_name}: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"An unexpected error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
def api_update_fit_status(request):
    """
    Handles FC actions (approve/deny) from the waitlist view.
    """
    if not is_fleet_commander(request.user):
        logger.warning(f"Non-FC user {request.user.username} tried to update fit status")
        return JsonResponse({"status": "error", "message": "Not authorized"}, status=403)

    fit_id = request.POST.get('fit_id')
    action = request.POST.get('action')
    logger.info(f"FC {request.user.username} performing action '{action}' on fit {fit_id}")

    try:
        fit = ShipFit.objects.get(id=fit_id)
    except ShipFit.DoesNotExist:
        logger.warning(f"FC {request.user.username} tried to {action} non-existent fit {fit_id}")
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)

    if action == 'approve':
        fit.status = 'APPROVED'
        
        if fit.category == ShipFit.FitCategory.NONE:
            fit.category = ShipFit.FitCategory.OTHER
            logger.debug(f"Fit {fit.id} approved, category set to OTHER")
        
        fit.save()
        
        logger.debug("Sending 'waitlist-updates' event")
        send_event('waitlist-updates', 'update', {
            'fit_id': fit.id,
            'action': 'approve'
        })
        
        logger.info(f"Fit {fit.id} ({fit.character.character_name}) approved by {request.user.username}")
        return JsonResponse({"status": "success", "message": "Fit approved"})
        
    elif action == 'deny':
        fit.status = 'DENIED'
        fit.denial_reason = "Denied by FC from waitlist."
        fit.save()
        
        logger.debug("Sending 'waitlist-updates' event")
        send_event('waitlist-updates', 'update', {
            'fit_id': fit.id,
            'action': 'deny'
        })
        
        logger.info(f"Fit {fit.id} ({fit.character.character_name}) denied by {request.user.username}")
        return JsonResponse({"status": "success", "message": "Fit denied"})

    logger.warning(f"FC {request.user.username} sent invalid action '{action}' for fit {fit_id}")
    return JsonResponse({"status": "error", "message": "Invalid action"}, status=400)


@login_required
def api_get_waitlist_html(request):
    """
    Returns just the HTML for the waitlist columns.
    Used by the live polling JavaScript.
    """
    logger.debug(f"Polling request received from {request.user.username}")
    
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    
    if not open_waitlist:
        logger.debug("Polling request: Waitlist is closed")
        return HttpResponseBadRequest("Waitlist closed")

    all_fits = ShipFit.objects.filter(
        waitlist=open_waitlist,
        status__in=['PENDING', 'APPROVED']
    ).select_related('character').order_by('submitted_at')

    xup_fits = all_fits.filter(status='PENDING')
    dps_fits = all_fits.filter(status='APPROVED', category__in=['DPS', 'MAR_DPS'])
    logi_fits = all_fits.filter(status='APPROVED', category='LOGI')
    sniper_fits = all_fits.filter(status='APPROVED', category__in=['SNIPER', 'MAR_SNIPER'])
    other_fits = all_fits.filter(status='APPROVED', category='OTHER')
    
    is_fc = is_fleet_commander(request.user)

    context = {
        'xup_fits': xup_fits,
        'dps_fits': dps_fits,
        'logi_fits': logi_fits,
        'sniper_fits': sniper_fits,
        'other_fits': other_fits,
        'is_fc': is_fc,
    }
    
    logger.debug(f"Polling response: XUP:{xup_fits.count()}, LOGI:{logi_fits.count()}, DPS:{dps_fits.count()}, SNIPER:{sniper_fits.count()}, OTHER:{other_fits.count()}")
    return render(request, '_waitlist_columns.html', context)