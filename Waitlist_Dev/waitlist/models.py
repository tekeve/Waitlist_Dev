from django.db import models
from django.conf import settings
# --- MODIFIED: Import EveType for the new model ---
from pilot.models import EveType, EveGroup
import json # Import json for our new fields

# Create your models here.

class EveCharacter(models.Model):
    """
    Stores EVE Online character data linked to a Django user.
    """
    # This links the EVE character to a user in Django's built-in auth system.
    # A User can have multiple EVE characters.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="eve_characters"
    )
    character_id = models.BigIntegerField(unique=True, primary_key=True)
    character_name = models.CharField(max_length=255)

    # ESI token information
    # We encrypt these in a real app, but store as text for this example.
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expiry = models.DateTimeField()
    
    # --- NEW: Fields for alt management ---
    is_main = models.BooleanField(
        default=False,
        help_text="Is this the main character for this user account?"
    )
    # --- END NEW ---
    
    # --- NEW: Corp/Alliance Info ---
    corporation_id = models.BigIntegerField(null=True, blank=True)
    corporation_name = models.CharField(max_length=255, null=True, blank=True)
    alliance_id = models.BigIntegerField(null=True, blank=True)
    alliance_name = models.CharField(max_length=255, null=True, blank=True)
    # --- END NEW ---

    def __str__(self):
        return self.character_name

class Fleet(models.Model):
    """
    Represents a fleet that a character can be invited to.
    Managed by FCs.
    
    --- MODIFIED FOR STATIC FLEETS ---
    These objects (e.g., 'Headquarters', 'Assaults') will now be static.
    'is_active', 'fleet_commander', and 'esi_fleet_id' will be 
    set when an FC "opens" the fleet, and cleared when "closed".
    --- END MODIFICATION ---
    """
    fleet_commander = models.ForeignKey(
        EveCharacter,
        # --- MODIFIED: Allow this to be null when no FC is active ---
        on_delete=models.SET_NULL, 
        null=True,
        blank=True,
        # --- END MODIFICATION ---
        related_name="commanded_fleets"
    )
    esi_fleet_id = models.BigIntegerField(
        # --- MODIFIED: Allow null, but keep unique if set ---
        unique=True, 
        null=True, 
        blank=True,
        # --- END MODIFICATION ---
        help_text="The ESI ID of the active fleet."
    )
    is_active = models.BooleanField(default=False) # --- MODIFIED: Default to False ---
    description = models.CharField(
        max_length=255, 
        unique=True # --- ADDED: Ensures we only have one 'Headquarters' etc.
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        if self.is_active and self.fleet_commander:
             return f"{self.description} (FC: {self.fleet_commander.character_name})"
        return f"{self.description} (Inactive)"

class FleetWaitlist(models.Model):
    """
    A specific waitlist for a specific fleet.
    This links a Fleet to a list of approved ShipFits.
    """
    fleet = models.OneToOneField(
        Fleet,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="waitlist" # Add related_name
    )
    is_open = models.BooleanField(default=False) # --- MODIFIED: Default to False ---

    def __str__(self):
        return f"Waitlist for {self.fleet.description}"


class ShipFit(models.Model):
    """
    Represents a single ship fit submitted to the waitlist.
    """
    class FitStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        DENIED = 'DENIED', 'Denied'
        IN_FLEET = 'IN_FLEET', 'In Fleet' # Not used yet, but good to have

    # --- NEW: Category Choices ---
    # This will be used for sorting into columns
    class FitCategory(models.TextChoices):
        NONE = 'NONE', 'None'
        DPS = 'DPS', 'DPS'
        LOGI = 'LOGI', 'Logi'
        SNIPER = 'SNIPER', 'Sniper'
        MAR_DPS = 'MAR_DPS', 'MAR DPS'
        MAR_SNIPER = 'MAR_SNIPER', 'MAR Sniper'
        OTHER = 'OTHER', 'Other' # <-- ADDED THIS NEW CATEGORY

    # --- UPDATED: Link to the Waitlist directly ---
    waitlist = models.ForeignKey(
        FleetWaitlist,
        on_delete=models.CASCADE,
        related_name="all_fits",
        null=True, # Allow fits to exist without a waitlist (for history)
        blank=True
    )
    
    # The character who submitted this fit
    character = models.ForeignKey(
        EveCharacter,
        on_delete=models.CASCADE,
        related_name="submitted_fits"
    )
    
    # The raw fit string (EFT format or similar) pasted by the user
    raw_fit = models.TextField(help_text="The ship fit in EFT (or similar) format.")
    
    # --- NEW: Store the fully parsed fit as JSON ---
    parsed_fit_json = models.TextField(
        null=True, 
        blank=True, 
        help_text="JSON representation of the parsed fit, including item IDs and icon URLs"
    )
    # --- END NEW ---

    # Status of the fit in the waitlist
    status = models.CharField(
        max_length=10,
        choices=FitStatus.choices,
        default=FitStatus.PENDING,
        db_index=True # Good to index this for fast filtering
    )

    # Reason for denial, to be filled in by an FC
    denial_reason = models.TextField(blank=True, null=True)

    # Timestamps
    submitted_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    # --- NEW FIELDS for parsed data (placeholders) ---
    # These will be filled by the fit_parser later
    
    ship_name = models.CharField(max_length=255, null=True, blank=True)
    ship_type_id = models.BigIntegerField(null=True, blank=True) # --- ADDED FOR IMAGE ---
    tank_type = models.CharField(max_length=50, null=True, blank=True)
    fit_issues = models.TextField(null=True, blank=True)
    
    # --- NEW: Category for sorting ---
    category = models.CharField(
        max_length=20,
        choices=FitCategory.choices,
        default=FitCategory.NONE,
        db_index=True
    )

    # These are placeholders for future ESI tracking
    total_fleet_hours = models.IntegerField(default=0)
    hull_fleet_hours = models.IntegerField(default=0)
    # --- END NEW FIELDS ---

    def __str__(self):
        return f"{self.character.character_name} - {self.ship_name} ({self.status})"

    def get_parsed_fit_summary(self):
        """
        Helper method to get the parsed fit as a Python dict
        for auto-approval checking.
        """
        if not self.parsed_fit_json:
            return {}
        try:
            parsed_list = json.loads(self.parsed_fit_json)
            # Convert list of dicts to a dict of {type_id: quantity}
            fit_summary = {}
            for item in parsed_list:
                if item.get('type_id'):
                    type_id = str(item['type_id']) # Use string keys for JSON consistency
                    quantity = item.get('quantity', 1)
                    fit_summary[type_id] = fit_summary.get(type_id, 0) + quantity
            return fit_summary
        except json.JSONDecodeError:
            return {}


# --- NEW MODEL: DoctrineFit ---
class DoctrineFit(models.Model):
    """
    Stores an approved doctrine fit for auto-approval.
    """
    name = models.CharField(max_length=255, unique=True, help_text="e.g., 'Standard Vargur', 'Logi Basilisk'")
    
    # Link to the ship hull type in our mini-SDE
    ship_type = models.ForeignKey(
        'pilot.EveType', # --- MODIFIED: Use string notation ---
        on_delete=models.CASCADE,
        related_name="doctrine_fits",
        null=True, blank=True # --- MODIFIED: Make optional for form ---
    )
    
    # The category this fit belongs to (for auto-sorting)
    category = models.CharField(
        max_length=20,
        choices=ShipFit.FitCategory.choices,
        default=ShipFit.FitCategory.NONE,
        db_index=True
    )
    
    # The actual items and quantities, stored as JSON
    # Example: {"31718": 1, "2048": 8, "2605": 8, ...}
    # (Keys are type_ids as strings, values are quantities)
    fit_items_json = models.TextField(
        help_text="JSON dictionary of {type_id: quantity} for all required items",
        blank=True # --- MODIFIED: Make optional for form ---
    )

    description = models.TextField(blank=True, null=True)
    
    # --- NEW FIELDS ---
    raw_fit_eft = models.TextField(
        blank=True, 
        null=True, 
        help_text="The raw EFT-formatted fit string."
    )
    parsed_fit_json = models.TextField(
        blank=True, 
        null=True, 
        help_text="JSON representation of the parsed fit (slotted)."
    )
    # --- END NEW FIELDS ---

    def __str__(self):
        return self.name
        
    def get_fit_items(self):
        """Helper to get the fit items as a Python dict."""
        if not self.fit_items_json:
            return {}
        try:
            return json.loads(self.fit_items_json)
        except json.JSONDecodeError:
            return {}

    # --- NEW HELPER METHOD ---
    def get_parsed_fit_list(self):
        """
        Helper method to get the parsed, slotted fit
        list from the JSON blob.
        """
        if not self.parsed_fit_json:
            return []
        try:
            return json.loads(self.parsed_fit_json)
        except json.JSONDecodeError:
            return []
    # --- END NEW HELPER METHOD ---
# --- END NEW MODEL ---


# --- NEW MODEL: FitSubstitutionGroup ---
class FitSubstitutionGroup(models.Model):
    """
    Defines a group of 'equal or better' items for a base item.
    e.g., Base = T2 LSE, Substitutes = Faction, Deadspace LSEs
    """
    name = models.CharField(
        max_length=255, 
        unique=True, 
        help_text="e.g., 'T2 Large Shield Extenders', 'T2 Large Cap Batteries'"
    )
    
    base_item = models.ForeignKey(
        'pilot.EveType',
        on_delete=models.CASCADE,
        related_name='substitution_base',
        help_text="The base item specified in a doctrine (e.g., Large Shield Extender II)"
    )
    
    substitutes = models.ManyToManyField(
        'pilot.EveType',
        related_name='allowed_as_substitute',
        blank=True,
        help_text="All other items that are considered 'equal or better' (e.g., Caldari Navy LSE)"
    )

    def __str__(self):
        return self.name
# --- END NEW MODEL ---


# ---
# --- NEW FLEET STRUCTURE MODELS ---
# ---
class FleetWing(models.Model):
    """
    Represents a wing in an EVE fleet.
    Created/updated when an FC opens the waitlist.
    """
    fleet = models.ForeignKey(Fleet, on_delete=models.CASCADE, related_name="wings")
    wing_id = models.BigIntegerField()
    name = models.CharField(max_length=100)
    
    class Meta:
        unique_together = ('fleet', 'wing_id')

    def __str__(self):
        return f"{self.fleet.description} - Wing {self.wing_id}: {self.name}"

class FleetSquad(models.Model):
    """
    Represents a squad in an EVE fleet wing.
    This model maps a waitlist category to an in-game squad.
    """
    wing = models.ForeignKey(FleetWing, on_delete=models.CASCADE, related_name="squads")
    squad_id = models.BigIntegerField()
    name = models.CharField(max_length=100)
    
    # This is the key field for mapping
    assigned_category = models.CharField(
        max_length=20,
        choices=ShipFit.FitCategory.choices,
        null=True, # We can't use 'NONE' as default, as 'unique' would fail
        blank=True,
        unique=True, # Ensures one squad per category
        db_index=True
    )

    class Meta:
        unique_together = ('wing', 'squad_id')

    def __str__(self):
        return f"Squad {self.squad_id}: {self.name} (Category: {self.get_assigned_category_display()})"
# ---
# --- END NEW FLEET STRUCTURE MODELS ---
# ---


# ---
# --- NEW MODELS FOR DYNAMIC ITEM COMPARISON ---
# ---
class EveDogmaAttribute(models.Model):
    """
    Stores a Dogma Attribute so FCs can select them by name
    instead of remembering the ID.
    e.g., (20, "cpuPenalty", "...")
    """
    attribute_id = models.IntegerField(primary_key=True, unique=True)
    name = models.CharField(max_length=255, db_index=True)
    description = models.TextField(blank=True, null=True)
    icon_id = models.IntegerField(blank=True, null=True)
    unit_name = models.CharField(max_length=50, blank=True, null=True) # e.g., "CPU" or "m"

    def __str__(self):
        return f"{self.name} (ID: {self.attribute_id})"

class ItemComparisonRule(models.Model):
    """
    Defines the "equal or better" logic for an item group.
    An FC can configure this in the admin.
    
    Example Rule:
    - group = "Co-Processor"
    - attribute = "cpuPenalty"
    - higher_is_better = False
    
    This tells the parser: "For Co-Processors, check the 'cpuPenalty'
    attribute, and a lower value is better."
    """
    # Use a string ref to pilot.EveGroup to avoid import issues
    group = models.ForeignKey(
        'pilot.EveGroup',
        on_delete=models.CASCADE,
        related_name="comparison_rules",
        help_text="The Item Group this rule applies to (e.g., 'Large Shield Extender')."
    )
    attribute = models.ForeignKey(
        EveDogmaAttribute,
        on_delete=models.CASCADE,
        help_text="The attribute to check (e.g., 'shieldCapacity')."
    )
    higher_is_better = models.BooleanField(
        default=True,
        help_text="Check if 'Higher is Better' (e.g., for damage). Uncheck if 'Lower is Better' (e.g., for 'cpuPenalty')."
    )

    class Meta:
        # Ensure we only have one rule per group/attribute combination
        unique_together = ('group', 'attribute')

    def __str__(self):
        comparison = "Higher is Better" if self.higher_is_better else "Lower is Better"
        return f"Rule for {self.group.name}: Check {self.attribute.name} ({comparison})"
# ---
# --- END NEW MODELS
# ---


# --- *** NEW MODEL: EveTypeDogmaAttribute *** ---
# From SDE: dgmTypeAttributes.csv
class EveTypeDogmaAttribute(models.Model):
    """
    This is the link table that stores the *value* of a
    specific attribute for a specific item.
    This replaces the attributes_json field on EveType.
    
    Example Row:
    - type = "Vargur" (EveType object)
    - attribute = "hiSlots" (EveDogmaAttribute object)
    - value = 8
    """
    type = models.ForeignKey(
        'pilot.EveType',
        on_delete=models.CASCADE,
        related_name="dogma_attributes"
    )
    attribute = models.ForeignKey(
        EveDogmaAttribute,
        on_delete=models.CASCADE,
        related_name="type_values"
    )
    value = models.FloatField(null=True, blank=True)

    class Meta:
        # We only need one entry per type/attribute pair
        unique_together = ('type', 'attribute')
        # Add an index on type_id to speed up lookups for a specific item
        indexes = [
            models.Index(fields=['type']),
        ]

    def __str__(self):
        return f"{self.type.name} - {self.attribute.name}: {self.value}"
# --- *** END NEW MODEL *** ---