from django.contrib import admin
from waitlist.models import (
    EveCharacter, ShipFit, Fleet, FleetWaitlist,
    FleetWing, FleetSquad, DoctrineFit
)
# --- MODIFICATION: Import EveType ---
from pilot.models import EveType, EveGroup
from django import forms
from django.core.exceptions import ValidationError
# --- MODIFICATION: Import parser ---
from waitlist.fit_parser import parse_eft_fit
# --- END MODIFICATION ---
import json
import logging

# Get a logger for this file
logger = logging.getLogger(__name__)

@admin.register(EveCharacter)
class EveCharacterAdmin(admin.ModelAdmin):
    """
    Admin view for EVE Characters.
    """
    list_display = ('character_name', 'character_id', 'user')
    search_fields = ('character_name', 'user__username')

@admin.register(ShipFit)
class ShipFitAdmin(admin.ModelAdmin):
    """
    Admin view for submitted Ship Fits.
    This is where FCs will approve/deny fits.
    """
    list_display = ('character', 'ship_name', 'status', 'category', 'submitted_at', 'waitlist')
    list_filter = ('status', 'category', 'submitted_at', 'waitlist')
    search_fields = ('character__character_name', 'ship_name')
    
    list_editable = ('status', 'category',)
    
    readonly_fields = (
        'character', 'raw_fit', 'submitted_at', 'last_updated', 'waitlist',
        'ship_name', 'ship_type_id', 'total_fleet_hours', 'hull_fleet_hours',
        'parsed_fit_json',
    )
    
    fieldsets = (
        (None, {
            'fields': ('character', 'status', 'category', 'denial_reason', 'waitlist')
        }),
        ('Fit Details', {
            'classes': ('collapse',),
            'fields': ('raw_fit', 'parsed_fit_json', 'submitted_at', 'last_updated')
        }),
        ('Parsed Data', {
            'classes': ('collapse',),
            'fields': (
                'ship_name', 'ship_type_id',
                'total_fleet_hours', 'hull_fleet_hours'
            )
        }),
    )

    actions = ['approve_fits', 'deny_fits']

    def get_fit_summary(self, obj):
        """Returns the first line of the raw_fit, usually the ship name."""
        try:
            return obj.raw_fit.splitlines()[0]
        except (IndexError, AttributeError):
            return "Empty Fit"
    get_fit_summary.short_description = "Fit Summary"

    def approve_fits(self, request, queryset):
        queryset.update(status='APPROVED', denial_reason=None)
    approve_fits.short_description = "Approve selected fits"

    def deny_fits(self, request, queryset):
        queryset.update(status='DENIED', denial_reason="Fit does not meet doctrine.")
    deny_fits.short_description = "Deny selected fits (default reason)"

@admin.register(Fleet)
class FleetAdmin(admin.ModelAdmin):
    """
    Admin view for managing active Fleets.
    """
    list_display = ('description', 'fleet_commander', 'esi_fleet_id', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('description', 'fleet_commander__character_name')

@admin.register(FleetWaitlist)
class FleetWaitlistAdmin(admin.ModelAdmin):
    """
    Admin view for managing Fleet Waitlists.
    """
    list_display = ('fleet', 'is_open', 'get_approved_count')
    list_filter = ('is_open',)

    def get_approved_count(self, obj):
        return obj.all_fits.filter(status='APPROVED').count()
    get_approved_count.short_description = "Approved Fits"

# ---
# --- NEW: DOCTRINE FIT ADMIN
# ---
class DoctrineFitAdminForm(forms.ModelForm):
    """
    Custom form to make pasting the EFT fit easier.
    """
    raw_fit_eft = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 20, 'cols': 80}),
        # --- MODIFICATION: Updated help text ---
        help_text="Paste the full EFT-formatted fit here. This field is REQUIRED on every save, even when just changing tiers, to ensure all data is parsed correctly.",
        # --- END MODIFICATION ---
        required=True # Make this required
    )
    
    # Make ship_type optional in the form, as we will populate it
    ship_type = forms.ModelChoiceField(
        queryset=EveType.objects.filter(group__category_id=6), # Category 6 = Ships
        required=False,
        help_text="This will be auto-populated from the EFT fit."
    )

    class Meta:
        model = DoctrineFit
        fields = '__all__'

    # --- NEW: Custom validation to check uniqueness of raw_fit_eft ---
    def clean_raw_fit_eft(self):
        """
        Check if a doctrine fit with this exact EFT string already exists.
        """
        raw_fit = self.cleaned_data.get('raw_fit_eft')
        
        # Check if a fit with this raw_fit already exists,
        # *excluding* the current instance (if we are editing)
        query = DoctrineFit.objects.filter(raw_fit_eft=raw_fit)
        if self.instance and self.instance.pk:
            query = query.exclude(pk=self.instance.pk)
            
        if query.exists():
            existing_fit = query.first()
            raise ValidationError(
                f"A doctrine fit with this exact EFT string already exists: '{existing_fit.name}' (ID: {existing_fit.id})."
            )
            
        return raw_fit
    # --- END NEW ---

@admin.register(DoctrineFit)
class DoctrineFitAdmin(admin.ModelAdmin):
    """
    Admin view for managing Doctrine Fits.
    """
    form = DoctrineFitAdminForm
    
    # --- MODIFICATION: Added tank_type ---
    list_display = ('name', 'ship_type', 'category', 'fleet_type', 'fit_tier', 'hull_tier', 'tank_type')
    list_filter = ('fleet_type', 'category', 'hull_tier', 'fit_tier', 'tank_type', 'ship_type__group__name')
    # --- END MODIFICATION ---
    search_fields = ('name', 'ship_type__name')
    
    # Use autocomplete for ship_type if it's set manually
    autocomplete_fields = ('ship_type',)
    
    # Define the layout of the admin form
    fieldsets = (
        (None, {
            # --- MODIFICATION: Added tank_type ---
            'fields': ('name', 'fleet_type', 'category', 'hull_tier', 'fit_tier', 'tank_type', 'description')
            # --- END MODIFICATION ---
        }),
        ('EFT Fit (Required)', {
            'fields': ('raw_fit_eft',)
        }),
        ('Auto-Generated Data (Read-Only)', {
            'classes': ('collapse',), # Hide by default
            'fields': ('ship_type', 'parsed_fit_json', 'fit_items_json')
        }),
    )
    
    # Make the auto-generated fields read-only in the admin
    readonly_fields = ('parsed_fit_json', 'fit_items_json')

    def save_model(self, request, obj: DoctrineFit, form, change):
        """
        Custom save logic to parse the EFT fit.
        """
        raw_fit_string = form.cleaned_data.get('raw_fit_eft')
        
        # --- MODIFICATION: Robust check for empty/whitespace fit ---
        if not raw_fit_string or raw_fit_string.isspace():
            # This should be caught by required=True, but we'll be extra safe.
            # This also aborts the save if the user somehow submits an empty field.
            self.message_user(
                request, 
                "Error: The 'Raw EFT Fit' field cannot be empty. Please paste the fit to save any changes.", 
                level='ERROR'
            )
            return # Abort the save
        # --- END MODIFICATION ---

        try:
            # 1. Parse the fit
            logger.info(f"Admin: Parsing EFT fit for new/updated doctrine: {obj.name}")
            ship_type, parsed_fit_list, fit_summary_counter = parse_eft_fit(raw_fit_string)
            
            # 2. Update the DoctrineFit object with parsed data
            obj.ship_type = ship_type
            obj.parsed_fit_json = json.dumps(parsed_fit_list)
            
            # Convert the Counter object to a plain dict for JSON serialization
            fit_items_dict = dict(fit_summary_counter)
            obj.fit_items_json = json.dumps(fit_items_dict)
            
            logger.info(f"Admin: Successfully parsed fit. Ship: {ship_type.name}")

        except ValueError as e:
            # If parsing fails, log the error and stop
            logger.error(f"Admin: Failed to parse EFT fit for {obj.name}. Error: {e}")
            # Add a message to the user
            self.message_user(request, f"Error parsing fit: {e}. Please correct the EFT string.", level='ERROR')
            # Don't save the object if parsing fails
            return
        except Exception as e:
            logger.error(f"Admin: An unexpected error occurred during parsing: {e}", exc_info=True)
            self.message_user(request, f"An unexpected error occurred: {e}", level='ERROR')
            return

        # 3. Call the default save method to save the object to the DB
        super().save_model(request, obj, form, change)
# ---
# --- END NEW: DOCTRINE FIT ADMIN
# ---

# Register Fleet Structure Models
class FleetSquadInline(admin.TabularInline):
    model = FleetSquad
    extra = 0
    fields = ('name', 'squad_id', 'assigned_category')
    readonly_fields = ('name', 'squad_id')

@admin.register(FleetWing)
class FleetWingAdmin(admin.ModelAdmin):
    list_display = ('name', 'wing_id', 'fleet')
    list_filter = ('fleet',)
    inlines = [FleetSquadInline]

@admin.register(FleetSquad)
class FleetSquadAdmin(admin.ModelAdmin):
    list_display = ('name', 'squad_id', 'wing', 'assigned_category')
    list_filter = ('wing__fleet', 'assigned_category')
    list_editable = ('assigned_category',)