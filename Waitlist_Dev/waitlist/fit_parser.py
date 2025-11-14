import re
import json
from collections import Counter
import requests
from pilot.models import EveType, EveGroup
from .models import (
    ShipFit
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