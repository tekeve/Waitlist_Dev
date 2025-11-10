# --- All parsing logic is now centralized here ---
import re
import json
from collections import Counter
import requests

# --- REMOVED: ESIClientProvider is no longer needed here ---
from pilot.models import EveType, EveGroup
# --- MODIFIED: Added imports for moved logic ---
from .models import (
    ShipFit, DoctrineFit, FitSubstitutionGroup,
    # --- *** NEW: Import new rule/data models *** ---
    ItemComparisonRule, EveTypeDogmaAttribute
)
# --- END MODIFIED ---

# ---
# --- *** REMOVED ALL ESI-CACHING FUNCTIONS *** ---
# - get_or_cache_eve_group
# - _get_dogma_value
# - _get_dogma_effects
# - get_or_cache_eve_type
# - get_or_cache_eve_type_by_id
# They are all replaced by the SDE importer.
# ---
# --- *** END REMOVAL ***
# ---


# ---
# --- THIS IS THE FIX: New parser logic based on EFT block order
# ---
def parse_eft_fit(raw_fit_original: str):
    """
    Parses a raw EFT fit string and returns the ship_type object,
    a list of dicts for the JSON blob, and a Counter summary.
    
    --- *** MODIFIED: This now queries the local SDE (EveType table) *** ---
    """
    # 1. Minimal sanitization
    raw_fit_no_nbsp = raw_fit_original.replace(u'\xa0', u' ')
    
    lines_raw = raw_fit_no_nbsp.splitlines()
    if not lines_raw:
        raise ValueError("Fit is empty.")
    
    first_line_index = -1
    header_line = ""
    for i, line in enumerate(lines_raw):
        stripped_line = line.strip()
        if stripped_line: # Find the first non-empty line
            first_line_index = i
            header_line = stripped_line
            break
            
    if first_line_index == -1:
        raise ValueError("Fit contains only whitespace.")

    # 2. Manually parse the header
    header_match = re.match(r'^\[([^,]+),\s*(.*?)\]$', header_line)
    if not header_match:
        raise ValueError("Could not find valid header. Fit must start with [Ship, Fit Name].")
        
    ship_name_raw = header_match.group(1).strip()
    if not ship_name_raw:
        raise ValueError("Ship name in header is empty.")

    tag_stripper = re.compile(r'<[^>]+>')
    ship_name = tag_stripper.sub('', ship_name_raw).strip()

    # 3. Get the Type ID for the ship (from our SDE)
    try:
        ship_type = EveType.objects.select_related('group').get(name__iexact=ship_name)
    except EveType.DoesNotExist:
        raise ValueError(f"Ship hull '{ship_name}' could not be found in local SDE. Is SDE imported?")
    
    # 4. Parse all items in the fit
    parsed_fit_list = [] # For storing JSON
    fit_summary_counter = Counter() # For auto-approval
    
    # Add the hull
    parsed_fit_list.append({
        "raw_line": header_line,
        "type_id": ship_type.type_id,
        "name": ship_type.name,
        "icon_url": f"https://images.evetech.net/types/{ship_type.type_id}/icon?size=32", # Re-generate URL
        "quantity": 1,
        "final_slot": "ship" # Special slot for the hull
    })
    fit_summary_counter[ship_type.type_id] += 1

    item_regex = re.compile(r'^(.*?)(?: x(\d+))?$')
    
    # This defines the order of fittable sections in an EFT block
    EFT_SECTION_ORDER = ['high', 'mid', 'low', 'rig', 'subsystem', 'drone']
    
    current_section_index = 0 # 0 = 'high', 1 = 'mid', ..., 5 = 'drone'
    
    # T3Cs are special
    is_t3c = (ship_type.subsystem_slots or 0) > 0

    for line in lines_raw[first_line_index + 1:]:
        stripped_line = line.strip()
        final_slot = 'cargo' # Default to cargo
        item_type = None
        quantity = 0
        
        if not stripped_line:
            final_slot = 'BLANK_LINE'
            if current_section_index < len(EFT_SECTION_ORDER):
                current_section_index += 1
            
            parsed_fit_list.append({
                "raw_line": "", "type_id": None, "name": "BLANK_LINE",
                "icon_url": None, "quantity": 0, "final_slot": final_slot
            })
            continue

        if stripped_line.startswith('[') and stripped_line.endswith(']'):
            # This is an empty slot, e.g., [Empty Low Slot]
            slot_name = stripped_line.lower()
            if 'high' in slot_name: item_slot_type = 'high'
            elif 'med' in slot_name: item_slot_type = 'mid'
            elif 'low' in slot_name: item_slot_type = 'low'
            elif 'rig' in slot_name: item_slot_type = 'rig'
            elif 'subsystem' in slot_name: item_slot_type = 'subsystem'
            else: item_slot_type = None 
            
            if item_slot_type:
                final_slot = item_slot_type
                try:
                    item_section_index = EFT_SECTION_ORDER.index(item_slot_type)
                    if item_section_index < current_section_index:
                        final_slot = 'cargo'
                    else:
                        current_section_index = item_section_index
                except ValueError:
                    final_slot = 'cargo'
            else:
                final_slot = 'cargo'
            
            parsed_fit_list.append({
                "raw_line": stripped_line, "type_id": None, "name": stripped_line,
                "icon_url": None, "quantity": 0, "final_slot": final_slot
            })
            continue

        # This is an item
        match = item_regex.match(stripped_line)
        if not match:
            parsed_fit_list.append({
                "raw_line": stripped_line, "type_id": None, "name": f"Unknown line: {stripped_line}",
                "icon_url": None, "quantity": 0, "final_slot": 'cargo'
            })
            continue
            
        item_name = match.group(1).strip()
        quantity = int(match.group(2)) if match.group(2) else 1
        
        if not item_name:
            continue

        # Get item from our SDE
        try:
            item_type = EveType.objects.get(name__iexact=item_name)
        except EveType.DoesNotExist:
             raise ValueError(f"Unknown item in fit: '{item_name}'. Is SDE imported?")
        
        if item_type:
            item_slot_type = item_type.slot_type # e.g., 'high', 'mid', 'drone', None
            
            if item_slot_type is None:
                final_slot = 'cargo'
            elif item_slot_type in EFT_SECTION_ORDER:
                try:
                    item_section_index = EFT_SECTION_ORDER.index(item_slot_type)
                    
                    if item_section_index == current_section_index:
                        final_slot = item_slot_type
                    elif is_t3c and item_slot_type == 'subsystem' and current_section_index < 5:
                        final_slot = 'subsystem'
                    elif item_section_index > current_section_index:
                        current_section_index = item_section_index
                        final_slot = item_slot_type
                    else:
                        final_slot = 'cargo'
                except ValueError:
                    final_slot = 'cargo'
            else:
                final_slot = 'cargo'

            parsed_fit_list.append({
                "raw_line": stripped_line,
                "type_id": item_type.type_id,
                "name": item_type.name,
                "icon_url": f"https://images.evetech.net/types/{item_type.type_id}/icon?size=32",
                "quantity": quantity,
                "final_slot": final_slot
            })
            fit_summary_counter[item_type.type_id] += quantity
        else:
            # This case is now handled by the try/except block
            pass

    return ship_type, parsed_fit_list, fit_summary_counter


# --- NEW PARSING FUNCTION FOR ADMIN ---

def parse_eft_to_full_doctrine_data(raw_fit_original: str):
    """
    Parses a raw EFT fit string and returns the ship_type object,
    a {type_id: quantity} summary dictionary, and the full
    parsed_fit_list as a JSON string.
    Used by the DoctrineFit admin form.
    
    --- MODIFIED: This now calls the centralized SDE parser ---
    """
    try:
        ship_type, parsed_fit_list, fit_summary_counter = parse_eft_fit(raw_fit_original)
        # Return all three components
        return ship_type, dict(fit_summary_counter), json.dumps(parsed_fit_list)
    except ValueError as e:
        # Re-raise as a generic exception for the admin form
        raise Exception(str(e))
# --- END MODIFIED FUNCTION ---


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


# --- MOVED FROM views.py: AUTO-APPROVAL HELPER ---
def check_fit_against_doctrines(ship_type_id, submitted_fit_summary: dict):
    """
    Compares a submitted fit summary against all matching doctrines.
    
    --- *** MODIFIED: Now uses SDE-backed ItemComparisonRule logic *** ---
    """
    if not ship_type_id:
        return None, 'PENDING', ShipFit.FitCategory.NONE

    # --- 1. Get all doctrines for this hull ---
    matching_doctrines = DoctrineFit.objects.filter(ship_type__type_id=ship_type_id)
    if not matching_doctrines.exists():
        return None, 'PENDING', ShipFit.FitCategory.NONE # No doctrines for this hull

    # --- 2. Build the manual substitution map ---
    # { 'base_item_id_str': {set of allowed_ids_int} }
    sub_groups = FitSubstitutionGroup.objects.prefetch_related('substitutes').all()
    sub_map = {}
    for group in sub_groups:
        base_id_str = str(group.base_item_id)
        allowed_ids = {sub.type_id for sub in group.substitutes.all()}
        allowed_ids.add(group.base_item_id) # The base item is always allowed
        sub_map[base_id_str] = allowed_ids


    # --- 3. Get all EveType data for ALL items in ONE query ---
    all_submitted_ids = {int(k) for k in submitted_fit_summary.keys()}
    all_doctrine_item_ids = set()
    for doctrine in matching_doctrines:
        all_doctrine_item_ids.update(int(k) for k in doctrine.get_fit_items().keys())

    all_type_ids = all_submitted_ids | all_doctrine_item_ids
    type_map = {
        t.type_id: t 
        for t in EveType.objects.filter(type_id__in=all_type_ids).select_related('group', 'group__category')
    }
    
    # --- *** NEW: Pre-cache all attributes for these types *** ---
    # This is a massive optimization. We fetch all dogma attributes for all
    # items in one query and build a dict-of-dicts for fast lookup.
    # { type_id: { attr_id: value, ... }, ... }
    attribute_values_by_type = {}
    dogma_attrs = EveTypeDogmaAttribute.objects.filter(type_id__in=all_type_ids).values('type_id', 'attribute_id', 'value')
    
    for attr in dogma_attrs:
        type_id = attr['type_id']
        if type_id not in attribute_values_by_type:
            attribute_values_by_type[type_id] = {}
        attribute_values_by_type[type_id][attr['attribute_id']] = attr['value'] or 0

    # Now, attach this cache to each EveType object
    for type_id, item_type in type_map.items():
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

    # --- 4. Loop through each doctrine and check for a match ---
    submitted_items_to_use = Counter({str(k): v for k, v in submitted_fit_summary.items()})

    for doctrine in matching_doctrines:
        doctrine_items_to_match = Counter(doctrine.get_fit_items())
        submitted_items_snapshot = submitted_items_to_use.copy()
        fit_matches_doctrine = True

        # --- 5. Check every item in the doctrine's shopping list ---
        for doctrine_type_id_str, required_quantity in doctrine_items_to_match.items():
            
            doctrine_type_id = int(doctrine_type_id_str)
            doctrine_item_type = type_map.get(doctrine_type_id)

            if not doctrine_item_type or not doctrine_item_type.group:
                fit_matches_doctrine = False
                break 

            # --- 5a. Build the list of all allowed items for this "slot" ---
            allowed_ids_for_slot = {doctrine_type_id}

            # 1. Get Manual Substitutions
            if doctrine_type_id_str in sub_map:
                allowed_ids_for_slot.update(sub_map[doctrine_type_id_str])

            # --- *** MODIFICATION: Use new database-driven check *** ---
            # 2. Get Automatic "Equal or Better" Substitutions
            
            comparison_rules = rules_by_group.get(doctrine_item_type.group_id, [])

            for submitted_id_str, qty in submitted_items_snapshot.items():
                submitted_item_id = int(submitted_id_str)
                
                if submitted_item_id in allowed_ids_for_slot:
                    continue 

                submitted_item_type = type_map.get(submitted_item_id)
                
                if not submitted_item_type or not submitted_item_type.group:
                    continue
                
                # --- Run the "Equal or Better" check ---
                if (submitted_item_type.group_id == doctrine_item_type.group_id and
                    submitted_item_type.group.category_id == doctrine_item_type.group.category_id):
                    
                    if not comparison_rules:
                        continue 
                        
                    is_equal_or_better = True
                    for rule in comparison_rules:
                        # --- *** Use the new helper that reads from the cache *** ---
                        doctrine_val = _get_attribute_value_from_item(doctrine_item_type, rule.attribute.attribute_id)
                        submitted_val = _get_attribute_value_from_item(submitted_item_type, rule.attribute.attribute_id)
                        
                        if rule.higher_is_better:
                            if submitted_val < doctrine_val:
                                is_equal_or_better = False
                                break 
                        else:
                            if submitted_val > doctrine_val:
                                is_equal_or_better = False
                                break 
                    
                    if is_equal_or_better:
                        allowed_ids_for_slot.add(submitted_item_id)
            # --- *** END MODIFICATION *** ---

            # --- 5b. Consume items from the snapshot ---
            found_quantity = 0
            for allowed_id in allowed_ids_for_slot:
                allowed_id_str = str(allowed_id)
                
                if allowed_id_str in submitted_items_snapshot:
                    available_qty = submitted_items_snapshot[allowed_id_str]
                    needed_qty = required_quantity - found_quantity
                    qty_to_use = min(available_qty, needed_qty)
                    
                    found_quantity += qty_to_use
                    submitted_items_snapshot[allowed_id_str] -= qty_to_use
                    
                    if submitted_items_snapshot[allowed_id_str] == 0:
                        del submitted_items_snapshot[allowed_id_str]
                
                if found_quantity == required_quantity:
                    break 
            
            # --- 5c. Check if we found enough ---
            if found_quantity < required_quantity:
                fit_matches_doctrine = False
                break 

        if not fit_matches_doctrine:
            continue 

        # --- 6. Check for extra, un-used items ---
        ship_type_id_str = str(ship_type_id)
        if ship_type_id_str in submitted_items_snapshot:
            if submitted_items_snapshot[ship_type_id_str] > doctrine_items_to_match.get(ship_type_id_str, 0):
                 fit_matches_doctrine = False
            del submitted_items_snapshot[ship_type_id_str]
        
        if len(submitted_items_snapshot) > 0:
            fit_matches_doctrine = False
            continue 

        # --- 7. Perfect Match! ---
        return doctrine, 'APPROVED', doctrine.category

    # Looped through all doctrines, no perfect match found.
    return None, 'PENDING', ShipFit.FitCategory.NONE
# --- END MODIFIED HELPER ---


# --- This is the original function, left as a placeholder ---
def parse_and_validate_fit(ship_fit: ShipFit):
    """
    Parses a ship fit and validates it against doctrine rules.
    
    This function is NOT called by the api_submit_fit view,
    which only does basic header parsing.
    
    This function could be called by an FC action (e.g., "Auto-Approve")
    or by a background task.
    """
    
    raw_text = ship_fit.raw_fit
    waitlist = ship_fit.waitlist
    character = ship_fit.character
    
    # For now, this is just a placeholder.
    # In the future, you could add logic here to:
    # 1. Parse all modules from raw_text (using regex or simple line splitting)
    # 2. Compare against FitCheckRule models associated with the waitlist
    # 3. Check character skills via ESI
    
    print(f"Placeholder: Validating fit {ship_fit.id} for {character.character_name}...")
    
    # Example placeholder logic
    if "Shield Booster" not in raw_text:
        ship_fit.fit_issues = "Missing Shield Booster"
        ship_fit.save()
        return False, "Missing Shield Booster"

    ship_fit.fit_issues = None
    ship_fit.save()
    return True, "Fit passes basic checks."