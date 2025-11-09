# --- All parsing logic is now centralized here ---
import re
import json
from collections import Counter
import requests

from esi.clients import EsiClientProvider
from pilot.models import EveType, EveGroup
# --- MODIFIED: Added imports for moved logic ---
from .models import ShipFit, DoctrineFit, FitSubstitutionGroup
# --- END MODIFIED ---

# --- HELPER FUNCTIONS (Copied from views.py) ---

def get_or_cache_eve_group(group_id):
    """
    Tries to get an EveGroup from the local DB.
    If not found, fetches from ESI and caches it.
    
    --- MODIFIED to use get_or_create to prevent race conditions ---
    """
    try:
        # get_or_create is atomic and prevents the race condition
        group, created = EveGroup.objects.get_or_create(
            group_id=group_id,
            defaults={'name': '...Fetching from ESI...'} # Temporary name
        )
        
        if created:
            # If we just created it, go update the name from ESI
            esi = EsiClientProvider()
            group_data = esi.client.Universe.get_universe_groups_group_id(
                group_id=group_id
            ).results()
            group.name = group_data['name']
            # --- NEW: Save the category_id ---
            group.category_id = group_data.get('category_id')
            # --- END NEW ---
            group.save()
            
        return group
    except Exception as e:
        # If ESI fails or DB fails, return None
        print(f"Error in get_or_cache_eve_group({group_id}): {e}")
        return None


def get_or_cache_eve_type(item_name):
    """
    Tries to get an EveType (ship, module, ammo) from the local DB by name.
    If not found, searches ESI, fetches details, and caches it.
    
    --- MODIFIED to use get_or_create to prevent race conditions ---
    """
    try:
        # First, try to get by name. This is fast and hits the cache.
        return EveType.objects.get(name__iexact=item_name)
    except EveType.DoesNotExist:
        try:
            # Not found by name. Go to ESI to get the ID.
            esi = EsiClientProvider()
            id_results = esi.client.Universe.post_universe_ids(
                names=[item_name] # Send a list with just our item name
            ).results()
            
            # 2. Check the results
            type_id = None
            if id_results.get('inventory_types'):
                type_id = id_results['inventory_types'][0]['id']
            elif id_results.get('categories'):
                type_id = id_results['categories'][0]['id']
            elif id_results.get('groups'):
                type_id = id_results['groups'][0]['id']
                
            if not type_id:
                return None # ESI couldn't find it
            
            # --- 3. NEW: Use get_or_create with the ID ---
            # This prevents a race condition if two items are processed
            # before the first one is saved.
            type_obj, created = EveType.objects.get_or_create(
                type_id=type_id,
                # We must provide defaults for all required fields
                defaults={
                    'name': '...Fetching from ESI...',
                    # We need a *valid* group, so we create a placeholder if we must
                    'group': get_or_cache_eve_group(0) or EveGroup.objects.get_or_create(group_id=0, defaults={'name': 'Unknown'})[0]
                }
            )

            if created:
                # If we just created it, fill in the correct details
                type_data = esi.client.Universe.get_universe_types_type_id(
                    type_id=type_id
                ).results()
                
                # Get the *actual* group
                group = get_or_cache_eve_group(type_data['group_id'])
                if not group:
                    # If group fetch fails, delete the placeholder type and fail
                    type_obj.delete()
                    return None
                    
                # 5. Get slot (if any)
                slot = None
                if 'dogma_attributes' in type_data:
                    for attr in type_data['dogma_attributes']:
                        if attr['attribute_id'] == 300: 
                            slot = int(attr['value'])
                            break
                
                # 6. Construct the icon URL
                icon_url = f"https://images.evetech.net/types/{type_id}/icon?size=32"

                # 7. Update the placeholder with the real data
                type_obj.name = type_data['name'] # Use canonical name
                type_obj.group = group
                type_obj.slot = slot
                type_obj.icon_url = icon_url
                type_obj.save()
            
            return type_obj
            
        except Exception as e:
            # ESI call or DB save failed
            print(f"Error in get_or_cache_eve_type({item_name}): {e}")
            return None
# --- END MODIFIED FUNCTIONS ---


# ---
# --- NEW HELPER FUNCTION: Get or Cache by ID ---
# ---
def get_or_cache_eve_type_by_id(type_id):
    """
    Tries to get an EveType from the local DB by its ID.
    If not found, fetches from ESI and caches it.
    This is used by the backfill script.
    """
    if not type_id:
        return None
        
    try:
        # 1. Try to get it from the DB first.
        return EveType.objects.get(type_id=type_id)
    except EveType.DoesNotExist:
        try:
            # 2. Not found, so fetch from ESI
            esi = EsiClientProvider()
            type_data = esi.client.Universe.get_universe_types_type_id(
                type_id=type_id
            ).results()
            
            # 3. Get or cache its group (which also caches the category_id)
            group = get_or_cache_eve_group(type_data['group_id'])
            if not group:
                # This should be rare, but if group fetch fails, we can't proceed
                raise Exception(f"Failed to fetch group {type_data['group_id']} for type {type_id}")
                
            # 4. Get slot (if any)
            slot = None
            if 'dogma_attributes' in type_data:
                for attr in type_data['dogma_attributes']:
                    if attr['attribute_id'] == 300: # 300 is 'implantSlot'
                        slot = int(attr['value'])
                        break
            
            # 5. Construct the icon URL
            icon_url = f"https://images.evetech.net/types/{type_id}/icon?size=32"

            # 6. Create the new EveType object
            new_type = EveType.objects.create(
                type_id=type_id,
                name=type_data['name'],
                group=group,
                slot=slot,
                icon_url=icon_url
            )
            return new_type
            
        except Exception as e:
            # ESI call or DB save failed
            print(f"Error in get_or_cache_eve_type_by_id({type_id}): {e}")
            return None
# ---
# --- END NEW HELPER FUNCTION
# ---


# --- NEW: Centralized EFT Parsing Function ---
def parse_eft_fit(raw_fit_original: str):
    """
    Parses a raw EFT fit string and returns the ship_type object,
    a list of dicts for the JSON blob, and a Counter summary.
    
    Raises ValueError on parsing failures.
    """
    # 1. Minimal sanitization
    raw_fit_no_nbsp = raw_fit_original.replace(u'\xa0', u' ')
    
    # --- THIS IS THE FIX ---
    # We no longer strip empty lines with a list comprehension.
    # We get the raw lines first.
    lines_raw = raw_fit_no_nbsp.splitlines()
    if not lines_raw:
        raise ValueError("Fit is empty.")
    
    # --- NEW: Find the first non-empty line for the header ---
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
    # --- END NEW ---

    # 2. Manually parse the header (first line)
    header_match = re.match(r'^\[([^,]+),\s*(.*?)\]$', header_line)
    if not header_match:
        raise ValueError("Could not find valid header. Fit must start with [Ship, Fit Name].")
        
    ship_name_raw = header_match.group(1).strip()
    if not ship_name_raw:
        raise ValueError("Ship name in header is empty.")

    # Strip formatting tags (e.g., <color=...>)
    tag_stripper = re.compile(r'<[^>]+>')
    ship_name = tag_stripper.sub('', ship_name_raw).strip()

    # 3. Get the Type ID for the ship (this caches it)
    ship_type = get_or_cache_eve_type(ship_name)
    
    if not ship_type:
        raise ValueError(f"Ship hull '{ship_name}' could not be found in ESI. Check spelling.")
    
    ship_type_id = ship_type.type_id
    
    # 4. Parse all items in the fit
    parsed_fit_list = [] # For storing JSON
    fit_summary_counter = Counter() # For auto-approval
    
    # Add the hull to both
    parsed_fit_list.append({
        "raw_line": header_line,
        "type_id": ship_type.type_id,
        "name": ship_type.name,
        "icon_url": ship_type.icon_url,
        "quantity": 1
    })
    fit_summary_counter[ship_type.type_id] += 1

    # --- REGEX FIX ---
    # This regex now correctly finds the quantity (e.g., " x6") at the
    # END of the line, and treats everything before it as the item name.
    # ^(.*?)       - Group 1: Non-greedily capture the item name (e.g., "Acolyte II")
    # (?: x(\d+))? - Optional non-capturing group for the quantity
    #   x         - Matches the literal " x"
    #   (\d+)     - Group 2: Captures the digits (e.g., "6")
    # $           - Anchors the match to the end of the string.
    item_regex = re.compile(r'^(.*?)(?: x(\d+))?$')
    # --- END REGEX FIX ---

    # --- MODIFIED: Loop through all lines *after* the header ---
    for line in lines_raw[first_line_index + 1:]:
        stripped_line = line.strip()

        if not stripped_line:
            # This is a blank line! Add it.
            parsed_fit_list.append({
                "raw_line": "",
                "type_id": None,
                "name": "BLANK_LINE", # Special key
                "icon_url": None,
                "quantity": 0
            })
            continue

        if stripped_line.startswith('[') and stripped_line.endswith(']'):
            # This is an empty slot, e.g., [Empty Low Slot]
            parsed_fit_list.append({
                "raw_line": stripped_line,
                "type_id": None,
                "name": stripped_line,
                "icon_url": None,
                "quantity": 0
            })
            continue

        # This is an item
        match = item_regex.match(stripped_line)
        if not match:
            # Line is not blank, not an empty slot, and not a parsable item.
            # We will add it as an "Unknown" line but not try to parse it.
            parsed_fit_list.append({
                "raw_line": stripped_line,
                "type_id": None,
                "name": f"Unknown line: {stripped_line}",
                "icon_url": None,
                "quantity": 0
            })
            continue
            
        item_name = match.group(1).strip()
        quantity = int(match.group(2)) if match.group(2) else 1
        
        if not item_name:
            continue

        # Get or cache the item
        item_type = get_or_cache_eve_type(item_name)
        
        if item_type:
            # Add to our JSON list for the modal
            parsed_fit_list.append({
                "raw_line": stripped_line, # Use the stripped line
                "type_id": item_type.type_id,
                "name": item_type.name,
                "icon_url": item_type.icon_url,
                "quantity": quantity
            })
            # Add to our summary dict for approval
            fit_summary_counter[item_type.type_id] += quantity
        else:
            # Could not find this item in ESI
            parsed_fit_list.append({
                "raw_line": stripped_line,
                "type_id": None,
                "name": f"Unknown Item: {item_name}",
                "icon_url": None,
                "quantity": quantity
            })
            # Raise an error to stop submission of invalid fits
            raise ValueError(f"Unknown item in fit: '{item_name}'. Check spelling.")

    return ship_type, parsed_fit_list, fit_summary_counter
# --- END REPLACEMENT ---


# --- NEW PARSING FUNCTION FOR ADMIN ---

def parse_eft_to_full_doctrine_data(raw_fit_original: str):
    """
    Parses a raw EFT fit string and returns the ship_type object,
    a {type_id: quantity} summary dictionary, and the full
    parsed_fit_list as a JSON string.
    Used by the DoctrineFit admin form.
    
    --- MODIFIED: This now calls the centralized parser ---
    """
    try:
        ship_type, parsed_fit_list, fit_summary_counter = parse_eft_fit(raw_fit_original)
        # Return all three components
        return ship_type, dict(fit_summary_counter), json.dumps(parsed_fit_list)
    except ValueError as e:
        # Re-raise as a generic exception for the admin form
        raise Exception(str(e))
# --- END MODIFIED FUNCTION ---


# --- MOVED FROM views.py: AUTO-APPROVAL HELPER ---
def check_fit_against_doctrines(ship_type_id, submitted_fit_summary: dict):
    """
    Compares a submitted fit summary against all matching doctrines.
    
    --- MODIFIED: Now uses FitSubstitutionGroup ---
    """
    if not ship_type_id:
        return None, 'PENDING', ShipFit.FitCategory.NONE

    # --- 1. Build the substitution map ---
    # This map will look like:
    # { 'base_item_id_str': {'base_item_id_str', 'sub_1_id_str', 'sub_2_id_str'}, ... }
    sub_groups = FitSubstitutionGroup.objects.prefetch_related('substitutes').all()
    sub_map = {}
    for group in sub_groups:
        allowed_ids = {str(sub.type_id) for sub in group.substitutes.all()}
        allowed_ids.add(str(group.base_item_id)) # The base item is always allowed
        
        # Map the base item ID to this set of allowed IDs
        sub_map[str(group.base_item_id)] = allowed_ids


    # --- 2. Get doctrines and submitted fit ---
    matching_doctrines = DoctrineFit.objects.filter(ship_type__type_id=ship_type_id)
    
    if not matching_doctrines.exists():
        return None, 'PENDING', ShipFit.FitCategory.NONE # No doctrines for this hull

    # Make a Counter of the submitted fit (with string keys)
    submitted_items_to_use = Counter({str(k): v for k, v in submitted_fit_summary.items()})

    # --- 3. Loop through each doctrine and check for a match ---
    for doctrine in matching_doctrines:
        # Get the doctrine's "shopping list"
        doctrine_items_to_match = Counter(doctrine.get_fit_items())
        
        # Make a *copy* of the submitted fit to "use up" items
        submitted_items_snapshot = submitted_items_to_use.copy()
        
        fit_matches_doctrine = True

        # --- 4. Check every item in the doctrine's shopping list ---
        for doctrine_type_id, required_quantity in doctrine_items_to_match.items():
            
            # Get the set of allowed IDs for this doctrine "slot"
            # Use sub_map.get() to provide a default (just the item itself)
            allowed_ids_for_slot = sub_map.get(doctrine_type_id, {doctrine_type_id})
            
            found_quantity = 0
            for allowed_id in allowed_ids_for_slot:
                if allowed_id in submitted_items_snapshot:
                    # Get how many of this allowed item the user has
                    qty = submitted_items_snapshot[allowed_id]
                    
                    # Add to our found quantity
                    found_quantity += qty
                    
                    # "Use up" these items so they can't match another slot
                    del submitted_items_snapshot[allowed_id]
            
            # Did we find enough items (including substitutes) for this slot?
            if found_quantity < required_quantity:
                fit_matches_doctrine = False
                break # This doctrine fails, stop checking its items

        if not fit_matches_doctrine:
            continue # This doctrine failed, try the next one

        # --- 5. Check for extra, un-used items ---
        # We matched all required items. Now, check for extras.
        # Remove the hull, which is *expected* to be in both.
        if str(ship_type_id) in submitted_items_snapshot:
            # Check if they fit *more* hulls than required
            if submitted_items_snapshot[str(ship_type_id)] > doctrine_items_to_match[str(ship_type_id)]:
                 fit_matches_doctrine = False # e.g., fit 2 Vargurs?
            del submitted_items_snapshot[str(ship_type_id)]
        
        # Check if any items are "left over"
        if len(submitted_items_snapshot) > 0:
            # User has extra modules not specified in the doctrine.
            fit_matches_doctrine = False
            continue # This doctrine fails, try the next one

        # --- 6. Perfect Match! ---
        # If we get here, fit_matches_doctrine is True AND there are no extra items.
        # This is a perfect match (with substitutions).
        return doctrine, 'APPROVED', doctrine.category

    # Looped through all doctrines, no perfect match found.
    return None, 'PENDING', ShipFit.FitCategory.NONE
# --- END MOVED HELPER ---


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