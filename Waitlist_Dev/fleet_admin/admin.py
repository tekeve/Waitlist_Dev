from django.contrib import admin
# Import the models that *actually exist* from waitlist.models
# --- MODIFIED: Import new models ---
from waitlist.models import (
    EveCharacter, ShipFit, Fleet, FleetWaitlist, DoctrineFit,
    FitSubstitutionGroup, FleetWing, FleetSquad
)
# --- END MODIFIED ---
# --- NEW IMPORTS for DoctrineFit Admin ---
from django import forms
from django.core.exceptions import ValidationError
# --- MODIFIED: Import renamed function ---
from waitlist.fit_parser import parse_eft_to_full_doctrine_data
# --- END MODIFIED ---
import json
# --- END NEW IMPORTS ---


# We will control FC/Admin permissions via Django's User/Group system,
# so we don't need a separate FleetCommander model registration for now.

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
    list_display = ('character', 'ship_name', 'status', 'category', 'submitted_at', 'waitlist') # --- MODIFIED ---
    list_filter = ('status', 'category', 'submitted_at', 'waitlist') # --- MODIFIED ---
    search_fields = ('character__character_name', 'ship_name') # --- MODIFIED ---
    
    # Make status and denial_reason editable from the list view
    list_editable = ('status', 'category',) # --- MODIFIED ---
    
    # Add fields to the detail view
    # --- FIX: Make the new fields read-only for now ---
    readonly_fields = (
        'character', 'raw_fit', 'submitted_at', 'last_updated', 'waitlist',
        'ship_name', 'ship_type_id', 'tank_type', 'fit_issues', 'total_fleet_hours', 'hull_fleet_hours',
        'parsed_fit_json', # --- ADDED ---
    )
    
    fieldsets = (
        (None, {
            'fields': ('character', 'status', 'category', 'denial_reason', 'waitlist') # --- MODIFIED ---
        }),
        ('Fit Details', {
            'classes': ('collapse',), # Make this section collapsible
            'fields': ('raw_fit', 'parsed_fit_json', 'submitted_at', 'last_updated') # --- MODIFIED ---
        }),
        # --- NEW: Read-only section for parsed data ---
        ('Parsed Data', {
            'classes': ('collapse',),
            'fields': (
                'ship_name', 'ship_type_id', 'tank_type', 'fit_issues', 
                'total_fleet_hours', 'hull_fleet_hours'
            )
        }),
    )
    # --- END FIX ---

    # Add custom actions to the admin
    actions = ['approve_fits', 'deny_fits']

    def get_fit_summary(self, obj):
        """Returns the first line of the raw_fit, usually the ship name."""
        try:
            return obj.raw_fit.splitlines()[0]
        except (IndexError, AttributeError):
            return "Empty Fit"
    get_fit_summary.short_description = "Fit Summary"

    def approve_fits(self, request, queryset):
        # --- MODIFIED: Don't auto-assign category ---
        queryset.update(status='APPROVED', denial_reason=None)
    approve_fits.short_description = "Approve selected fits"

    def deny_fits(self, request, queryset):
        queryset.update(status='DENIED', denial_reason="Fit does not meet doctrine.")
    deny_fits.short_description = "Deny selected fits (default reason)"

@admin.register(Fleet)
class FleetAdmin(admin.ModelAdmin): # Corrected this line (removed extra .admin)
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
    
    # --- REMOVED ---
    # filter_horizontal = ('approved_fits',)

    def get_approved_count(self, obj):
        # --- UPDATED: Use new related name ---
        return obj.all_fits.filter(status='APPROVED').count()
    get_approved_count.short_description = "Approved Fits"


# --- NEW: Custom Form and Admin for DoctrineFit ---

class DoctrineFitForm(forms.ModelForm):
    """
    Custom form for the DoctrineFit admin to add an
    EFT fit importer.
    """
    # This is an extra, non-model field
    eft_fit_input = forms.CharField(
        label="EFT Fit Importer (Optional)",
        widget=forms.Textarea(attrs={'rows': 15, 'cols': 80}),
        required=False,
        help_text="Paste a full EFT-style fit here. This will automatically parse, cache SDE, and populate the 'Ship type' and 'Fit items JSON' fields below."
    )

    class Meta:
        model = DoctrineFit
        fields = '__all__'

    def clean(self):
        """
        This method is called during form validation.
        We use it to populate the real fields from our importer.
        """
        cleaned_data = super().clean()
        eft_fit = cleaned_data.get('eft_fit_input')
        
        # If the user pasted a fit, parse it
        if eft_fit:
            try:
                # --- MODIFIED: Run the new parser ---
                ship_type, fit_summary, parsed_list_json = parse_eft_to_full_doctrine_data(eft_fit)
                
                # Success! Populate the real fields
                cleaned_data['ship_type'] = ship_type
                cleaned_data['fit_items_json'] = json.dumps(fit_summary)
                cleaned_data['raw_fit_eft'] = eft_fit
                cleaned_data['parsed_fit_json'] = parsed_list_json
                # --- END MODIFICATION ---
            
            except Exception as e:
                # Parser failed, raise an error on the EFT field
                raise ValidationError({
                    'eft_fit_input': f"Failed to parse EFT fit: {str(e)}"
                })
        
        # If no EFT fit, the other fields must be valid
        # --- THIS IS THE FIX ---
        # We now check this *after* the parser has had a chance to run
        elif not cleaned_data.get('ship_type') or not cleaned_data.get('fit_items_json'):
            raise ValidationError(
                "If not using the EFT Importer, 'Ship type' and 'Fit items JSON' are required."
            )
        # --- END FIX ---

        return cleaned_data


@admin.register(DoctrineFit)
class DoctrineFitAdmin(admin.ModelAdmin):
    """
    Admin view for managing Doctrine Fits.
    """
    # Use our custom form
    form = DoctrineFitForm
    
    list_display = ('name', 'ship_type', 'category')
    list_filter = ('category', 'ship_type')
    search_fields = ('name', 'ship_type__name')
    
    # Make the JSON field collapsible
    fieldsets = (
        (None, {
            'fields': ('name', 'category', 'description')
        }),
        # --- NEW: Add EFT Importer fieldset ---
        ('EFT Fit Importer (Optional)', {
            'classes': ('collapse',),
            'fields': ('eft_fit_input',)
        }),
        # --- END NEW ---
        # --- MODIFIED: Added new fields ---
        ('Doctrine Definition (Auto-populated)', {
            'fields': ('ship_type', 'fit_items_json', 'raw_fit_eft', 'parsed_fit_json')
        }),
        # --- END MODIFICATION ---
    )
    
    # ---
    # --- THE FIX IS HERE ---
    # ---
    # We remove 'fit_items_json', 'raw_fit_eft', and 'parsed_fit_json'
    # from readonly_fields so the form's clean() method can save them.
    # We will make 'ship_type' readonly instead, as it's set by the parser.
    readonly_fields = ('ship_type',)
    # ---
    # --- END THE FIX ---
    # ---


# --- NEW: Admin for FitSubstitutionGroup ---
@admin.register(FitSubstitutionGroup)
class FitSubstitutionGroupAdmin(admin.ModelAdmin):
    """
    Admin view for managing module substitution groups.
    """
    list_display = ('name', 'base_item')
    search_fields = ('name', 'base_item__name')
    
    # Use autocomplete fields for easy selection
    autocomplete_fields = ('base_item', 'substitutes')
    
    # Use a filter horizontal for a nice M2M interface
    filter_horizontal = ('substitutes',)

# --- END NEW ---


# ---
# --- NEW: Register Fleet Structure Models ---
# ---
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
# ---
# --- END NEW ---
# ---