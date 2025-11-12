import re
import json
from collections import Counter
import requests
from pilot.models import EveType, EveGroup
from .models import (
    ShipFit, DoctrineFit, FitSubstitutionGroup,
    ItemComparisonRule, EveTypeDogmaAttribute
)
# Import logging
import logging
# Get a logger for this specific Python file
logger = logging.getLogger(__name__)

# New parser logic based on EFT block order
def parse_eft_fit(raw_fit_original: str):
    """
    Parses a raw EFT fit string and returns the ship_type object,
    a list of dicts for the JSON blob, and a Counter summary.
    
    --- *** MODIFIED: This now queries the local SDE (EveType table) *** ---
    --- *** MODIFIED: This now detects 'low-slot-first' or 'high-slot-first' format *** ---
    """
    # 1. Minimal sanitization
    raw_fit_no_nbsp = raw_fit_original.replace(u'\xa0', u' ')
    
    lines_raw = raw_fit_no_nbsp.splitlines()
    if not lines_raw:
        logger.warning("Fit parsing failed: Fit is empty")
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
        logger.warning("Fit parsing failed: Fit contains only whitespace")
        raise ValueError("Fit contains only whitespace.")

    # 2. Manually parse the header
    header_match = re.match(r'^\[([^,]+),\s*(.*?)\]$', header_line)
    if not header_match:
        logger.warning(f"Fit parsing failed: Invalid header: {header_line}")
        raise ValueError("Could not find valid header. Fit must start with [Ship, Fit Name].")
        
    ship_name_raw = header_match.group(1).strip()
    if not ship_name_raw:
        logger.warning(f"Fit parsing failed: Ship name in header is empty: {header_line}")
        raise ValueError("Ship name in header is empty.")

    tag_stripper = re.compile(r'<[^>]+>')
    ship_name = tag_stripper.sub('', ship_name_raw).strip()

    # 3. Get the Type ID for the ship (from our SDE)
    try:
        ship_type = EveType.objects.select_related('group').get(name__iexact=ship_name)
    except EveType.DoesNotExist:
        logger.warning(f"Fit parsing failed: Ship hull '{ship_name}' not found in SDE")
        raise ValueError(f"Ship hull '{ship_name}' could not be found in local SDE. Is SDE imported?")
    
    logger.debug(f"Parsing fit for ship: {ship_type.name} ({ship_type.type_id})")
    
    # 4. --- NEW: Detect Fit Order ---
    # We peek at the first *actual item* after the header to decide
    # which slot order to use.
    
    item_regex = re.compile(r'^(.*?)(?: x(\d+))?$')
    first_slot_type = None
    
    for line in lines_raw[first_line_index + 1:]:
        stripped_line = line.strip()
        
        if not stripped_line:
            continue # Skip blank lines
        if stripped_line.startswith('[') and stripped_line.endswith(']'):
            continue # Skip empty slots
            
        match = item_regex.match(stripped_line)
        if not match:
            continue # Skip unmatchable lines
            
        item_name = match.group(1).strip()
        if not item_name:
            continue # Skip lines that parse to an empty name

        # Found the first item, check its type
        try:
            first_item_type = EveType.objects.get(name__iexact=item_name)
            first_slot_type = first_item_type.slot_type
            logger.debug(f"First item found: '{item_name}', slot_type: '{first_slot_type}'.")
            break # We have our answer
        except EveType.DoesNotExist:
             logger.warning(f"Fit parsing failed: Unknown item '{item_name}'")
             raise ValueError(f"Unknown item in fit: '{item_name}'. Is SDE imported?")
    
    # This defines the order of fittable sections in an EFT block
    EFT_SECTION_ORDER = []
    
    if first_slot_type == 'low':
        # This is the in-game copy/paste format
        EFT_SECTION_ORDER = ['low', 'mid', 'high', 'rig', 'subsystem', 'drone']
        logger.debug("Using LOW-MID-HIGH parsing order.")
    else:
        # This is the traditional EFT format
        EFT_SECTION_ORDER = ['high', 'mid', 'low', 'rig', 'subsystem', 'drone']
        logger.debug("Using HIGH-MID-LOW parsing order.")
    # --- END NEW: Detect Fit Order ---

    # 5. Parse all items in the fit
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
    
    current_section_index = 0 # 0 = 'high' or 'low', based on detection
    
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
            
            if item_slot_type and item_slot_type in EFT_SECTION_ORDER: # Check if it's a parseable slot
                final_slot = item_slot_type
                try:
                    item_section_index = EFT_SECTION_ORDER.index(item_slot_type)
                    if item_section_index < current_section_index:
                        # This logic is now correct for both parse orders
                        # e.g., H-M-L: Found 'high' (0) when in 'mid' (1) -> cargo
                        # e.g., L-M-H: Found 'low' (0) when in 'mid' (1) -> cargo
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
            logger.warning(f"Fit parsing: Could not parse line: '{stripped_line}'")
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
             logger.warning(f"Fit parsing failed: Unknown item '{item_name}'")
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
                        # T3C logic: subsystems can appear before rigs
                        # Both lists have 'subsystem' at index 4 and 'drone' at index 5
                        # This logic remains correct.
                        final_slot = 'subsystem'
                    elif item_section_index > current_section_index:
                        # This is a new section, advance our index
                        current_section_index = item_section_index
                        final_slot = item_slot_type
                    else:
                        # Item from a previous section, must be cargo
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
            
    logger.debug(f"Fit parsed successfully for {ship_type.name}. {len(parsed_fit_list)} total lines, {len(fit_summary_counter)} unique items.")
    return ship_type, parsed_fit_list, fit_summary_counter


# PARSING FUNCTION FOR ADMIN

def parse_eft_to_full_doctrine_data(raw_fit_original: str):
    """
    Parses a raw EFT fit string and returns the ship_type object,
    a {type_id: quantity} summary dictionary, and the full
    parsed_fit_list as a JSON string.
    Used by the DoctrineFit admin form.
    """
    logger.debug("Admin: Parsing EFT fit to create/update doctrine")
    try:
        ship_type, parsed_fit_list, fit_summary_counter = parse_eft_fit(raw_fit_original)
        # Return all three components
        logger.info(f"Admin: Successfully parsed doctrine fit for {ship_type.name}")
        return ship_type, dict(fit_summary_counter), json.dumps(parsed_fit_list)
    except ValueError as e:
        # Re-raise as a generic exception for the admin form
        logger.warning(f"Admin: Failed to parse doctrine fit: {e}")
        raise Exception(str(e))

# Attribute value getter
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

# AUTO-APPROVAL HELPER
def check_fit_against_doctrines(ship_type_id, submitted_fit_summary: dict):
    """
    Compares a submitted fit summary against all matching doctrines.
    """
    if not ship_type_id:
        logger.debug("check_fit_against_doctrines: No ship_type_id provided")
        return None, 'PENDING', ShipFit.FitCategory.NONE

    # 1. Get all doctrines for this hull
    matching_doctrines = DoctrineFit.objects.filter(ship_type__type_id=ship_type_id)
    if not matching_doctrines.exists():
        logger.debug(f"check_fit_against_doctrines: No doctrines found for ship_type_id {ship_type_id}")
        return None, 'PENDING', ShipFit.FitCategory.NONE # No doctrines for this hull
        
    logger.debug(f"Checking fit against {matching_doctrines.count()} doctrines for ship {ship_type_id}")

    # ---
    # --- REMOVED: Manual substitution map (FitSubstitutionGroup)
    # ---
    logger.debug("Manual substitution groups are disabled. Using ItemComparisonRule only.")
    # ---
    # --- END REMOVAL
    # ---


    # 3. Get all EveType data for ALL items in ONE query
    all_submitted_ids = {int(k) for k in submitted_fit_summary.keys()}
    all_doctrine_item_ids = set()
    for doctrine in matching_doctrines:
        all_doctrine_item_ids.update(int(k) for k in doctrine.get_fit_items().keys())

    all_type_ids = all_submitted_ids | all_doctrine_item_ids
    type_map = {
        t.type_id: t 
        for t in EveType.objects.filter(type_id__in=all_type_ids).select_related('group', 'group__category')
    }
    logger.debug(f"Loaded {len(type_map)} unique EveTypes from DB for comparison")
    
    # Pre-cache all attributes for these types
    # We fetch all dogma attributes for all items in one query 
    # and build a dict-of-dicts for fast lookup.
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
    logger.debug(f"Pre-cached {len(dogma_attrs)} dogma attributes for comparison")

    # Get all ItemComparisonRules in one query
    # ---
    # --- NEW: Get all rules (both global and specific to this ship)
    # ---
    all_rules_qs = ItemComparisonRule.objects.filter(
        models.Q(ship_type__isnull=True) | models.Q(ship_type_id=ship_type_id)
    ).select_related('attribute')
    
    # We now build two rulebooks: one for specific, one for global
    # { group_id: [rule, ...], ... }
    specific_rules_by_group = {}
    global_rules_by_group = {}
    
    for rule in all_rules_qs:
        if rule.ship_type_id == ship_type_id:
            # This is a ship-specific rule
            if rule.group_id not in specific_rules_by_group:
                specific_rules_by_group[rule.group_id] = []
            specific_rules_by_group[rule.group_id].append(rule)
        else:
            # This is a global rule
            if rule.group_id not in global_rules_by_group:
                global_rules_by_group[rule.group_id] = []
            global_rules_by_group[rule.group_id].append(rule)
            
    logger.debug(f"Loaded {len(specific_rules_by_group)} specific rule groups and {len(global_rules_by_group)} global rule groups for ship {ship_type_id}")
    # ---
    # --- END NEW
    # ---

    # Loop through each doctrine and check for a match
    submitted_items_to_use = Counter({str(k): v for k, v in submitted_fit_summary.items()})

    for doctrine in matching_doctrines:
        logger.debug(f"--- Checking against doctrine: {doctrine.name} ---")
        doctrine_items_to_match = Counter(doctrine.get_fit_items())
        submitted_items_snapshot = submitted_items_to_use.copy()
        fit_matches_doctrine = True

        # 5. Check every item in the doctrine's shopping list
        for doctrine_type_id_str, required_quantity in doctrine_items_to_match.items():
            
            doctrine_type_id = int(doctrine_type_id_str)
            doctrine_item_type = type_map.get(doctrine_type_id)

            if not doctrine_item_type or not doctrine_item_type.group:
                logger.warning(f"Doctrine {doctrine.name} item {doctrine_type_id_str} not in type_map. Skipping check.")
                fit_matches_doctrine = False
                break 

            # 5a. Build the list of all allowed items for this "slot"
            allowed_ids_for_slot = {doctrine_type_id}

            # ---
            # --- REMOVED: Manual Substitutions check
            # ---

            # MODIFICATION: Use new database-driven check
            # 2. Get Automatic "Equal or Better" Substitutions
            
            # ---
            # --- NEW: Rule override logic
            # ---
            # Check for ship-specific rules first
            comparison_rules = specific_rules_by_group.get(doctrine_item_type.group_id)
            
            if comparison_rules is None:
                # No specific rules found, fall back to global rules
                comparison_rules = global_rules_by_group.get(doctrine_item_type.group_id, [])
                if comparison_rules:
                     logger.debug(f"Using {len(comparison_rules)} GLOBAL rules for group {doctrine_item_type.group.name}")
            else:
                 logger.debug(f"Using {len(comparison_rules)} SPECIFIC rules for group {doctrine_item_type.group.name} on ship {ship_type_id}")
            # ---
            # --- END NEW
            # ---

            if comparison_rules: # Only run this logic if rules exist for this group
                # logger.debug(f"Found {len(comparison_rules)} auto-sub rules for group {doctrine_item_type.group.name}")
                for submitted_id_str, qty in submitted_items_snapshot.items():
                    submitted_item_id = int(submitted_id_str)
                    
                    if submitted_item_id in allowed_ids_for_slot:
                        continue 

                    submitted_item_type = type_map.get(submitted_item_id)
                    
                    if not submitted_item_type or not submitted_item_type.group:
                        continue
                    
                    # Run the "Equal or Better" check
                    if (submitted_item_type.group_id == doctrine_item_type.group_id and
                        submitted_item_type.group.category_id == doctrine_item_type.group.category_id):
                        
                        is_equal_or_better = True
                        for rule in comparison_rules:
                            # Use the new helper that reads from the cache
                            doctrine_val = _get_attribute_value_from_item(doctrine_item_type, rule.attribute.attribute_id)
                            submitted_val = _get_attribute_value_from_item(submitted_item_type, rule.attribute.attribute_id)
                            
                            if rule.higher_is_better:
                                if submitted_val < doctrine_val:
                                    logger.debug(f"Auto-sub failed for {submitted_item_type.name}: {rule.attribute.name} is {submitted_val} (need >= {doctrine_val})")
                                    is_equal_or_better = False
                                    break 
                            else:
                                if submitted_val > doctrine_val:
                                    logger.debug(f"Auto-sub failed for {submitted_item_type.name}: {rule.attribute.name} is {submitted_val} (need <= {doctrine_val})")
                                    is_equal_or_better = False
                                    break 
                        
                        if is_equal_or_better:
                            logger.debug(f"Auto-sub success: {submitted_item_type.name} accepted for {doctrine_item_type.name}")
                            allowed_ids_for_slot.add(submitted_item_id)

            # 5b. Consume items from the snapshot
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
            
            # 5c. Check if we found enough
            if found_quantity < required_quantity:
                logger.debug(f"Fit failed doctrine {doctrine.name}: Missing item. Need {required_quantity} of {doctrine_item_type.name} (or sub), found {found_quantity}")
                fit_matches_doctrine = False
                break 

        if not fit_matches_doctrine:
            continue 

        # 6. Check for extra, un-used items
        ship_type_id_str = str(ship_type_id)
        if ship_type_id_str in submitted_items_snapshot:
            if submitted_items_snapshot[ship_type_id_str] > doctrine_items_to_match.get(ship_type_id_str, 0):
                 logger.debug(f"Fit failed doctrine {doctrine.name}: Extra ship hull item found")
                 fit_matches_doctrine = False
            del submitted_items_snapshot[ship_type_id_str]
        
        if len(submitted_items_snapshot) > 0:
            logger.debug(f"Fit failed doctrine {doctrine.name}: Extra items found: {submitted_items_snapshot}")
            fit_matches_doctrine = False
            continue 

        # 7. Perfect Match!
        logger.info(f"Fit PERFECTLY matched doctrine {doctrine.name}. Approving with category {doctrine.category}")
        return doctrine, 'APPROVED', doctrine.category

    # Looped through all doctrines, no perfect match found.
    logger.info(f"Fit for ship {ship_type_id} did not match any doctrines. Setting to PENDING.")
    return None, 'PENDING', ShipFit.FitCategory.NONE