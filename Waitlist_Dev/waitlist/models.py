from django.db import models
from django.conf import settings
# --- MODIFIED: Import EveType for the new model ---
from pilot.models import EveType
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