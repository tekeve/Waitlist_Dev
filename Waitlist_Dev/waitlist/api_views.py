import logging
import json
from collections import Counter
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest, Http404
from django.db import models
from .models import (
    ShipFit
)
from pilot.models import EveType
from .helpers import is_fleet_commander

logger = logging.getLogger(__name__)

@login_required
def api_get_fit_details(request):
    """
    Returns the parsed fit JSON for the FC's inspection modal.
    --- MODIFIED ---
    Now also allows the pilot who submitted the fit to view it.
    REMOVED all comparison logic.
    """
    fit_id = request.GET.get('fit_id')
    logger.debug(f"User {request.user.username} requesting fit details for fit_id {fit_id}")
    if not fit_id:
        logger.warning("api_get_fit_details called without fit_id")
        return HttpResponseBadRequest("Missing fit_id")
        
    try:
        fit = get_object_or_404(ShipFit, id=fit_id)
    except Http404:
        logger.warning(f"User {request.user.username} requested fit {fit_id}, but it was not found")
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)

    is_fc = is_fleet_commander(request.user)
    is_owner = (fit.character.user == request.user)

    if not is_fc and not is_owner:
        logger.warning(f"User {request.user.username} tried to get fit details for fit {fit_id} which they do not own")
        return JsonResponse({"status": "error", "message": "Not authorized"}, status=403)
        
    logger.debug(f"User {request.user.username} authorized for fit {fit_id} (FC: {is_fc}, Owner: {is_owner})")
        
    try:
        ship_eve_type = EveType.objects.filter(type_id=fit.ship_type_id).first()
        if not ship_eve_type:
            logger.error(f"Could not find EveType for ship_type_id {fit.ship_type_id} (Fit {fit.id})")
            return JsonResponse({"status": "error", "message": "Ship hull not found in SDE cache."}, status=404)
            
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
            logger.warning(f"Corrupted parsed_fit_json for Fit {fit.id}")
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
            
        logger.info(f"Successfully served fit details for fit {fit.id} ({fit.character.character_name})")
        return JsonResponse({
            "status": "success",
            "name": f"{fit.character.character_name}'s {fit.ship_name}",
            "raw_fit": fit.raw_fit,
            "character_id": fit.character.character_id,
            "character_user_id": fit.character.user.id,
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
        import traceback
        traceback.print_exc()
        logger.error(f"Error in api_get_fit_details for fit_id {fit_id}: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)