# --- NEW FILE ---
from django.contrib import admin
from .models import PilotSnapshot, EveGroup, EveType, EveCategory

# --- *** NEW: Register EveCategory *** ---
@admin.register(EveCategory)
class EveCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'category_id', 'published')
    search_fields = ('name',)
    list_filter = ('published',)
# --- *** END NEW *** ---

@admin.register(EveGroup)
class EveGroupAdmin(admin.ModelAdmin):
    # --- *** MODIFIED: Show category *** ---
    list_display = ('name', 'group_id', 'category', 'published')
    search_fields = ('name',)
    # --- *** MODIFIED: Filter by category name *** ---
    list_filter = ('published', 'category__name')
    autocomplete_fields = ('category',) # Add autocomplete
    # --- *** END MODIFICATION *** ---

@admin.register(EveType)
class EveTypeAdmin(admin.ModelAdmin):
    # --- *** MODIFIED: Show more useful data *** ---
    list_display = ('name', 'type_id', 'group', 'meta_level', 'published')
    search_fields = ('name', 'type_id')
    list_filter = ('published', 'group__category__name', 'group__name')
    autocomplete_fields = ('group',) # Add autocomplete
    # --- *** END MODIFICATION *** ---

# We can also register the snapshot if you want to see it
@admin.register(PilotSnapshot)
class PilotSnapshotAdmin(admin.ModelAdmin):
    list_display = ('character', 'last_updated', 'get_total_sp')
    search_fields = ('character__character_name',)