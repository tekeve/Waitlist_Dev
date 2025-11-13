import logging
import json
from collections import Counter
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest, Http404
# --- THIS IS THE FIX ---
from django.db import models
# --- END THE FIX ---

from .models import (
    ShipFit, DoctrineFit, FitSubstitutionGroup,
    ItemComparisonRule, EveTypeDogmaAttribute
)
from pilot.models import EveType
from .helpers import is_fleet_commander # Import from helper

logger = logging.getLogger(__name__)


# ---
# --- HELPER FUNCTION (Moved from views.py)
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
        # Trust the parser's 'final_slot' designation
        final_slot = item.get('final_slot')
        if not final_slot or final_slot not in item_bins:
            final_slot = 'cargo' # Fallback
            
        type_id = item.get('type_id')
        item_type = item_types_map.get(type_id) if type_id else None
        
        # Build the item object
        item_obj = {
            "type_id": type_id,
            "name": item.get('name', 'Unknown'),
            "icon_url": item.get('icon_url'), # Get icon_url from the parsed JSON
            "quantity": item.get('quantity', 1),
            "raw_line": item.get('raw_line', item.get('name', 'Unknown')),
            # An item is "empty" if it's a fittable slot and has no type_id
            "is_empty": (final_slot in ['high','mid','low','rig','subsystem'] and not type_id)
        }

        if item_type:
            # Overwrite with canonical data from DB
            item_obj['name'] = item_type.name

        if final_slot == 'BLANK_LINE':
            # We don't add blank lines to the final display
            continue
        
        # Add to the correct bin
        item_bins[final_slot].append(item_obj)


    # 5. Create the final slotted structure
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
                    "type_id": None
                })
            
            final_slots[slot_key] = slot_list

    # Drones and cargo are just lists
    final_slots['drone'] = item_bins['drone']
    final_slots['cargo'] = item_bins['cargo']

    return {
        "ship": {
            "type_id": ship_eve_type.type_id,
            "name": ship_eve_type.name,
            "icon_url": f"https://images.evetech.net/types/{ship_eve_type.type_id}/render?size=128"
        },
        "slots": final_slots,
        "slot_counts": slot_counts,
        "is_t3c": is_t3c
    }


# ---
# --- HELPER FUNCTION (Moved from views.py)
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
        logger.warning(f"_get_attribute_value_from_item fallback for {item_type.name} (attr {attribute_id})")
        try:
            attr_obj = EveTypeDogmaAttribute.objects.get(type=item_type, attribute_id=attribute_id)
            return attr_obj.value or 0
        except EveTypeDogmaAttribute.DoesNotExist:
            return 0
                
    # Return the value from the pre-populated cache
    return item_type._attribute_cache.get(attribute_id, 0)


# ---
# --- API VIEWS (Moved from views.py)
# ---

@login_required
def api_get_doctrine_fit_details(request):
    """
    Returns the details for a specific doctrine fit.
    This is for the public fittings page modal.
    """
    fit_id = request.GET.get('fit_id')
    logger.debug(f"User {request.user.username} requesting doctrine fit details for fit_id {fit_id}")
    if not fit_id:
        logger.warning("api_get_doctrine_fit_details called without fit_id")
        return HttpResponseBadRequest("Missing fit_id")
        
    try:
        doctrine = get_object_or_404(DoctrineFit, id=fit_id)
        
        # 1. Get the ship's EveType
        ship_eve_type = doctrine.ship_type
        if not ship_eve_type:
            logger.error(f"DoctrineFit {doctrine.id} ({doctrine.name}) is missing ship_type")
            raise Http404("Doctrine fit is missing a ship type.")
            
        # 2. Get the parsed list of items
        parsed_list = doctrine.get_parsed_fit_list()
        if not parsed_list:
            logger.warning(f"DoctrineFit {doctrine.id} missing parsed_fit_json, re-parsing from raw EFT")
            # Fallback: re-parse from raw EFT
            if doctrine.raw_fit_eft:
                # This import is local to avoid circular dependency
                from .fit_parser import parse_eft_fit
                _, parsed_list, _ = parse_eft_fit(doctrine.raw_fit_eft)
            else:
                logger.error(f"DoctrineFit {doctrine.id} has no raw_fit_eft to parse")
                parsed_list = [] # No data

        # 3. Build the slotted context
        slotted_context = _build_slotted_fit_context(ship_eve_type, parsed_list)

        # 4. Return the new structure + the raw EFT for copying
        logger.info(f"Successfully served doctrine fit details for {doctrine.name}")
        return JsonResponse({
            "status": "success",
            "name": doctrine.name,
            "raw_eft": doctrine.raw_fit_eft,
            "slotted_fit": slotted_context
        })
        
    except Http404:
        logger.warning(f"DoctrineFit not found for fit_id {fit_id}")
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)
    except Exception as e:
        logger.error(f"Error in api_get_doctrine_fit_details for fit_id {fit_id}: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
def api_get_fit_details(request):
    """
    Returns the parsed fit JSON for the FC's inspection modal.
    """
    if not is_fleet_commander(request.user):
        logger.warning(f"Non-FC user {request.user.username} tried to get fit details")
        return JsonResponse({"status": "error", "message": "Not authorized"}, status=403)
        
    fit_id = request.GET.get('fit_id')
    logger.debug(f"FC {request.user.username} requesting fit details for fit_id {fit_id}")
    if not fit_id:
        logger.warning("api_get_fit_details called without fit_id")
        return HttpResponseBadRequest("Missing fit_id")
        
    try:
        fit = get_object_or_404(ShipFit, id=fit_id)
        
        # 1. Get the ship's EveType and base slot counts
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

        # 2. Get the pilot's submitted fit list
        try:
            full_fit_list = json.loads(fit.parsed_fit_json) if fit.parsed_fit_json else []
        except json.JSONDecodeError:
            logger.warning(f"Corrupted parsed_fit_json for Fit {fit.id}")
            full_fit_list = [] # Handle corrupted JSON
            
        # 3. Get the best matching doctrine
        doctrine = DoctrineFit.objects.filter(ship_type__type_id=fit.ship_type_id).first()
        
        # 4. --- START NEW COMPARISON LOGIC ---
        
        # 4a. Get all EveTypes for items in the fit
        item_ids = [item['type_id'] for item in full_fit_list if item.get('type_id')]
        if doctrine:
            item_ids.extend(int(k) for k in doctrine.get_fit_items().keys())
        item_types_map = {
            t.type_id: t 
            for t in EveType.objects.filter(type_id__in=set(item_ids)).select_related('group', 'group__category')
        }

        # 4b. Get doctrine items
        # --- REMOVED: Manual substitution maps (sub_map, reverse_sub_map) ---
        doctrine_items_to_fill = Counter()
        doctrine_name = "No Doctrine Found"
        if doctrine:
            doctrine_name = doctrine.name
            doctrine_items_to_fill = Counter(doctrine.get_fit_items())
            logger.debug(f"Comparing fit {fit.id} against doctrine '{doctrine_name}'")
            
        # --- REMOVED: FitSubstitutionGroup logic ---

        # Pre-cache all attributes for these types
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
        logger.debug(f"Pre-cached {len(dogma_attrs)} dogma attributes for {len(item_types_map)} types")

        # Get all ItemComparisonRules in one query
        # ---
        # --- THIS IS THE FIX: Use the same prioritized rule logic as fit_parser.py ---
        # ---
        all_rules_qs = ItemComparisonRule.objects.filter(
            models.Q(ship_type__isnull=True) | models.Q(ship_type_id=fit.ship_type_id)
        ).select_related('attribute')
        
        specific_rules_by_group = {}
        global_rules_by_group = {}
        
        for rule in all_rules_qs:
            if rule.ship_type_id == fit.ship_type_id:
                # This is a ship-specific rule
                if rule.group_id not in specific_rules_by_group:
                    specific_rules_by_group[rule.group_id] = []
                specific_rules_by_group[rule.group_id].append(rule)
            else:
                # This is a global rule
                if rule.group_id not in global_rules_by_group:
                    global_rules_by_group[rule.group_id] = []
                global_rules_by_group[rule.group_id].append(rule)
                
        logger.debug(f"Loaded {len(specific_rules_by_group)} specific rule groups and {len(global_rules_by_group)} global rule groups")
        # ---
        # --- END THE FIX ---
        # ---

        # 4c. Create bins to sort items into
        item_bins = {
            'high': [], 'mid': [], 'low': [], 'rig': [], 
            'subsystem': [], 'drone': [], 'cargo': [],
            'ship': [], 'BLANK_LINE': []
        }
        
        # 4d. Create a copy of doctrine items to "consume"
        doctrine_items_to_fill_copy = doctrine_items_to_fill.copy()
        
        # 4e. Loop through fit items
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
                "icon_url": item.get('icon_url'),
                "quantity": item.get('quantity', 1),
                "raw_line": item.get('raw_line', item.get('name', 'Unknown')),
                "is_empty": (final_slot in ['high','mid','low','rig','subsystem'] and not type_id),
                "status": "doctrine", # Default
                "potential_matches": [],
                "substitutes_for": [],
                "failure_reasons": [],
            }
            
            if item_type:
                item_obj['name'] = item_type.name

            if final_slot == 'BLANK_LINE':
                continue
            
            if item_obj['is_empty']:
                item_obj['status'] = 'empty'
                item_bins[final_slot].append(item_obj)
                continue # No comparison needed

            # --- Start Comparison Logic ---
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
                    
                    # --- REMOVED: Manual Substitute Match (reverse_sub_map) ---
                    
                    elif (item_type and item_type.group_id):
                        # Automatic "Equal or Better" Check
                        found_match = False
                        # ---
                        # --- THIS IS THE FIX (Part 1):
                        # --- Loop over the *original* doctrine list, not the copy.
                        for doctrine_id_str, doctrine_qty in doctrine_items_to_fill.items():
                        # --- END THE FIX ---
                            
                            # --- MODIFIED: Remove check on missing_qty ---
                            # if missing_qty <= 0:
                            #     continue
                            # --- END MODIFIED ---
                            
                            doctrine_item_type = item_types_map.get(int(doctrine_id_str))
                            if not doctrine_item_type or not doctrine_item_type.group:
                                continue

                            # ---
                            # --- MODIFICATION: Revert to GROUP check ---
                            # ---
                            # Check if they are in the same GROUP (e.g. both are 'Shield Hardener')
                            if (doctrine_item_type.group_id == item_type.group_id):
                            
                                # ---
                                # --- THIS IS THE FIX: Use prioritized rule lookup
                                # ---
                                comparison_rules = specific_rules_by_group.get(doctrine_item_type.group_id)
            
                                if comparison_rules is None:
                                    # No specific rules found, fall back to global rules
                                    comparison_rules = global_rules_by_group.get(doctrine_item_type.group_id, [])
                                # ---
                                # --- END THE FIX ---
                                # ---
                                
                                if not comparison_rules:
                                    # ---
                                    # --- NEW: If no rules, this is a failed match but not a sub
                                    # ---
                                    item_obj['status'] = 'problem'
                                    found_match = True
                                    break # Exit inner loop
                                
                                # ---
                                # --- THIS IS THE FIX ---
                                # ---
                                # Initialize to True *only if* there are rules to run.
                                # If rules are empty, it stays False, and fails.
                                is_equal_or_better = bool(comparison_rules)
                                # ---
                                # --- END THE FIX ---
                                # ---
                                failure_reasons = [] 
                                for rule in comparison_rules:
                                    attr_id = rule.attribute.attribute_id
                                    doctrine_val = _get_attribute_value_from_item(doctrine_item_type, attr_id)
                                    submitted_val = _get_attribute_value_from_item(item_type, attr_id)
                                    
                                    if rule.higher_is_better:
                                        if submitted_val < doctrine_val:
                                            is_equal_or_better = False
                                            failure_reasons.append({
                                                "attribute_name": rule.attribute.name,
                                                "doctrine_value": doctrine_val,
                                                "submitted_value": submitted_val
                                            })
                                            # --- BUG FIX: Remove break ---
                                            # --- END BUG FIX ---
                                    else: # Lower is better
                                        if submitted_val > doctrine_val:
                                            is_equal_or_better = False
                                            failure_reasons.append({
                                                "attribute_name": rule.attribute.name,
                                                "doctrine_value": doctrine_val,
                                                "submitted_value": submitted_val
                                            })
                                            # --- BUG FIX: Remove break ---
                                            # --- END BUG FIX ---
                                
                                if is_equal_or_better:
                                    # ---
                                    # --- THIS IS THE FIX (Part 2):
                                    # --- Check if the slot is still available in our "copy" list
                                    if doctrine_items_to_fill_copy.get(doctrine_id_str, 0) > 0:
                                        # Slot is available, consume it
                                        item_obj['status'] = 'accepted_sub'
                                        item_obj['substitutes_for'] = [{
                                            "name": doctrine_item_type.name,
                                            "type_id": doctrine_item_type.type_id,
                                            "icon_url": f"https://images.evetech.net/types/{doctrine_item_type.type_id}/icon?size=32",
                                            "quantity": doctrine_items_to_fill.get(str(doctrine_item_type.type_id), 0)
                                        }]
                                        doctrine_items_to_fill_copy[doctrine_id_str] -= qty_in_fit
                                    else:
                                        # This is a valid sub, but the slot is already filled.
                                        # Mark as a problem (extra item).
                                        item_obj['status'] = 'problem'
                                    # ---
                                    # --- END THE FIX ---
                                    # ---
                                    found_match = True
                                    break # This break is CORRECT (we found a valid sub)
                                else:
                                    item_obj['status'] = 'problem'
                                    item_obj['failure_reasons'] = failure_reasons
                                    item_obj['potential_matches'] = [{
                                        "name": doctrine_item_type.name,
                                        "type_id": doctrine_item_type.type_id,
                                        "icon_url": f"https://images.evetech.net/types/{doctrine_item_type.type_id}/icon?size=32",
                                        "quantity": doctrine_items_to_fill_copy.get(str(doctrine_item_type.type_id), 0)
                                    }]
                                    found_match = True
                                    # ---
                                    # --- THIS IS THE FIX: ---
                                    # --- We must break here, because we have found
                                    # --- the item this is supposed to replace, and it FAILED.
                                    # --- We don't want to compare it to anything else.
                                    break
                                    # ---
                                    # --- END THE FIX ---
                                    # ---
                        
                        if not found_match:
                             item_obj['status'] = 'problem' # No match found

                    else:
                        item_obj['status'] = 'problem'

                if (item_obj['status'] == 'problem' and 
                    not item_obj['potential_matches'] and 
                    not item_obj['failure_reasons'] and 
                    item_type and item_type.group_id):
                    
                    # ---
                    # --- THIS IS THE FIX ---
                    # ---
                    # This item is a problem, but we didn't find a direct
                    # doctrine item it was trying (and failing) to replace.
                    # This can happen if it's an "extra" item and the doctrine
                    # slots for its group are already filled.
                    #
                    # We now check the *original* doctrine list (`doctrine_items_to_fill`)
                    # instead of the *remaining* list (`doctrine_items_to_fill_copy`)
                    # to find potential matches.
                    
                    # Find all doctrine items in the same group
                    all_doctrine_ids_in_group = {
                        int(m_id_str) for m_id_str in doctrine_items_to_fill.keys() # Use original list
                    }
                    if all_doctrine_ids_in_group:
                        # Find which of those are in the same group as our problem item
                        missing_in_group = EveType.objects.filter(
                            group_id=item_type.group_id,
                            type_id__in=all_doctrine_ids_in_group # Check against all doctrine items
                        )
                        for m_type in missing_in_group:
                            item_obj['potential_matches'].append({
                                "name": m_type.name, 
                                "type_id": m_type.type_id, 
                                "icon_url": f"https://images.evetech.net/types/{m_type.type_id}/icon?size=32",
                                # Get quantity from original doctrine list
                                "quantity": doctrine_items_to_fill.get(str(m_type.type_id), 0) 
                            })
                    # ---
                    # --- END THE FIX ---
                    # ---
            # --- End Comparison Logic ---

            # Add to bin
            if final_slot in item_bins:
                item_bins[final_slot].append(item_obj)
            else:
                item_bins['cargo'].append(item_obj) # Fallback
            
        # 5. Create the final slotted structure
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
            
        # 6. Find any remaining "Missing" items
        final_missing_ids = {
            int(m_id_str) for m_id_str, qty in doctrine_items_to_fill_copy.items() 
            if qty > 0 and int(m_id_str) != fit.ship_type_id
        }
        missing_types = EveType.objects.filter(type_id__in=final_missing_ids)
        missing_items = [{
            "type_id": t.type_id, 
            "name": t.name, 
            "icon_url": f"https://images.evetech.net/types/{t.type_id}/icon?size=32",
            "quantity": doctrine_items_to_fill_copy[str(t.type_id)]
        } for t in missing_types]


        # 7. Return the full structure
        logger.info(f"Successfully served fit details for fit {fit.id} ({fit.character.character_name})")
        return JsonResponse({
            "status": "success",
            "name": f"{fit.character.character_name} vs. {doctrine_name}",
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
            "missing_items": missing_items, # For the 'Make Sub' dropdown
            "doctrine_name": doctrine_name
        })

    except Http404:
        logger.warning(f"FC {request.user.username} requested fit {fit_id}, but it was not found")
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error(f"Error in api_get_fit_details for fit_id {fit_id}: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)