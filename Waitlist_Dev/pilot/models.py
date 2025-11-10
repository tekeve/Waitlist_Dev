from django.db import models
# --- REMOVED: from waitlist.models import EveCharacter ---
import json

# --- *** NEW MODEL: EveCategory *** ---
# From SDE: invCategories.csv
class EveCategory(models.Model):
    category_id = models.IntegerField(primary_key=True, unique=True)
    name = models.CharField(max_length=255, db_index=True)
    icon_id = models.IntegerField(null=True, blank=True)
    published = models.BooleanField(default=True)

    def __str__(self):
        return self.name

# --- *** UPDATED MODEL: EveGroup *** ---
# From SDE: invGroups.csv
class EveGroup(models.Model):
    group_id = models.IntegerField(primary_key=True, unique=True)
    name = models.CharField(max_length=255, db_index=True)
    
    # --- *** MODIFIED: This is now a ForeignKey *** ---
    category = models.ForeignKey(
        EveCategory, 
        on_delete=models.CASCADE, 
        related_name="groups",
        null=True, # Allow null
        blank=True
    )
    # --- *** END MODIFICATION *** ---
    
    icon_id = models.IntegerField(null=True, blank=True)
    published = models.BooleanField(default=True)

    def __str__(self):
        return self.name

# --- *** UPDATED MODEL: EveType *** ---
# From SDE: invTypes.csv
class EveType(models.Model):
    type_id = models.IntegerField(primary_key=True, unique=True)
    name = models.CharField(max_length=255, db_index=True)
    group = models.ForeignKey(
        EveGroup, 
        on_delete=models.CASCADE, 
        related_name="types",
        null=True, # Allow null
        blank=True
    )
    
    description = models.TextField(blank=True, null=True)
    mass = models.FloatField(null=True, blank=True)
    volume = models.FloatField(null=True, blank=True)
    capacity = models.FloatField(null=True, blank=True)
    icon_id = models.IntegerField(null=True, blank=True)
    published = models.BooleanField(default=True)
    
    # --- *** MODIFIED: Kept these fields, will be populated by SDE importer *** ---
    # These come from dgmTypeAttributes.csv
    hi_slots = models.IntegerField(null=True, blank=True, help_text="Ship: High slots (Dogma Attr 14)")
    med_slots = models.IntegerField(null=True, blank=True, help_text="Ship: Medium slots (Dogma Attr 13)")
    low_slots = models.IntegerField(null=True, blank=True, help_text="Ship: Low slots (Dogma Attr 12)")
    rig_slots = models.IntegerField(null=True, blank=True, help_text="Ship: Rig slots (Dogma Attr 1137)")
    subsystem_slots = models.IntegerField(null=True, blank=True, help_text="Ship: Subsystem slots (Dogma Attr 1367)")
    
    # This comes from dgmTypeEffects.csv
    slot_type = models.CharField(
        max_length=10, 
        null=True, 
        blank=True, 
        db_index=True, 
        help_text="Module: 'high', 'mid', 'low', 'rig', 'subsystem', or 'drone'"
    )
    
    # This comes from dgmTypeAttributes.csv
    meta_level = models.IntegerField(
        null=True, 
        blank=True, 
        default=0, 
        help_text="Item meta level (Dogma Attr 633)"
    )
    
    # --- *** REMOVED: attributes_json is no longer needed *** ---
    # attributes_json = models.TextField(...)
    # --- *** END REMOVAL *** ---

    def __str__(self):
        return self.name
# --- END UPDATED MODELS ---


class PilotSnapshot(models.Model):
    """
    Stores a snapshot of a character's skills and implants.
    This avoids us having to store millions of individual skill records.
    We can just fetch the JSON blob from ESI and store it.
    """
    character = models.OneToOneField(
        'waitlist.EveCharacter', # --- MODIFIED: Use string notation ---
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="pilot_snapshot"
    )
    
    # We will store the direct JSON response from ESI.
    # This is much more efficient than creating 500+ skill objects.
    # We'll need to import json to load/dump this.
    skills_json = models.TextField(blank=True, null=True, help_text="JSON response from ESI /skills/ endpoint")
    implants_json = models.TextField(blank=True, null=True, help_text="JSON response from ESI /implants/ endpoint")
    
    last_updated = models.DateTimeField(auto_now=True)

    # --- MODIFIED THIS METHOD ---
    def get_implant_ids(self):
        """Helper to get implant ID list from JSON."""
        if not self.implants_json:
            return []
        try:
            # The ESI response is just a list of type_ids, e.g., [33323, 22118]
            implant_ids = json.loads(self.implants_json)
            return implant_ids
        except json.JSONDecodeError:
            return []
    # --- END MODIFICATION ---

    def get_skills(self):
        """Helper to get skill list from JSON."""
        if not self.skills_json:
            return []
        try:
            # The ESI response is a dict, e.g.:
            # {"skills": [{"skill_id": 3339, "active_skill_level": 5}, ...], "total_sp": 150000000}
            skills_data = json.loads(self.skills_json)
            
            # We will just return the list of skill dicts for the template
            # We can add 'icon_url' here if we want, but it's slow.
            # It's better to just pass the skill_id to the template.
            return skills_data.get('skills', [])
        except json.JSONDecodeError:
            return []
            
    def get_total_sp(self):
        """Helper to get total SP from JSON."""
        if not self.skills_json:
            return 0
        try:
            skills_data = json.loads(self.skills_json)
            return skills_data.get('total_sp', 0)
        except json.JSONDecodeError:
            return 0

    def __str__(self):
        return f"Snapshot for {self.character.character_name}"