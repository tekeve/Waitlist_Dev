import pandas as pd
import requests
import io
import time
from django.core.management.base import BaseCommand
from django.db import transaction, connection
from pilot.models import EveCategory, EveGroup, EveType
from waitlist.models import EveDogmaAttribute, EveTypeDogmaAttribute

# SDE File URLs
SDE_BASE_URL = "https://www.fuzzwork.co.uk/dump/latest/"
CATEGORIES_URL = f"{SDE_BASE_URL}invCategories.csv"
GROUPS_URL = f"{SDE_BASE_URL}invGroups.csv"
TYPES_URL = f"{SDE_BASE_URL}invTypes.csv"
DOGMA_ATTRIBUTES_URL = f"{SDE_BASE_URL}dgmAttributeTypes.csv"
DOGMA_TYPE_ATTRIBUTES_URL = f"{SDE_BASE_URL}dgmTypeAttributes.csv"
DOGMA_EFFECTS_URL = f"{SDE_BASE_URL}dgmTypeEffects.csv"

# Dogma Attribute IDs for populating EveType fields
DOGMA_ATTR_IDS = {
    'hi_slots': 14,
    'med_slots': 13,
    'low_slots': 12,
    'rig_slots': 1137,
    'subsystem_slots': 1367,
    'meta_level': 633,
}

# Dogma Effect IDs for determining module slot type
DOGMA_EFFECT_IDS = {
    'high': 12,
    'mid': 13,
    'low': 11,
    'rig': 2663,
    'subsystem': 3772,
}

class Command(BaseCommand):
    help = 'Downloads and imports the latest SDE from Fuzzwork.'

    def handle(self, *args, **options):
        start_time = time.time()
        
        with transaction.atomic():
            self.stdout.write(self.style.SUCCESS("--- Starting SDE Import ---"))
            
            # 1. Eve Categories
            self.import_categories()
            
            # 2. Eve Groups
            self.import_groups()
            
            # 3. Eve Types
            self.import_types()
            
            # 4. Dogma Attributes
            self.import_dogma_attributes()
            
            # 5. Dogma Type Attributes (The big one)
            self.import_dogma_type_attributes()
            
            # 6. Dogma Type Effects (for module slots)
            self.import_dogma_type_effects()

            # 7. Post-processing: Populate EveType helper fields
            self.populate_evetype_helpers()

        end_time = time.time()
        self.stdout.write(self.style.SUCCESS(f"\n--- SDE Import Complete in {end_time - start_time:.2f} seconds ---"))

    def _download_csv(self, url, columns):
        self.stdout.write(f"Downloading {url.split('/')[-1]}...")
        try:
            r = requests.get(url)
            r.raise_for_status()
            # --- *** THIS IS THE FIX *** ---
            # Remove 'keep_default_na=False'
            # Let pandas read empty fields as NaN by default
            return pd.read_csv(io.StringIO(r.text), usecols=columns)
            # --- *** END THE FIX *** ---
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to download or parse {url}: {e}"))
            raise e # Stop the transaction

    def import_categories(self):
        df = self._download_csv(CATEGORIES_URL, ['categoryID', 'categoryName', 'iconID', 'published'])
        self.stdout.write("Importing Eve Categories...")
        
        EveCategory.objects.all().delete() # Clear old data
        
        categories = [
            EveCategory(
                category_id=row['categoryID'],
                name=row['categoryName'],
                icon_id=row['iconID'] if pd.notna(row['iconID']) else None,
                published=row['published']
            )
            for _, row in df.iterrows()
        ]
        EveCategory.objects.bulk_create(categories, batch_size=1000)
        self.stdout.write(f"Imported {len(categories)} categories.")

    def import_groups(self):
        df = self._download_csv(GROUPS_URL, ['groupID', 'groupName', 'categoryID', 'iconID', 'published'])
        self.stdout.write("Importing Eve Groups...")
        
        EveGroup.objects.all().delete() # Clear old data
        
        # Get all categories for foreign key mapping
        categories = {c.category_id: c for c in EveCategory.objects.all()}
        
        groups = [
            EveGroup(
                group_id=row['groupID'],
                name=row['groupName'],
                category=categories.get(row['categoryID']), # Link FK
                icon_id=row['iconID'] if pd.notna(row['iconID']) else None,
                published=row['published']
            )
            for _, row in df.iterrows()
        ]
        EveGroup.objects.bulk_create(groups, batch_size=1000)
        self.stdout.write(f"Imported {len(groups)} groups.")

    def import_types(self):
        df = self._download_csv(
            TYPES_URL, 
            ['typeID', 'groupID', 'typeName', 'description', 'mass', 'volume', 'capacity', 'iconID', 'published']
        )
        self.stdout.write("Importing Eve Types (this may take a moment)...")
        
        EveType.objects.all().delete() # Clear old data
        
        # Get all groups for foreign key mapping
        groups = {g.group_id: g for g in EveGroup.objects.all()}
        
        types_to_create = []
        for _, row in df.iterrows():
            # Skip rows with no group
            if not pd.notna(row['groupID']):
                continue
                
            types_to_create.append(
                EveType(
                    type_id=row['typeID'],
                    group=groups.get(row['groupID']), # Link FK
                    name=row['typeName'],
                    description=row['description'] if pd.notna(row['description']) else None,
                    mass=row['mass'] if pd.notna(row['mass']) else None,
                    volume=row['volume'] if pd.notna(row['volume']) else None,
                    capacity=row['capacity'] if pd.notna(row['capacity']) else None,
                    icon_id=row['iconID'] if pd.notna(row['iconID']) else None,
                    published=row['published']
                )
            )

        EveType.objects.bulk_create(types_to_create, batch_size=1000)
        self.stdout.write(f"Imported {len(types_to_create)} types.")

    def import_dogma_attributes(self):
        df = self._download_csv(
            DOGMA_ATTRIBUTES_URL,
            ['attributeID', 'attributeName', 'description', 'iconID', 'unitID', 'displayName']
        )
        self.stdout.write("Importing Dogma Attributes...")
        
        EveDogmaAttribute.objects.all().delete() # Clear old data
        
        attributes = [
            EveDogmaAttribute(
                attribute_id=row['attributeID'],
                name=row['displayName'] if pd.notna(row['displayName']) and row['displayName'] else row['attributeName'],
                description=row['description'] if pd.notna(row['description']) else None,
                icon_id=row['iconID'] if pd.notna(row['iconID']) else None,
                unit_name=str(row['unitID']) if pd.notna(row['unitID']) else None # Store unitID for now
            )
            for _, row in df.iterrows()
        ]
        EveDogmaAttribute.objects.bulk_create(attributes, batch_size=1000)
        self.stdout.write(f"Imported {len(attributes)} dogma attributes.")

    def import_dogma_type_attributes(self):
        # --- *** THIS IS THE FIX *** ---
        # 1. Download the correct columns: 'valueInt' and 'valueFloat'
        df = self._download_csv(DOGMA_TYPE_ATTRIBUTES_URL, ['typeID', 'attributeID', 'valueInt', 'valueFloat'])
        
        # 2. Create the 'value' column by combining them.
        #    'valueInt' is used first, and if it's NaN, 'valueFloat' is used.
        df['value'] = df['valueInt'].fillna(df['valueFloat'])
        # --- *** END THE FIX *** ---

        self.stdout.write("Importing Dogma Type Attributes (this is the big one)...")
        
        EveTypeDogmaAttribute.objects.all().delete() # Clear old data
        
        # Get all types and attributes for FK mapping
        types = {t.type_id: t for t in EveType.objects.all()}
        attributes = {a.attribute_id: a for a in EveDogmaAttribute.objects.all()}
        
        type_attributes = []
        for _, row in df.iterrows():
            # Only import if we know about both the type and the attribute
            if row['typeID'] in types and row['attributeID'] in attributes:
                type_attributes.append(
                    EveTypeDogmaAttribute(
                        type=types[row['typeID']],
                        attribute=attributes[row['attributeID']],
                        value=row['value'] if pd.notna(row['value']) else None
                    )
                )

        EveTypeDogmaAttribute.objects.bulk_create(type_attributes, batch_size=1000)
        self.stdout.write(f"Imported {len(type_attributes)} type attribute links.")

    def import_dogma_type_effects(self):
        df = self._download_csv(DOGMA_EFFECTS_URL, ['typeID', 'effectID'])
        self.stdout.write("Importing Dogma Type Effects (for module slots)...")

        # Get all types for FK mapping
        types = {t.type_id: t for t in EveType.objects.filter(group__category_id__in=[7, 18])} # Modules & Drones
        
        # Invert the effect ID map for easy lookup
        effect_id_to_slot = {v: k for k, v in DOGMA_EFFECT_IDS.items()}
        
        types_to_update = []
        
        # Drones (Category 18) are special
        drone_group_ids = set(EveGroup.objects.filter(category_id=18).values_list('group_id', flat=True))
        for type_obj in types.values():
            if type_obj.group_id in drone_group_ids:
                type_obj.slot_type = 'drone'
                types_to_update.append(type_obj)
        
        self.stdout.write(f"Marked {len(types_to_update)} types as 'drone'.")

        # Modules
        for _, row in df.iterrows():
            if row['typeID'] in types and row['effectID'] in effect_id_to_slot:
                type_obj = types[row['typeID']]
                if not type_obj.slot_type: # Only set if not already set (e.g., as 'drone')
                    type_obj.slot_type = effect_id_to_slot[row['effectID']]
                    types_to_update.append(type_obj)

        EveType.objects.bulk_update(types_to_update, ['slot_type'], batch_size=1000)
        self.stdout.write(f"Updated {len(types_to_update)} module slot types.")

    def populate_evetype_helpers(self):
        self.stdout.write("Populating EveType helper fields (slots, meta level)...")
        
        # Use raw SQL for this bulk update, it's much faster
        
        with connection.cursor() as cursor:
            for field_name, attr_id in DOGMA_ATTR_IDS.items():
                self.stdout.write(f"  - Populating '{field_name}' from attribute {attr_id}...")
                
                # --- *** THIS IS THE FIX *** ---
                # The previous SQL (UPDATE...FROM) was for PostgreSQL.
                # This is the correct syntax for MySQL/MariaDB (UPDATE...JOIN...SET).
                rowcount = cursor.execute(
                    f"""
                    UPDATE {EveType._meta.db_table} AS t
                    JOIN {EveTypeDogmaAttribute._meta.db_table} AS da
                      ON t.type_id = da.type_id
                    SET t.{field_name} = da.value
                    WHERE da.attribute_id = %s
                    """,
                    [attr_id]
                )
                # --- *** END THE FIX *** ---
                
                self.stdout.write(f"    ...updated {rowcount} rows.")
        
        # Set meta_level to 0 for any items that are still NULL
        updated = EveType.objects.filter(meta_level__isnull=True).update(meta_level=0)
        self.stdout.write(f"  - Set {updated} NULL meta_levels to 0.")