from django.contrib import admin
# --- MODIFICATION: Removed unused model imports ---
from waitlist.models import (
    EveCharacter, ShipFit, Fleet, FleetWaitlist,
    FleetWing, FleetSquad
)
# --- END MODIFICATION ---
from pilot.models import EveType, EveGroup
from django import forms
from django.core.exceptions import ValidationError
# --- MODIFICATION: Removed unused import ---
# from waitlist.fit_parser import parse_eft_to_full_doctrine_data
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
    
    # --- MODIFICATION: Simplified readonly_fields ---
    readonly_fields = (
        'character', 'raw_fit', 'submitted_at', 'last_updated', 'waitlist',
        'ship_name', 'ship_type_id', 'total_fleet_hours', 'hull_fleet_hours',
        'parsed_fit_json',
    )
    # --- END MODIFICATION ---
    
    fieldsets = (
        (None, {
            'fields': ('character', 'status', 'category', 'denial_reason', 'waitlist')
        }),
        ('Fit Details', {
            'classes': ('collapse',),
            'fields': ('raw_fit', 'parsed_fit_json', 'submitted_at', 'last_updated')
        }),
        # --- MODIFICATION: Simplified Parsed Data fieldset ---
        ('Parsed Data', {
            'classes': ('collapse',),
            'fields': (
                'ship_name', 'ship_type_id',
                'total_fleet_hours', 'hull_fleet_hours'
            )
        }),
        # --- END MODIFICATION ---
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

# --- MODIFICATION: Removed DoctrineFitForm ---

# --- MODIFICATION: Removed @admin.register(DoctrineFit) ---

# --- MODIFICATION: Removed @admin.register(FitSubstitutionGroup) ---

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

# --- MODIFICATION: Removed all rule model admin registrations ---
# --- (EveDogmaAttributeAdmin, ItemComparisonRuleAdmin, EveTypeDogmaAttributeAdmin) ---