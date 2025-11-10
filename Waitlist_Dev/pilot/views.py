from django.shortcuts import render, get_object_or_404, redirect, resolve_url
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import logout
from django.utils import timezone
from datetime import timedelta, datetime # --- Import datetime ---
import json
import requests # For handling HTTP errors during refresh
# --- NEW IMPORT ---
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse
from django.template.loader import render_to_string # --- NEW IMPORT ---
from django.views.decorators.http import require_POST # --- THIS IS THE FIX ---

from waitlist.models import EveCharacter
from .models import PilotSnapshot, EveGroup, EveType, EveCategory

from esi.clients import EsiClientProvider
from esi.models import Token
# --- NEW: Import ESI exceptions ---
from bravado.exception import HTTPNotFound
from django.db import transaction # --- NEW: Import transaction ---

# ---
# --- NEW: is_fleet_commander helper (copied from waitlist.views) ---
# ---
def is_fleet_commander(user):
    """
    Checks if a user is in the 'Fleet Commander' group.
    """
    return user.groups.filter(name='Fleet Commander').exists()
# ---
# --- END NEW HELPER
# ---


# --- HELPER FUNCTION: GET AND REFRESH TOKEN ---
# We need this logic in both views, so let's make it a helper
def get_refreshed_token_for_character(user, character):
    """
    Fetches and, if necessary, refreshes the ESI token for a character.
    Handles auth failure by logging the user out.
    Returns the valid Token object or None if a redirect is needed.
    """
    # ---
    # --- THE FIX IS HERE ---
    # ---
    # We wrap the entire function in a generic try/except to catch
    # unexpected errors (like TypeError) and fail safely by returning None.
    try:
        token = Token.objects.filter(
            user=user, 
            character_id=character.character_id
        ).order_by('-created').first()
        
        if not token:
            raise Token.DoesNotExist

        # --- MODIFIED: Check for None *before* comparing to timezone.now()
        if not character.token_expiry or character.token_expiry < timezone.now():
            token.refresh()
            character.access_token = token.access_token
            character.token_expiry = token.expires # .expires is added in-memory by .refresh()
            
            # --- NEW: Refresh public data on token refresh ---
            esi = EsiClientProvider()
            try:
                public_data = esi.client.Character.get_characters_character_id(
                    character_id=character.character_id
                ).results()
                
                corp_id = public_data.get('corporation_id')
                alliance_id = public_data.get('alliance_id')
                
                corp_name = None
                if corp_id:
                    corp_data = esi.client.Corporation.get_corporations_corporation_id(
                        corporation_id=corp_id
                    ).results()
                    corp_name = corp_data.get('name')
                    
                alliance_name = None
                if alliance_id:
                    try:
                        alliance_data = esi.client.Alliance.get_alliances_alliance_id(
                            alliance_id=alliance_id
                        ).results()
                        alliance_name = alliance_data.get('name')
                    except HTTPNotFound:
                        alliance_name = "N/A" # Handle dead alliances
                
                # Update character model
                character.corporation_id = corp_id
                character.corporation_name = corp_name
                character.alliance_id = alliance_id
                character.alliance_name = alliance_name
                
            except Exception as e:
                print(f"Error refreshing public data for {character.character_id}: {e}")
            # --- END NEW ---
            
            character.save()
            
        return token

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 400:
            # Refresh token is invalid. Delete token and character.
            if 'token' in locals() and token:
                token.delete()
            character.delete()
            return None # Will cause a redirect
        else:
            raise e # Re-raise other ESI errors
    except Token.DoesNotExist:
        return None # Will cause a redirect
    except Exception as e:
        # --- ADDED: Catch any other errors (like TypeError)
        print(f"Error in get_refreshed_token_for_character: {e}")
        return None # Fail safely
    # --- END FIX ---
# --- END HELPER FUNCTION ---


@login_required
def pilot_detail(request, character_id):
    """
    Displays the skills and implants for a specific character.
    This view is now FAST and only loads data from the database.
    It passes a flag to the template if a refresh is needed.
    """
    
    esi = EsiClientProvider()
    character = get_object_or_404(EveCharacter, character_id=character_id, user=request.user)
    
    # 1. Get and refresh token (this is fast)
    token = get_refreshed_token_for_character(request.user, character)
    if not token:
        # Token was invalid, helper logged user out
        logout(request)
        return redirect('esi_auth:login')

    # 2. Check scopes (fast)
    required_scopes = ['esi-skills.read_skills.v1', 'esi-clones.read_implants.v1']
    available_scopes = set(s.name for s in token.scopes.all())
    has_all_scopes = all(scope in available_scopes for scope in required_scopes)
    if not has_all_scopes:
        return redirect(f"{resolve_url('esi_auth:login')}?scopes=regular")

    # 3. Get snapshot and check if it's stale
    snapshot, created = PilotSnapshot.objects.get_or_create(character=character)
    
    needs_update = False
    if created or snapshot.last_updated < (timezone.now() - timedelta(hours=1)):
        needs_update = True
    if not snapshot.skills_json or not snapshot.implants_json:
        needs_update = True
        
    # --- THIS VIEW NO LONGER RUNS THE ESI UPDATE ---
    # The 'if needs_update:' block of ESI calls has been MOVED
    # to the new api_refresh_pilot view.
            
    # --- SDE & GROUPING LOGIC (This is fast, it reads from our DB) ---
    grouped_skills = {}
    skills_list = snapshot.get_skills()
    if skills_list:
        all_skill_ids = [s['skill_id'] for s in skills_list]
        cached_types = {t.type_id: t for t in EveType.objects.filter(type_id__in=all_skill_ids).select_related('group')}
        
        # We ONLY show skills we have cached. The refresh API
        # will handle fetching any missing ones.
        for skill in skills_list:
            skill_id = skill['skill_id']
            if skill_id in cached_types:
                eve_type = cached_types[skill_id]
                group_name = eve_type.group.name
                
                if group_name not in grouped_skills:
                    grouped_skills[group_name] = []
                    
                grouped_skills[group_name].append({
                    'name': eve_type.name,
                    'level': skill['active_skill_level']
                })
    sorted_grouped_skills = dict(sorted(grouped_skills.items()))
    # --- END SDE & GROUPING LOGIC ---


    # --- IMPLANT LOGIC (This is fast, it reads from our DB) ---
    all_implant_ids = snapshot.get_implant_ids()
    enriched_implants = []
    if all_implant_ids:
        cached_implant_types = {t.type_id: t for t in EveType.objects.filter(type_id__in=all_implant_ids).select_related('group')}
        
        for implant_id in all_implant_ids:
            if implant_id in cached_implant_types:
                eve_type = cached_implant_types[implant_id]
                enriched_implants.append({
                    'type_id': implant_id,
                    'name': eve_type.name,
                    'group_name': eve_type.group.name,
                    'slot': eve_type.slot if eve_type.slot else 0,
                    'icon_url': f"https://images.evetech.net/types/{implant_id}/icon?size=64"
                })
    
    sorted_implants = sorted(enriched_implants, key=lambda i: i.get('slot', 0))
    
    implants_other = []
    implants_col1 = [] # Slots 1-5
    implants_col2 = [] # Slots 6-10
    for implant in sorted_implants:
        slot = implant.get('slot', 0)
        if 0 < slot <= 5:
            implants_col1.append(implant)
        elif 5 < slot <= 10:
            implants_col2.append(implant)
        else:
            implants_other.append(implant)
    # --- END IMPLANT LOGIC ---

    # --- NEW: Context logic for Main/Alts ---
    all_user_chars = request.user.eve_characters.all().order_by('character_name')
    main_char = all_user_chars.filter(is_main=True).first()
    if not main_char:
        main_char = all_user_chars.first()
    # --- END NEW ---

    context = {
        'character': character,
        'implants_other': implants_other,
        'implants_col1': implants_col1,
        'implants_col2': implants_col2,
        'total_sp': snapshot.get_total_sp(),
        'snapshot_time': snapshot.last_updated,
        'portrait_url': f"https://images.evetech.net/characters/{character.character_id}/portrait?size=256",
        'grouped_skills': sorted_grouped_skills,
        'needs_refresh': needs_update, # <-- Pass the flag!
        
        # --- NEW CONTEXT ---
        'is_fc': is_fleet_commander(request.user), # For base template
        'user_characters': all_user_chars, # For X-Up modal
        'all_chars_for_header': all_user_chars, # For header dropdown
        'main_char_for_header': main_char, # For header dropdown
        # --- END NEW CONTEXT ---
    }
    
    return render(request, 'pilot_detail.html', context)


# --- NEW API VIEW ---
@login_required
def api_refresh_pilot(request, character_id):
    """
    This view runs in the background to fetch and cache all
    ESI data (snapshot and SDE) for a character.
    """
    
    if request.method != 'POST': # Only allow POST requests
        return HttpResponseBadRequest("Invalid request method")

    esi = EsiClientProvider()
    character = get_object_or_404(EveCharacter, character_id=character_id, user=request.user)
    
    # 1. Get and refresh token
    token = get_refreshed_token_for_character(request.user, character)
    if not token:
        # User's token is invalid
        logout(request)
        return JsonResponse({"status": "error", "message": "Auth failed"}, status=401)
        
    # --- ALL SLOW ESI LOGIC IS NOW HERE ---
    try:
        # 2. Fetch fresh snapshot data from ESI
        skills_response = esi.client.Skills.get_characters_character_id_skills(
            character_id=character_id,
            token=token.access_token
        ).results()

        implants_response = esi.client.Clones.get_characters_character_id_implants(
            character_id=character_id,
            token=token.access_token
        ).results()
        
        # --- NEW: Get public data ---
        public_data = esi.client.Character.get_characters_character_id(
            character_id=character_id
        ).results()
        
        corp_id = public_data.get('corporation_id')
        alliance_id = public_data.get('alliance_id')
        
        corp_name = None
        if corp_id:
            corp_data = esi.client.Corporation.get_corporations_corporation_id(
                corporation_id=corp_id
            ).results()
            corp_name = corp_data.get('name')
            
        alliance_name = None
        if alliance_id:
            try:
                alliance_data = esi.client.Alliance.get_alliances_alliance_id(
                    alliance_id=alliance_id
                ).results()
                alliance_name = alliance_data.get('name')
            except HTTPNotFound:
                alliance_name = "N/A" # Handle dead alliances
        # --- END NEW ---

        if 'skills' not in skills_response or 'total_sp' not in skills_response:
            raise Exception(f"Invalid skills response: {skills_response}")
        if not isinstance(implants_response, list):
            raise Exception(f"Invalid implants response: {implants_response}")

        # 3. Save the fresh snapshot
        snapshot, created = PilotSnapshot.objects.get_or_create(character=character)
        snapshot.skills_json = json.dumps(skills_response)
        snapshot.implants_json = json.dumps(implants_response)
        snapshot.save() # This also updates 'last_updated'
        
        # --- NEW: Save corp/alliance data ---
        character.corporation_id = corp_id
        character.corporation_name = corp_name
        character.alliance_id = alliance_id
        character.alliance_name = alliance_name
        character.save()
        # --- END NEW ---
        
        # 4. Perform SDE Caching (the other slow part)
        
        # --- Cache Skills SDE ---
        skills_list = snapshot.get_skills()
        all_skill_ids = [s['skill_id'] for s in skills_list]
        cached_groups = {g.group_id: g for g in EveGroup.objects.all()}
        
        cached_type_ids = set(EveType.objects.filter(type_id__in=all_skill_ids).values_list('type_id', flat=True))
        missing_skill_ids = [sid for sid in all_skill_ids if sid not in cached_type_ids]

        for skill_id in missing_skill_ids:
            try:
                type_data = esi.client.Universe.get_universe_types_type_id(type_id=skill_id).results()
                group_id = type_data['group_id']
                group = cached_groups.get(group_id)
                
                if not group:
                    group_data = esi.client.Universe.get_universe_groups_group_id(group_id=group_id).results()
                    # --- FIX: Need to get category first ---
                    category_id = group_data.get('category_id')
                    category = None
                    if category_id:
                        try:
                            category = EveCategory.objects.get(category_id=category_id)
                        except EveCategory.DoesNotExist:
                            pass # Will be created if SDE importer ran
                    # --- END FIX ---
                    group = EveGroup.objects.create(
                        group_id=group_id, 
                        name=group_data['name'],
                        category_id=category_id
                    )
                    cached_groups[group.group_id] = group
                
                slot = None
                if 'dogma_attributes' in type_data:
                    for attr in type_data['dogma_attributes']:
                        # --- FIX: Dogma Attr for implant slot is 300 ---
                        if attr['attribute_id'] == 300: 
                            slot = int(attr['value']); 
                            break
                
                EveType.objects.create(type_id=skill_id, name=type_data['name'], group=group, slot=slot)
            except Exception:
                continue # Skip this one skill
        
        # --- Cache Implants SDE ---
        all_implant_ids = snapshot.get_implant_ids()
        cached_type_ids = set(EveType.objects.filter(type_id__in=all_implant_ids).values_list('type_id', flat=True))
        missing_implant_ids = [iid for iid in all_implant_ids if iid not in cached_type_ids]

        for implant_id in missing_implant_ids:
            try:
                type_data = esi.client.Universe.get_universe_types_type_id(type_id=implant_id).results()
                group_id = type_data['group_id']
                group = cached_groups.get(group_id)

                if not group:
                    group_data = esi.client.Universe.get_universe_groups_group_id(group_id=group_id).results()
                    # --- FIX: Need to get category first ---
                    category_id = group_data.get('category_id')
                    category = None
                    if category_id:
                        try:
                            category = EveCategory.objects.get(category_id=category_id)
                        except EveCategory.DoesNotExist:
                            pass # Will be created if SDE importer ran
                    # --- END FIX ---
                    group = EveGroup.objects.create(
                        group_id=group_id, 
                        name=group_data['name'],
                        category_id=category_id
                    )
                    cached_groups[group.group_id] = group
                
                slot = None
                if 'dogma_attributes' in type_data:
                    for attr in type_data['dogma_attributes']:
                         # --- FIX: Dogma Attr for implant slot is 300 ---
                        if attr['attribute_id'] == 300: 
                            slot = int(attr['value']); 
                            break
                        
                EveType.objects.create(type_id=implant_id, name=type_data['name'], group=group, slot=slot)
            except Exception:
                continue # Skip this one implant

        # 5. All done, send success
        return JsonResponse({"status": "success"})

    except Exception as e:
        # Something went wrong during the ESI calls
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


# --- NEW API VIEW FOR X-UP MODAL ---
@login_required
def api_get_implants(request):
    """
    Fetches and returns a character's implants as HTML
    for the X-Up modal.
    """
    character_id = request.GET.get('character_id')
    if not character_id:
        return HttpResponseBadRequest("Missing character_id")

    try:
        character = EveCharacter.objects.get(character_id=character_id, user=request.user)
    except EveCharacter.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Character not found or not yours."}, status=403)

    esi = EsiClientProvider()
    token = get_refreshed_token_for_character(request.user, character)
    if not token:
        logout(request)
        # --- THIS IS THE FIX: The text 'Check for correct scope' was mangled with the status code.
        return JsonResponse({"status": "error", "message": "Auth failed"}, status=401)
    
    # Check for correct scope
    if 'esi-clones.read_implants.v1' not in [s.name for s in token.scopes.all()]:
        return JsonResponse({"status": "error", "message": "Missing 'esi-clones.read_implants.v1' scope."}, status=403)

    try:
        # --- Make ESI call and get headers ---
        implants_op = esi.client.Clones.get_characters_character_id_implants(
            character_id=character_id,
            token=token.access_token
        )
        implants_response = implants_op.results()
        
        # --- Get Expiry header ---
        # ESI returns an 'Expires' header (e.g., 'Thu, 06 Nov 2025 10:12:13 GMT')
        # We also get 'Cache-Control' (e.g., 'max-age=120')
        # The 'X-Esi-Expires' header is easier to parse.
        expires_str = implants_op.header.get('Expires', [None])[0]
        expires_dt = None
        expires_iso = None
        if expires_str:
            try:
                # Parse the HTTP date string
                expires_dt = datetime.strptime(expires_str, '%a, %d %b %Y %H:%M:%S %Z').replace(tzinfo=timezone.utc)
                expires_iso = expires_dt.isoformat()
            except ValueError:
                expires_dt = timezone.now() + timedelta(minutes=2) # Fallback
                expires_iso = expires_dt.isoformat()
        else:
            expires_dt = timezone.now() + timedelta(minutes=2) # Fallback
            expires_iso = expires_dt.isoformat()

        if not isinstance(implants_response, list):
            raise Exception("Invalid implants response")

        # --- SDE & Grouping Logic (same as pilot_detail) ---
        all_implant_ids = implants_response # Response is just a list of IDs
        enriched_implants = []
        
        # ---
        # --- THE FIX IS HERE ---
        # ---
        # Wrap the SDE cache-fill in a try/except so it doesn't crash
        # the whole request if one ESI lookup fails.
        try:
            if all_implant_ids:
                # --- We MUST cache missing SDE data here ---
                cached_types = {t.type_id: t for t in EveType.objects.filter(type_id__in=all_implant_ids).select_related('group')}
                cached_groups = {g.group_id: g for g in EveGroup.objects.all()}
                
                missing_ids = [iid for iid in all_implant_ids if iid not in cached_types]
                for implant_id in missing_ids:
                    try:
                        type_data = esi.client.Universe.get_universe_types_type_id(type_id=implant_id).results()
                        group_id = type_data['group_id']
                        group = cached_groups.get(group_id)
                        if not group:
                            group_data = esi.client.Universe.get_universe_groups_group_id(group_id=group_id).results()
                            # --- FIX: Need to get category first ---
                            category_id = group_data.get('category_id')
                            category = None
                            if category_id:
                                try:
                                    category = EveCategory.objects.get(category_id=category_id)
                                except EveCategory.DoesNotExist:
                                    pass # Will be created if SDE importer ran
                            # --- END FIX ---
                            group = EveGroup.objects.create(
                                group_id=group_id, 
                                name=group_data['name'],
                                category_id=category_id
                            )
                            cached_groups[group.group_id] = group
                        
                        slot = None
                        if 'dogma_attributes' in type_data:
                            for attr in type_data['dogma_attributes']:
                                # --- FIX: Dogma Attr for implant slot is 300 ---
                                if attr['attribute_id'] == 300: 
                                    slot = int(attr['value']); 
                                    break
                        
                        new_type = EveType.objects.create(type_id=implant_id, name=type_data['name'], group=group, slot=slot)
                        cached_types[implant_id] = new_type
                    except Exception:
                        continue # Skip this implant
                # --- End SDE Cache ---

                for implant_id in all_implant_ids:
                    if implant_id in cached_types:
                        eve_type = cached_types[implant_id]
                        enriched_implants.append({
                            'name': eve_type.name,
                            'slot': eve_type.slot if eve_type.slot else 0,
                            'icon_url': f"https://images.evetech.net/types/{implant_id}/icon?size=32"
                        })
        except Exception as e:
            # Log the SDE error to the console but don't crash the request
            print(f"ERROR: Failed to cache SDE for implants in api_get_implants: {e}")
            # The 'enriched_implants' list will be empty, which is fine.
        # --- END FIX ---

        
        sorted_implants = sorted(enriched_implants, key=lambda i: i.get('slot', 0))
        
        implants_other = []
        implants_col1 = [] # Slots 1-5
        implants_col2 = [] # Slots 6-10
        for implant in sorted_implants:
            slot = implant.get('slot', 0)
            if 0 < slot <= 5:
                implants_col1.append(implant)
            elif 5 < slot <= 10:
                implants_col2.append(implant)
            else:
                implants_other.append(implant)
        
        context = {
            'implants_other': implants_other,
            'implants_col1': implants_col1,
            'implants_col2': implants_col2,
        }
        
        # --- MODIFICATION: Added a try/except around rendering ---
        try:
            # Render the partial template to HTML
            html = render_to_string('_implant_list.html', context)
        except Exception as e:
            # This will catch TemplateDoesNotExist or other rendering errors
            return JsonResponse({
                "status": "error", 
                "message": f"Template rendering failed: {str(e)}"
            }, status=500)
        # --- END MODIFICATION ---
        
        # Return the HTML and the expiry time
        return JsonResponse({
            "status": "success",
            "html": html,
            "expires_iso": expires_iso
        })

    except Exception as e:
        # This catches ESI errors, token errors, etc.
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


# ---
# --- NEW: API for setting main character
# ---
@login_required
@require_POST
def api_set_main_character(request):
    """
    Sets a new main character for the logged-in user.
    """
    character_id = request.POST.get('character_id')
    if not character_id:
        return JsonResponse({"status": "error", "message": "Missing character_id."}, status=400)
        
    try:
        with transaction.atomic():
            # 1. Get the character to set as main
            new_main = EveCharacter.objects.get(
                character_id=character_id,
                user=request.user # Ensure it belongs to this user
            )
            
            # 2. Unset all other mains for this user
            request.user.eve_characters.exclude(
                character_id=character_id
            ).update(is_main=False)
            
            # 3. Set the new main
            new_main.is_main = True
            new_main.save()
            
            return JsonResponse({"status": "success", "message": f"{new_main.character_name} is now your main character."})

    except EveCharacter.DoesNotExist:
        return JsonResponse({"status": "error", "message": "Character not found or does not belong to you."}, status=404)
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)
# ---
# --- END NEW API
# ---