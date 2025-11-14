import logging
import json
import os
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required, user_passes_test
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest
from bravado.exception import HTTPNotFound
from esi.clients import EsiClientProvider
from django_eventstream import send_event

from django.db.models import Q

from .models import (
    EveCharacter, ShipFit, Fleet, FleetWaitlist,
    FleetWing, FleetSquad
)

from pilot.models import EveType, EveGroup
from .helpers import is_fleet_commander, get_refreshed_token_for_character, _update_fleet_structure

logger = logging.getLogger(__name__)


# --- FC ADMIN VIEWS ---
@login_required
@user_passes_test(is_fleet_commander)
def fc_admin_view(request):
    """
    Displays the FC admin page for opening/closing waitlists.
    """
    logger.debug(f"FC {request.user.username} accessing fc_admin_view")
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).select_related('fleet', 'fleet__fleet_commander').first()
    
    user_fc_characters = EveCharacter.objects.filter(user=request.user)
    
    available_fleets = Fleet.objects.filter(is_active=False).order_by('description')

    all_user_chars = request.user.eve_characters.all().order_by('character_name')
    main_char = all_user_chars.filter(is_main=True).first()
    if not main_char:
        main_char = all_user_chars.first()

    context = {
        'open_waitlist': open_waitlist,
        'user_fc_characters': user_fc_characters,
        'available_fleets': available_fleets,
        'is_fc': True,
        'user_characters': all_user_chars,
        'all_chars_for_header': all_user_chars,
        'main_char_for_header': main_char,
    }
    return render(request, 'fc_admin.html', context)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_manage_waitlist(request):
    """
    API endpoint for FC actions (open, close, takeover).
    """
    action = request.POST.get('action')
    logger.info(f"FC {request.user.username} performing manage_waitlist action: '{action}'")
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()

    if action == 'close':
        if not open_waitlist:
            logger.warning(f"FC {request.user.username} tried to close waitlist, but none is open")
            return JsonResponse({"status": "error", "message": "Waitlist is already closed."}, status=400)
        
        try:
            fleet = open_waitlist.fleet
            logger.info(f"Closing waitlist for fleet {fleet.description} (ID: {fleet.id})")
            fleet.is_active = False
            fleet.fleet_commander = None
            fleet.esi_fleet_id = None
            fleet.save()
            
            open_waitlist.is_open = False
            open_waitlist.save()
            
            FleetWing.objects.filter(fleet=fleet).delete()
            
            pending_fits = ShipFit.objects.filter(
                waitlist=open_waitlist,
                status='PENDING'
            )
            count = pending_fits.update(status='DENIED', denial_reason="Waitlist closed before approval.")
            logger.info(f"Denied {count} pending fits.")
            
            logger.debug("Sending 'waitlist-updates' event")
            send_event('waitlist-updates', 'update', {
                'action': 'close'
            })
            
            return JsonResponse({"status": "success", "message": "Waitlist closed. All pending fits denied."})
        except Exception as e:
            logger.error(f"Error closing waitlist: {e}", exc_info=True)
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)

    elif action == 'open':
        if open_waitlist:
            logger.warning(f"FC {request.user.username} tried to open waitlist, but one is already open")
            return JsonResponse({"status": "error", "message": "A waitlist is already open. Please close it first."}, status=400)

        fleet_id = request.POST.get('fleet_id')
        fleet_commander_id = request.POST.get('fleet_commander_id')

        if not all([fleet_id, fleet_commander_id]):
            logger.warning(f"FC {request.user.username} tried to open waitlist with missing data")
            return JsonResponse({"status": "error", "message": "Fleet Type and FC Character are required."}, status=400)
            
        try:
            fc_character = EveCharacter.objects.get(
                character_id=fleet_commander_id, 
                user=request.user
            )
            fleet_to_open = Fleet.objects.get(id=fleet_id, is_active=False)

            fleet_to_open.fleet_commander = fc_character
            fleet_to_open.is_active = True
            fleet_to_open.save()
            
            waitlist, created = FleetWaitlist.objects.get_or_create(fleet=fleet_to_open)
            waitlist.is_open = True
            waitlist.save()
            
            logger.debug("Sending 'waitlist-updates' event")
            send_event('waitlist-updates', 'update', {
                'action': 'open'
            })
            
            logger.info(f"Waitlist '{fleet_to_open.description}' opened by FC {fc_character.character_name}")
            return JsonResponse({"status": "success", "message": f"Waitlist '{fleet_to_open.description}' opened. Please link your in-game fleet."})
            
        except EveCharacter.DoesNotExist:
            logger.warning(f"FC {request.user.username} tried to open waitlist with invalid char_id {fleet_commander_id}")
            return JsonResponse({"status": "error", "message": "Invalid FC character selected."}, status=403)
        except Fleet.DoesNotExist:
            logger.warning(f"FC {request.user.username} tried to open fleet {fleet_id} which is active or non-existent")
            return JsonResponse({"status": "error", "message": "The fleet you selected is already open or does not exist."}, status=400)
        except Exception as e:
            logger.error(f"Error opening waitlist: {e}", exc_info=True)
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)

    elif action == 'takeover':
        if not open_waitlist:
            logger.warning(f"FC {request.user.username} tried to link fleet, but no waitlist is open")
            return JsonResponse({"status": "error", "message": "No waitlist is currently open to link a fleet to."}, status=400)
            
        fleet_commander_id = request.POST.get('fleet_commander_id')
        if not fleet_commander_id:
            logger.warning(f"FC {request.user.username} tried to link fleet with no FC char selected")
            return JsonResponse({"status": "error", "message": "FC Character is required."}, status=400)
            
        try:
            fc_character = EveCharacter.objects.get(
                character_id=fleet_commander_id, 
                user=request.user
            )
            logger.debug(f"FC {fc_character.character_name} attempting to link fleet")
            token = get_refreshed_token_for_character(request.user, fc_character)

            required_scopes = [
                'esi-fleets.read_fleet.v1',
                'esi-fleets.write_fleet.v1'
            ]
            available_scopes = set(s.name for s in token.scopes.all())
            
            if not all(s in available_scopes for s in required_scopes):
                missing = [s for s in required_scopes if s not in available_scopes]
                logger.warning(f"FC {fc_character.character_name} link failed: Missing scopes: {missing}")
                return JsonResponse({
                    "status": "error", 
                    "message": f"Missing required FC scopes: {', '.join(missing)}. Please log in again using the 'Add FC Scopes' option."
                }, status=403)

            esi = EsiClientProvider()
            new_esi_fleet_id = None
            
            try:
                logger.debug(f"Getting ESI fleet info for {fc_character.character_name}")
                fleet_info = esi.client.Fleets.get_characters_character_id_fleet(
                    character_id=fc_character.character_id,
                    token=token.access_token
                ).results()
                
                if fleet_info.get('role') != 'fleet_commander':
                    logger.warning(f"FC {fc_character.character_name} link failed: Not fleet boss (Role: {fleet_info.get('role')})")
                    return JsonResponse({"status": "error", "message": "You are not the Fleet Commander (Boss) of your current fleet."}, status=403)

                new_esi_fleet_id = fleet_info.get('fleet_id')
                logger.debug(f"Got ESI fleet ID: {new_esi_fleet_id}")

            except HTTPNotFound as e:
                logger.warning(f"FC {fc_character.character_name} link failed: Not in a fleet (404)")
                return JsonResponse({"status": "error", "message": "You are not in a fleet. Please create one in-game first, then link it."}, status=400)
            
            if not new_esi_fleet_id:
                logger.error(f"FC {fc_character.character_name} link failed: ESI returned no fleet ID")
                return JsonResponse({"status": "error", "message": "Could not fetch new Fleet ID from ESI."}, status=500)

            fleet = open_waitlist.fleet
            fleet.fleet_commander = fc_character
            fleet.esi_fleet_id = new_esi_fleet_id
            fleet.save()
            
            logger.debug(f"Pulling fleet structure for {new_esi_fleet_id}")
            _update_fleet_structure(esi, fc_character, token, new_esi_fleet_id, fleet)
            
            logger.info(f"Fleet {fleet.id} successfully linked to ESI fleet {new_esi_fleet_id} by {fc_character.character_name}")
            return JsonResponse({
                "status": "success", 
                "message": f"Waitlist successfully linked to fleet {new_esi_fleet_id} and structure updated.",
                "esi_fleet_id": new_esi_fleet_id
            })
            
        except EveCharacter.DoesNotExist:
            logger.warning(f"FC {request.user.username} link failed: Invalid char_id {fleet_commander_id}")
            return JsonResponse({"status": "error", "message": "Invalid FC character selected."}, status=403)
        except Exception as e:
            logger.error(f"Error linking fleet: {e}", exc_info=True)
            return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)

    logger.error(f"FC {request.user.username} sent invalid action: '{action}'")
    return JsonResponse({"status": "error", "message": "Invalid action."}, status=400)


@login_required
@user_passes_test(is_fleet_commander)
def api_get_fleet_structure(request):
    """
    Returns the current fleet's wing/squad structure
    from the database.
    """
    logger.debug(f"FC {request.user.username} getting fleet structure")
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        logger.debug("api_get_fleet_structure: No waitlist open")
        return JsonResponse({"status": "error", "message": "No waitlist is open."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id:
        logger.debug(f"api_get_fleet_structure: Fleet {fleet.id} not linked to ESI")
        return JsonResponse({"status": "error", "message": "Fleet is not linked to ESI."}, status=400)

    wings = FleetWing.objects.filter(fleet=fleet).prefetch_related('squads')
    
    available_categories = [
        {"id": choice[0], "name": choice[1]}
        for choice in ShipFit.FitCategory.choices
        if choice[0] != 'NONE'
    ]

    structure = {
        "wings": [],
        "available_categories": available_categories
    }
    
    for wing in wings:
        wing_data = {
            "id": wing.wing_id,
            "name": wing.name,
            "squads": []
        }
        
        for squad in wing.squads.order_by('squad_id'):
            wing_data["squads"].append({
                "id": squad.squad_id,
                "name": squad.name,
                "assigned_category": squad.assigned_category
            })
        structure["wings"].append(wing_data)

    logger.debug(f"Returning {len(structure['wings'])} wings for fleet {fleet.id}")
    return JsonResponse({"status": "success", "structure": structure})


@login_required
@user_passes_test(is_fleet_commander)
def api_get_fleet_members(request):
    """
    Gets the current fleet members from ESI and returns a
    structured list with ship types and counts.
    """
    logger.debug(f"FC {request.user.username} getting fleet members overview")
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        logger.debug("api_get_fleet_members: No waitlist open")
        return JsonResponse({"status": "error", "message": "No waitlist is open."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        logger.debug(f"api_get_fleet_members: Fleet {fleet.id} not linked")
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        fleet_id = fleet.esi_fleet_id
        
        logger.debug(f"Getting ESI fleet members for {fleet_id}")
        esi_members = esi.client.Fleets.get_fleets_fleet_id_members(
            fleet_id=fleet_id,
            token=token.access_token
        ).results()
        
        total_member_count = len(esi_members)
        
        wings_from_db = FleetWing.objects.filter(fleet=fleet).prefetch_related('squads').order_by('wing_id')
        
        processed_wings = {}
        for wing in wings_from_db:
            wing_id = wing.wing_id
            processed_wings[wing_id] = {
                "id": wing_id,
                "name": wing.name, 
                "member_count": 0,
                "wing_commander": None,
                "squads": {}
            }
            for squad in wing.squads.all().order_by('squad_id'):
                squad_id = squad.squad_id
                processed_wings[wing_id]["squads"][squad_id] = {
                    "id": squad_id,
                    "name": squad.name, 
                    "member_count": 0,
                    "squad_commander": None,
                    "members": []
                }
        
        all_character_ids = list(set(m['character_id'] for m in esi_members))
        all_ship_type_ids = list(set(m['ship_type_id'] for m in esi_members))
        
        char_names_map = {
            c.character_id: c.character_name 
            for c in EveCharacter.objects.filter(character_id__in=all_character_ids)
        }
        ship_types_map = {
            t.type_id: t 
            for t in EveType.objects.filter(type_id__in=all_ship_type_ids).select_related('group')
        }
        
        cached_char_ids = set(char_names_map.keys())
        missing_char_ids = [cid for cid in all_character_ids if cid not in cached_char_ids]
        
        if missing_char_ids:
            logger.debug(f"Resolving {len(missing_char_ids)} unknown character names from ESI")
            try:
                names_response = esi.client.Universe.post_universe_names(
                    ids=missing_char_ids
                ).results()
                
                for item in names_response:
                    if item['category'] == 'character':
                        char_names_map[item['id']] = item['name']
            except Exception as e:
                logger.warning(f"Failed to resolve {len(missing_char_ids)} character names from ESI: {e}")
        
        category_keys_str = os.environ.get("FLEET_OVERVIEW_CATEGORIES", "")
        category_names_str = os.environ.get("FLEET_OVERVIEW_CATEGORY_NAMES", "")
        
        category_keys = [key.strip().upper() for key in category_keys_str.split(',') if key.strip()]
        category_names = [name.strip() for name in category_names_str.split(',') if name.strip()]
        
        if len(category_keys) != len(category_names):
            logger.error("FLEET_OVERVIEW_CATEGORIES and FLEET_OVERVIEW_CATEGORY_NAMES have a different number of items!")
            category_names = [key.title() for key in category_keys]
            
        categories_to_load = list(zip(category_keys, category_names))
        
        SHIP_NAMES_TO_COUNT = {}
        
        for key_upper, display_name in categories_to_load:
            ship_list_str = os.environ.get(f"FLEET_OVERVIEW_{key_upper}", "")
            ship_names = [name.strip() for name in ship_list_str.split(',') if name.strip()]
            
            if ship_names:
                SHIP_NAMES_TO_COUNT[key_upper.lower()] = ship_names
        
        all_ship_names_to_find = [name for names_list in SHIP_NAMES_TO_COUNT.values() for name in names_list]

        ship_types_from_db = EveType.objects.filter(name__in=all_ship_names_to_find)
        
        name_to_type_map = {t.name: t for t in ship_types_from_db}
        
        detailed_ship_counts = {}
        type_id_to_category_map = {}

        for key_lower, ship_names in SHIP_NAMES_TO_COUNT.items():
            detailed_ship_counts[key_lower] = []
            for name in ship_names:
                ship_type = name_to_type_map.get(name)
                if ship_type:
                    detailed_ship_counts[key_lower].append({
                        "type_id": ship_type.type_id,
                        "name": ship_type.name,
                        "count": 0
                    })
                    type_id_to_category_map[ship_type.type_id] = key_lower
                else:
                    logger.warning(f"Fleet overview: Ship name '{name}' not found in local EveType SDE.")

        fleet_commander_data = None
        
        for member in esi_members:
            char_id = member['character_id']
            ship_type_id = member['ship_type_id']
            wing_id = member['wing_id']
            squad_id = member['squad_id']
            role = member['role']
            
            char_name = char_names_map.get(char_id, f"Unknown Char {char_id}")
            ship_type = ship_types_map.get(ship_type_id)
            
            ship_name = "Unknown Ship"
            if ship_type:
                ship_name = ship_type.name

            if ship_type_id in type_id_to_category_map:
                category = type_id_to_category_map[ship_type_id]
                for ship_dict in detailed_ship_counts[category]:
                    if ship_dict["type_id"] == ship_type_id:
                        ship_dict["count"] += 1
                        break

            member_data = {
                "character_id": char_id,
                "character_name": char_name,
                "ship_type_id": ship_type_id,
                "ship_name": ship_name,
                "role": role
            }

            if role == 'fleet_commander':
                fleet_commander_data = member_data
                continue 

            if wing_id in processed_wings:
                processed_wings[wing_id]["member_count"] += 1
                
                if role == 'wing_commander':
                    processed_wings[wing_id]["wing_commander"] = member_data
                    continue 

                if squad_id in processed_wings[wing_id]["squads"]:
                    processed_wings[wing_id]["squads"][squad_id]["member_count"] += 1

                    if role == 'squad_commander':
                        processed_wings[wing_id]["squads"][squad_id]["squad_commander"] = member_data
                    else: 
                        processed_wings[wing_id]["squads"][squad_id]["members"].append(member_data)

        final_wings_list = []
        for wing_id in sorted(processed_wings.keys()):
            wing_data = processed_wings[wing_id]
            wing_data['squads'] = [squad_data for squad_id, squad_data in sorted(wing_data['squads'].items())]
            final_wings_list.append(wing_data)

        
        always_show_names_str = os.environ.get("FLEET_OVERVIEW_ALWAYS_SHOW", "")
        always_show_names = set([name.strip() for name in always_show_names_str.split(',') if name.strip()])
        logger.debug(f"Always showing ships: {always_show_names}")

        final_detailed_ship_counts_list = []
        
        for key_upper, display_name in categories_to_load:
            key_lower = key_upper.lower()
            
            if key_lower in detailed_ship_counts:
                ships_list = detailed_ship_counts[key_lower]
                
                ships_to_show = [
                    ship for ship in ships_list 
                    if ship["count"] > 0 or ship["name"] in always_show_names
                ]
                
                if ships_to_show:
                    final_detailed_ship_counts_list.append({
                        "key": key_lower,
                        "name": display_name,
                        "ships": ships_to_show
                    })

        logger.debug(f"Returning fleet overview: {final_detailed_ship_counts_list}")
        return JsonResponse({
            "status": "success",
            "detailed_ship_counts": final_detailed_ship_counts_list,
            "wings": final_wings_list,
            "fleet_commander": fleet_commander_data,
            "total_member_count": total_member_count,
            "fleet_boss_name": fleet.fleet_commander.character_name 
        })

    except HTTPNotFound:
        logger.warning(f"api_get_fleet_members: ESI fleet {fleet_id} not found.")
        return JsonResponse({"status": "error", "message": "ESI fleet not found. It may have been closed in-game."}, status=404)
    except Exception as e:
        logger.error(f"Error getting fleet members: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_save_squad_mappings(request):
    """
    Saves the category-to-squad mappings AND new names.
    This now pushes name changes to ESI.
    """
    logger.info(f"FC {request.user.username} saving squad mappings")
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        logger.warning(f"api_save_squad_mappings: No waitlist open")
        return JsonResponse({"status": "error", "message": "No waitlist is open."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        logger.warning(f"api_save_squad_mappings: Fleet {fleet.id} not linked")
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        data = json.loads(request.body)
        wing_data = data.get('wings', [])
        squad_data = data.get('squads', [])
        logger.debug(f"Received {len(wing_data)} wings and {len(squad_data)} squads to update")
        
        all_db_wings = {w.wing_id: w for w in fleet.wings.all()}
        all_db_squads = {s.squad_id: s for s in FleetSquad.objects.filter(wing__fleet=fleet)}
        
        FleetSquad.objects.filter(wing__fleet=fleet).update(assigned_category=None)
        
        for wing_info in wing_data:
            wing_id = int(wing_info['id'])
            new_name = wing_info['name']
            
            db_wing = all_db_wings.get(wing_id)
            if db_wing and db_wing.name != new_name:
                logger.debug(f"Renaming wing {wing_id} to '{new_name}' in ESI")
                esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
                    fleet_id=fleet.esi_fleet_id,
                    wing_id=wing_id,
                    naming={'name': new_name},
                    token=token.access_token
                ).results()
                db_wing.name = new_name
                db_wing.save()

        for squad_info in squad_data:
            squad_id = int(squad_info['id'])
            new_name = squad_info['name']
            new_category = squad_info['category']
            
            db_squad = all_db_squads.get(squad_id)
            if db_squad:
                if db_squad.name != new_name:
                    logger.debug(f"Renaming squad {squad_id} to '{new_name}' in ESI")
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet.esi_fleet_id,
                        squad_id=squad_id,
                        naming={'name': new_name},
                        token=token.access_token
                    ).results()
                
                db_squad.name = new_name
                db_squad.assigned_category = new_category
                db_squad.save()
        
        logger.debug("Refreshing fleet structure from ESI after save")
        _update_fleet_structure(
            esi, fc_character, token, 
            fleet.esi_fleet_id, fleet
        )
        
        wings = FleetWing.objects.filter(fleet=fleet).prefetch_related('squads')
        available_categories = [
            {"id": choice[0], "name": choice[1]}
            for choice in ShipFit.FitCategory.choices
            if choice[0] != 'NONE'
        ]
        structure = { "wings": [], "available_categories": available_categories }
        
        for wing in wings:
            squads_list = wing.squads.order_by('squad_id')
            wing_data = {
                "id": wing.wing_id,
                "name": wing.name,
                "squads": [{
                    "id": squad.squad_id,
                    "name": squad.name,
                    "assigned_category": squad.assigned_category
                } for squad in squads_list]
            }
            structure["wings"].append(wing_data)

        logger.info(f"Squad mappings saved successfully by {request.user.username}")
        return JsonResponse({"status": "success", "structure": structure})
        
    except json.JSONDecodeError:
        logger.warning(f"api_save_squad_mappings: Invalid JSON received from {request.user.username}")
        return JsonResponse({"status": "error", "message": "Invalid request data."}, status=400)
    except Exception as e:
        logger.error(f"Error saving squad mappings: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_invite_pilot(request):
    """
    Invites a pilot to the fleet, placing them in the
    correct squad if one is mapped.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    fit_id = request.POST.get('fit_id')
    logger.debug(f"FC {request.user.username} inviting fit {fit_id}")
    
    try:
        fit = ShipFit.objects.get(id=fit_id, waitlist=open_waitlist, status='APPROVED')
        pilot_to_invite = fit.character
        
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)

        role = "squad_member"
        wing_id = None
        squad_id = None

        if fit.category != ShipFit.FitCategory.NONE:
            try:
                mapped_squad = FleetSquad.objects.get(
                    wing__fleet=fleet,
                    assigned_category=fit.category
                )
                role = "squad_commander" if mapped_squad.name.lower().startswith("scout") else "squad_member"
                wing_id = mapped_squad.wing.wing_id
                squad_id = mapped_squad.squad_id
                logger.debug(f"Found mapped squad {squad_id} for category {fit.category}")
                
            except FleetSquad.DoesNotExist:
                logger.debug(f"No squad mapped for {fit.category}, finding fallback")
                on_grid_wing = fleet.wings.filter(name="On Grid").first()
                if on_grid_wing:
                    first_squad = on_grid_wing.squads.order_by('squad_id').first()
                    if first_squad:
                        wing_id = first_squad.wing.wing_id
                        squad_id = first_squad.squad_id
                        logger.debug(f"Using 'On Grid' fallback squad {squad_id}")
                
                if not squad_id:
                    first_wing = fleet.wings.order_by('wing_id').first()
                    if first_wing:
                        first_squad = first_wing.squads.order_by('squad_id').first()
                        if first_squad:
                            wing_id = first_wing.wing_id
                            squad_id = first_squad.squad_id
                            logger.debug(f"Using absolute first squad {squad_id}")

        if not wing_id or not squad_id:
            role = "fleet_commander"
            logger.warning(f"No squads found for fleet {fleet.id}, defaulting role to fleet_commander")
        
        invitation = {
            "character_id": pilot_to_invite.character_id,
            "role": role
        }
        if wing_id:
            invitation["wing_id"] = wing_id
        if squad_id:
            invitation["squad_id"] = squad_id
        
        logger.debug(f"Sending ESI invite to {pilot_to_invite.character_name}: {invitation}")
        esi = EsiClientProvider()
        esi.client.Fleets.post_fleets_fleet_id_members(
            fleet_id=fleet.esi_fleet_id,
            invitation=invitation,
            token=token.access_token
        ).results()

        fit.status = ShipFit.FitStatus.IN_FLEET
        fit.save()
        
        logger.debug("Sending 'waitlist-updates' event")
        send_event('waitlist-updates', 'update', {
            'fit_id': fit.id,
            'action': 'invite'
        })
        
        logger.info(f"Invite sent to {pilot_to_invite.character_name} by {fc_character.character_name}")
        return JsonResponse({"status": "success", "message": "Invite sent."})

    except ShipFit.DoesNotExist:
        logger.warning(f"FC {request.user.username} tried to invite non-existent/unapproved fit {fit_id}")
        return JsonResponse({"status": "error", "message": "Fit not found or not approved."}, status=404)
    except Exception as e:
        logger.error(f"Error inviting pilot for fit {fit_id}: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"ESI Error: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_create_default_layout(request):
    """
    Applies a hard-coded default squad layout to the FC's
    current in-game fleet.
    """
    logger.info(f"FC {request.user.username} creating default fleet layout")
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        logger.warning("api_fc_create_default_layout: No waitlist open")
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        logger.warning(f"api_fc_create_default_layout: Fleet {fleet.id} not linked")
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    try:
        DEFAULT_LAYOUT = [
            {
                "name": "On Grid",
                "squads": [
                    {"name": "Logi", "category": "LOGI"},
                    {"name": "DPS", "category": "DPS"},
                    {"name": "Sniper", "category": "SNIPER"},
                    {"name": "Other", "category": "OTHER"},
                    {"name": "Mar DPS", "category": "MAR_DPS"},
                    {"name": "Mar Sniper", "category": "MAR_SNIPER"},
                    {"name": "Boxer 1", "category": None},
                    {"name": "Boxer 2", "category": None},
                    {"name": "Boxer 3", "category": None},
                    {"name": "Boxer 4", "category": None},
                ]
            },
            {
                "name": "Off Grid",
                "squads": [
                    {"name": "Scout 1", "category": None},
                    {"name": "Scout 2", "category": None},
                    {"name": "Scout 3", "category": None},
                    {"name": "Sponge 1", "category": None},
                    {"name": "Sponge 2", "category": None},
                    {"name": "Sponge 3", "category": None},
                ]
            }
        ]
        
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        fleet_id = fleet.esi_fleet_id
        
        try:
            logger.debug(f"Checking FC position for {fc_character.character_name}")
            fleet_info = esi.client.Fleets.get_characters_character_id_fleet(
                character_id=fc_character.character_id,
                token=token.access_token
            ).results()
            
            if fleet_info.get('role') != 'fleet_commander':
                logger.warning(f"Default layout failed: FC {fc_character.character_name} is in a squad")
                return JsonResponse({
                    "status": "error", 
                    "message": "You are in a squad. Please move yourself to the 'Fleet Command' position before creating the layout."
                }, status=400)
        except HTTPNotFound:
             logger.warning(f"Default layout failed: FC {fc_character.character_name} not in fleet")
             return JsonResponse({"status": "error", "message": "You are not in the fleet. Please link the fleet first."}, status=400)

        logger.debug(f"Getting current ESI structure for fleet {fleet_id}")
        current_wings = esi.client.Fleets.get_fleets_fleet_id_wings(
            fleet_id=fleet_id,
            token=token.access_token
        ).results()
        
        FleetWing.objects.filter(fleet=fleet).delete()
        logger.debug("Cleared local DB structure")

        wing_index = 0
        for wing_def in DEFAULT_LAYOUT:
            squad_index = 0
            wing_name = wing_def['name']
            
            esi_wing = current_wings[wing_index] if wing_index < len(current_wings) else None
            wing_id = None
            
            if esi_wing:
                wing_id = esi_wing['id']
                logger.debug(f"Reusing and renaming wing {wing_id} to '{wing_name}'")
                esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
                    fleet_id=fleet_id,
                    wing_id=wing_id,
                    naming={'name': wing_name},
                    token=token.access_token
                ).results()
            else:
                logger.debug(f"Creating new wing, renaming to '{wing_name}'")
                new_wing_op = esi.client.Fleets.post_fleets_fleet_id_wings(
                    fleet_id=fleet_id,
                    token=token.access_token
                ).results()
                wing_id = new_wing_op['wing_id']
                esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
                    fleet_id=fleet_id,
                    wing_id=wing_id,
                    naming={'name': wing_name},
                    token=token.access_token
                ).results()
            
            db_wing = FleetWing.objects.create(
                fleet=fleet, wing_id=wing_id, name=wing_name
            )
            
            existing_squads = sorted(esi_wing['squads'], key=lambda s: s['id']) if esi_wing else []

            for squad_def in wing_def['squads']:
                squad_name = squad_def['name']
                category = squad_def['category']
                squad_id = None
                
                esi_squad = existing_squads[squad_index] if squad_index < len(existing_squads) else None
                
                if esi_squad:
                    squad_id = esi_squad['id']
                    logger.debug(f"  Reusing squad {squad_id}, renaming to '{squad_name}'")
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet_id,
                        squad_id=squad_id,
                        naming={'name': squad_name},
                        token=token.access_token
                    ).results()
                else:
                    logger.debug(f"  Creating new squad in wing {wing_id}, renaming to '{squad_name}'")
                    new_squad = esi.client.Fleets.post_fleets_fleet_id_wings_wing_id_squads(
                        fleet_id=fleet_id,
                        wing_id=wing_id,
                        token=token.access_token
                    ).results()
                    squad_id = new_squad['squad_id']
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet_id,
                        squad_id=squad_id,
                        naming={'name': squad_name},
                        token=token.access_token
                    ).results()

                FleetSquad.objects.create(
                    wing=db_wing,
                    squad_id=squad_id,
                    name=squad_name,
                    assigned_category=category
                )
                
                squad_index += 1
            
            if squad_index < len(existing_squads):
                for i in range(squad_index, len(existing_squads)):
                    esi_squad = existing_squads[i]
                    squad_id = esi_squad['id']
                    squad_name = f"Squad {i + 1}"
                    logger.debug(f"  Cleaning up leftover squad {squad_id}, renaming to '{squad_name}'")
                    
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet_id,
                        squad_id=squad_id,
                        naming={'name': squad_name},
                        token=token.access_token
                    ).results()

                    FleetSquad.objects.create(
                        wing=db_wing,
                        squad_id=squad_id,
                        name=squad_name,
                        assigned_category=None
                    )
            
            wing_index += 1
        
        if wing_index < len(current_wings):
            for i in range(wing_index, len(current_wings)):
                esi_wing = current_wings[i]
                wing_id = esi_wing['id']
                wing_name = f"Wing {i + 1}"
                logger.debug(f"Cleaning up leftover wing {wing_id}, renaming to '{wing_name}'")
                
                esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
                    fleet_id=fleet_id,
                    wing_id=wing_id,
                    naming={'name': wing_name},
                    token=token.access_token
                ).results()
                
                db_wing = FleetWing.objects.create(
                    fleet=fleet, wing_id=wing_id, name=wing_name
                )
                
                squad_index = 0
                squads_to_clean = sorted(esi_wing['squads'], key=lambda s: s['id'])
                for esi_squad in squads_to_clean:
                    squad_id = esi_squad['id']
                    squad_name = f"Squad {squad_index + 1}"
                    logger.debug(f"  Cleaning up leftover squad {squad_id} in wing {wing_id}, renaming to '{squad_name}'")
                    
                    esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
                        fleet_id=fleet_id,
                        squad_id=squad_id,
                        naming={'name': squad_name},
                        token=token.access_token
                    ).results()

                    FleetSquad.objects.create(
                        wing=db_wing,
                        squad_id=squad_id,
                        name=squad_name,
                        assigned_category=None
                    )
                    squad_index += 1

        logger.info(f"Default fleet layout created successfully for fleet {fleet_id} by {fc_character.character_name}")
        return JsonResponse({"status": "success", "message": "Fleet layout successfully merged and mappings saved."})

    except Exception as e:
        logger.error(f"Error creating default layout: {e}", exc_info=True)
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_refresh_structure(request):
    """
    Pulls the current fleet structure from ESI,
    updates the database, and returns the new structure.
    """
    logger.debug(f"FC {request.user.username} refreshing fleet structure")
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        logger.warning("api_fc_refresh_structure: No waitlist open")
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        logger.warning(f"api_fc_refresh_structure: Fleet {fleet.id} not linked")
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        _update_fleet_structure(
            esi, fc_character, token, 
            fleet.esi_fleet_id, fleet
        )
        
        wings = FleetWing.objects.filter(fleet=fleet).prefetch_related('squads')
        available_categories = [
            {"id": choice[0], "name": choice[1]}
            for choice in ShipFit.FitCategory.choices
            if choice[0] != 'NONE'
        ]
        structure = {
            "wings": [],
            "available_categories": available_categories
        }
        
        for wing in wings:
            wing_data = {
                "id": wing.wing_id,
                "name": wing.name,
                "squads": []
            }
            for squad in wing.squads.order_by('squad_id'):
                wing_data["squads"].append({
                    "id": squad.squad_id,
                    "name": squad.name,
                    "assigned_category": squad.assigned_category
                })
            structure["wings"].append(wing_data)

        logger.info(f"Fleet structure refreshed for {fleet.id} by {fc_character.character_name}")
        return JsonResponse({"status": "success", "structure": structure})

    except HTTPNotFound as e:
        logger.warning(f"HTTPNotFound while refreshing fleet structure for fleet {fleet.id} (ESI ID: {fleet.esi_fleet_id}). ESI fleet is likely dead. Closing waitlist.")
        
        try:
            fleet = open_waitlist.fleet
            fleet.is_active = False
            fleet.fleet_commander = None
            fleet.esi_fleet_id = None
            fleet.save()
            
            open_waitlist.is_open = False
            open_waitlist.save()
            
            FleetWing.objects.filter(fleet=fleet).delete()
            
            pending_fits = ShipFit.objects.filter(
                waitlist=open_waitlist,
                status='PENDING'
            )
            count = pending_fits.update(status='DENIED', denial_reason="Fleet closed (ESI fleet not found).")
            logger.info(f"Closed waitlist {open_waitlist.id} and denied {count} pending fits.")

            logger.debug("Sending 'waitlist-updates' event")
            send_event('waitlist-updates', 'update', {
                'action': 'close'
            })

            return JsonResponse({
                "status": "error",
                "message": "ESI fleet not found! It may have been closed in-game. The waitlist has been automatically closed."
            }, status=404)
            
        except Exception as close_e:
            logger.error(f"Error during automatic waitlist close after HTTPNotFound: {close_e}", exc_info=True)
            return JsonResponse({"status":"error", "message": f"ESI fleet not found, and an error occurred during auto-close: {close_e}"}, status=500)
    except Exception as e:
        logger.error(f"Error refreshing fleet structure: {e}", exc_info=True)
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_add_squad(request):
    """
    Adds a new squad to a wing in-game.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    wing_id = request.POST.get('wing_id')
    logger.info(f"FC {request.user.username} adding squad to wing {wing_id}")
    if not wing_id:
        logger.warning("api_fc_add_squad: Missing wing_id")
        return JsonResponse({"status": "error", "message": "Missing wing_id."}, status=400)
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        new_squad = esi.client.Fleets.post_fleets_fleet_id_wings_wing_id_squads(
            fleet_id=fleet.esi_fleet_id,
            wing_id=wing_id,
            token=token.access_token
        ).results()
        
        squad_id = new_squad['squad_id']
        logger.debug(f"Created new squad {squad_id} in ESI, renaming")
        
        esi.client.Fleets.put_fleets_fleet_id_squads_squad_id(
            fleet_id=fleet.esi_fleet_id,
            squad_id=squad_id,
            naming={'name': "New Squad"},
            token=token.access_token
        ).results()
        
        logger.info(f"Squad {squad_id} added to wing {wing_id} by {fc_character.character_name}")
        return JsonResponse({"status": "success", "message": "New squad added."})

    except Exception as e:
        logger.error(f"Error adding squad to wing {wing_id}: {e}", exc_info=True)
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_delete_squad(request):
    """
    Deletes a squad from a wing in-game.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    squad_id = request.POST.get('squad_id')
    logger.info(f"FC {request.user.username} deleting squad {squad_id}")
    if not squad_id:
        logger.warning("api_fc_delete_squad: Missing squad_id")
        return JsonResponse({"status": "error", "message": "Missing squad_id."}, status=400)
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        esi.client.Fleets.delete_fleets_fleet_id_squads_squad_id(
            fleet_id=fleet.esi_fleet_id,
            squad_id=squad_id,
            token=token.access_token
        ).results()
        
        logger.info(f"Squad {squad_id} deleted by {fc_character.character_name}")
        return JsonResponse({"status": "success", "message": "Squad deleted."})

    except Exception as e:
        logger.error(f"Error deleting squad {squad_id}: {e}", exc_info=True)
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_add_wing(request):
    """
    Adds a new wing to the fleet in-game.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)
    
    logger.info(f"FC {request.user.username} adding wing to fleet {fleet.esi_fleet_id}")
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        new_wing = esi.client.Fleets.post_fleets_fleet_id_wings(
            fleet_id=fleet.esi_fleet_id,
            token=token.access_token
        ).results()
        
        wing_id = new_wing['wing_id']
        logger.debug(f"Created new wing {wing_id} in ESI, renaming")
        
        esi.client.Fleets.put_fleets_fleet_id_wings_wing_id(
            fleet_id=fleet.esi_fleet_id,
            wing_id=wing_id,
            naming={'name': "New Wing"},
            token=token.access_token
        ).results()
        
        logger.info(f"Wing {wing_id} added to fleet by {fc_character.character_name}")
        return JsonResponse({"status": "success", "message": "New wing added."})

    except Exception as e:
        logger.error(f"Error adding wing: {e}", exc_info=True)
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
@user_passes_test(is_fleet_commander)
def api_fc_delete_wing(request):
    """
    Deletes a wing from the fleet in-game.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    if not open_waitlist:
        return JsonResponse({"status": "error", "message": "Waitlist is closed."}, status=400)
        
    fleet = open_waitlist.fleet
    if not fleet.esi_fleet_id or not fleet.fleet_commander:
        return JsonResponse({"status": "error", "message": "Fleet is not linked or FC is not set."}, status=400)

    wing_id = request.POST.get('wing_id')
    logger.info(f"FC {request.user.username} deleting wing {wing_id}")
    if not wing_id:
        logger.warning("api_fc_delete_wing: Missing wing_id")
        return JsonResponse({"status": "error", "message": "Missing wing_id."}, status=400)
        
    try:
        fc_character = fleet.fleet_commander
        token = get_refreshed_token_for_character(request.user, fc_character)
        esi = EsiClientProvider()
        
        esi.client.Fleets.delete_fleets_fleet_id_wings_wing_id(
            fleet_id=fleet.esi_fleet_id,
            wing_id=wing_id,
            token=token.access_token
        ).results()
        
        logger.info(f"Wing {wing_id} deleted by {fc_character.character_name}")
        return JsonResponse({"status": "success", "message": "Wing deleted."})

    except Exception as e:
        logger.error(f"Error deleting wing {wing_id}: {e}", exc_info=True)
        return JsonResponse({"status":"error", "message": f"An error occurred: {str(e)}"}, status=500)