# --- NEW FILE ---
import time
from django.core.management.base import BaseCommand
from django.db import transaction

from pilot.models import EveType
from esi.clients import EsiClientProvider

# ---
# --- IMPORTANT: These helpers are copied from fit_parser.py
# --- to make this script self-contained and avoid import issues.
# ---
def _get_dogma_value(dogma_attributes, attribute_id):
    """Safely find a dogma attribute value from the list."""
    if not dogma_attributes:
        return None

def _get_dogma_effects(dogma_effects_list):
    """Safely extracts effect IDs from the dogma_effects list."""
    if not dogma_effects_list:
        return set()
    return {effect.get('effect_id') for effect in dogma_effects_list}
# ---
# --- END HELPERS
# ---

class Command(BaseCommand):
    help = 'Scans the EveType table for items missing slot data and backfills them from ESI.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("--- Starting EveType Slot Backfill ---"))
        
        # Find all types that are missing *both* ship slot data and module slot data.
        # This prevents us from re-processing items we've already checked.
        types_to_update = EveType.objects.filter(
            hi_slots__isnull=True, 
            slot_type__isnull=True
        ).select_related('group')
        
        total_types = types_to_update.count()
        if total_types == 0:
            self.stdout.write(self.style.SUCCESS("All EveTypes are already up-to-date. No backfill needed."))
            return

        self.stdout.write(self.style.WARNING(f"Found {total_types} types missing slot data. Fetching from ESI..."))
        self.stdout.write("This may take a while.")

        esi = EsiClientProvider()
        updated_count = 0
        failed_count = 0
        
        # Process in batches to be nice to the database
        for eve_type in types_to_update:
            try:
                type_data = esi.client.Universe.get_universe_types_type_id(
                    type_id=eve_type.type_id
                ).results()
                
                dogma_attrs = type_data.get('dogma_attributes', [])
                dogma_effects_list = type_data.get('dogma_effects', []) # --- ADDED ---
                group = eve_type.group # We already have this from select_related
                
                # ---
                # --- THIS IS THE FIX ---
                # ---
                # Check if the group's category_id is missing and fetch it
                if group.category_id is None:
                    try:
                        self.stdout.write(f"    - Group '{group.name}' is stale. Fetching category_id...")
                        group_data = esi.client.Universe.get_universe_groups_group_id(
                            group_id=group.group_id
                        ).results()
                        group.category_id = group_data.get('category_id')
                        group.name = group_data.get('name', group.name) # Update name too
                        group.save()
                        self.stdout.write(f"    - Updated stale group: {group.name} (Category: {group.category_id})")
                    except Exception as e:
                        self.stdout.write(self.style.WARNING(f"    - Could not update group {group.group_id}: {e}"))
                        # Continue with category_id as None
                # ---
                # --- END THE FIX ---
                # ---

                # 1. Get ship slot counts (if applicable)
                eve_type.hi_slots = _get_dogma_value(dogma_attrs, 14)
                eve_type.med_slots = _get_dogma_value(dogma_attrs, 13)
                eve_type.low_slots = _get_dogma_value(dogma_attrs, 12)
                eve_type.rig_slots = _get_dogma_value(dogma_attrs, 1137)
                eve_type.subsystem_slots = _get_dogma_value(dogma_attrs, 1367)

                # 2. Get module slot type (if applicable)
                # ---
                # --- THIS IS THE FIX ---
                # ---
                slot_type = None
                effect_ids = _get_dogma_effects(dogma_effects_list) # Get the set of effect IDs

                if group.category_id == 18: # Category 18 is Drone
                    slot_type = 'drone'
                elif 12 in effect_ids: # effectID 12 = hiPower
                    slot_type = 'high'
                elif 13 in effect_ids: # effectID 13 = medPower
                    slot_type = 'mid'
                elif 11 in effect_ids: # effectID 11 = loPower
                    slot_type = 'low'
                elif 2663 in effect_ids: # effectID 2663 = rigSlot
                    slot_type = 'rig'
                elif 3772 in effect_ids: # effectID 3772 = subSystem
                    slot_type = 'subsystem'
                # ---
                # --- END THE FIX ---
                # ---
                
                eve_type.slot_type = slot_type
                
                # 3. Save the updated type
                eve_type.save()
                
                updated_count += 1
                self.stdout.write(self.style.SUCCESS(f"  Updated: {eve_type.name} (Slot: {slot_type or 'N/A'})"))
                
                # Be nice to ESI
                time.sleep(0.05) 

            except Exception as e:
                failed_count += 1
                self.stdout.write(self.style.ERROR(f"  Failed: {eve_type.name} (ID: {eve_type.type_id}). Error: {e}"))

        self.stdout.write(self.style.SUCCESS(f"\n--- Backfill Complete ---"))
        self.stdout.write(self.style.SUCCESS(f"Successfully updated: {updated_count}"))
        self.stdout.write(self.style.ERROR(f"Failed to update:   {failed_count}"))