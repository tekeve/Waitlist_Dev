import logging
import json
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponse
from django.utils import timezone
from django_eventstream import send_event
from .models import EveCharacter, ShipFit, FleetWaitlist
from pilot.models import EveType
from .fit_parser import parse_eft_fit
from .helpers import is_fleet_commander

# Get a logger for this specific Python file
logger = logging.getLogger(__name__)


@login_required
def home(request):
    """
    Handles the main homepage (/).
    - If user is authenticated, shows the waitlist_view.
    - If not, shows the simple login page (homepage.html).
    """
    
    logger.debug(f"User {request.user.username} accessing home view")
    
    if not request.user.is_authenticated:
        logger.debug("User is not authenticated, showing public homepage.html")
        return render(request, 'homepage.html')

    logger.debug("User is authenticated, preparing waitlist_view.html")
    
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    
    all_fits = []
    if open_waitlist:
        logger.debug(f"Open waitlist found: {open_waitlist.fleet.description}")
        all_fits = ShipFit.objects.filter(
            waitlist=open_waitlist,
            status__in=['PENDING', 'APPROVED']
        ).select_related('character').order_by('submitted_at')
    else:
        logger.debug("No open waitlist found.")

    xup_fits = all_fits.filter(status='PENDING') if open_waitlist else []
    dps_fits = all_fits.filter(status='APPROVED', category__in=['DPS', 'MAR_DPS']) if open_waitlist else []
    logi_fits = all_fits.filter(status='APPROVED', category='LOGI') if open_waitlist else []
    sniper_fits = all_fits.filter(status='APPROVED', category__in=['SNIPER', 'MAR_SNIPER']) if open_waitlist else []
    other_fits = all_fits.filter(status='APPROVED', category='OTHER') if open_waitlist else []
    
    is_fc = is_fleet_commander(request.user)
    
    all_user_chars = request.user.eve_characters.all().order_by('character_name')
    main_char = all_user_chars.filter(is_main=True).first()
    if not main_char:
        main_char = all_user_chars.first()
    
    context = {
        'xup_fits': xup_fits,
        'dps_fits': dps_fits,
        'logi_fits': logi_fits,
        'sniper_fits': sniper_fits,
        'other_fits': other_fits,
        'is_fc': is_fc,
        'open_waitlist': open_waitlist,
        'user_characters': all_user_chars,
        'all_chars_for_header': all_user_chars,
        'main_char_for_header': main_char,
    }
    return render(request, 'waitlist_view.html', context)

@login_required
@require_POST
def api_submit_fit(request):
    """
    Handles the fit submission from the X-Up modal.
    """
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    logger.debug(f"User {request.user.username} attempting fit submission")

    if not open_waitlist:
        logger.warning(f"Fit submission failed for {request.user.username}: Waitlist is closed")
        return JsonResponse({"status": "error", "message": "The waitlist is currently closed."}, status=400)

    character_id = request.POST.get('character_id')
    raw_fit_original = request.POST.get('raw_fit') 
    
    try:
        character = EveCharacter.objects.get(
            character_id=character_id, 
            user=request.user
        )
    except EveCharacter.DoesNotExist:
        logger.warning(f"Fit submission failed: User {request.user.username} submitted for char {character_id} which they don't own")
        return JsonResponse({"status": "error", "message": "Invalid character selected."}, status=403)
    
    if not raw_fit_original:
        logger.warning(f"Fit submission failed for {character.character_name}: Fit was empty")
        return JsonResponse({"status": "error", "message": "Fit cannot be empty."}, status=400)
    
    try:
        logger.debug(f"Parsing fit for {character.character_name}")
        ship_type, parsed_fit_list, fit_summary_counter = parse_eft_fit(raw_fit_original)
        ship_type_id = ship_type.type_id
        logger.debug(f"Fit parsed successfully: {ship_type.name}")

        new_status = 'PENDING'
        new_category = ShipFit.FitCategory.NONE
        logger.info(f"Fit for {character.character_name} submitted. Status set to PENDING.")

        fit, created = ShipFit.objects.update_or_create(
            character=character,
            waitlist=open_waitlist,
            status__in=['PENDING', 'APPROVED'],
            defaults={
                'raw_fit': raw_fit_original,
                'parsed_fit_json': json.dumps(parsed_fit_list),
                'status': new_status,
                'waitlist': open_waitlist,
                'ship_name': ship_type.name,
                'ship_type_id': ship_type_id,
                'tank_type': None,
                'fit_issues': None,
                'category': new_category,
                'submitted_at': timezone.now(),
                'last_updated': timezone.now(),
            }
        )
        
        logger.debug("Sending 'waitlist-updates' event")
        send_event('waitlist-updates', 'update', {
            'fit_id': fit.id,
            'action': 'submit'
        })
        
        if created:
            logger.info(f"New fit {fit.id} created for {character.character_name}")
            return JsonResponse({"status": "success", "message": f"Fit for {character.character_name} submitted!"})
        else:
            logger.info(f"Fit {fit.id} updated for {character.character_name}")
            return JsonResponse({"status": "success", "message": f"Fit for {character.character_name} updated."})

    except ValueError as e:
        logger.warning(f"Fit parsing failed for {character.character_name}: {e}")
        return JsonResponse({"status": "error", "message": str(e)}, status=400)
    except Exception as e:
        logger.error(f"Unexpected error in api_submit_fit for {character.character_name}: {e}", exc_info=True)
        return JsonResponse({"status": "error", "message": f"An unexpected error occurred: {str(e)}"}, status=500)


@login_required
@require_POST
def api_update_fit_status(request):
    """
    Handles FC actions (approve/deny) from the waitlist view.
    """
    if not is_fleet_commander(request.user):
        logger.warning(f"Non-FC user {request.user.username} tried to update fit status")
        return JsonResponse({"status": "error", "message": "Not authorized"}, status=403)

    fit_id = request.POST.get('fit_id')
    action = request.POST.get('action')
    logger.info(f"FC {request.user.username} performing action '{action}' on fit {fit_id}")

    try:
        fit = ShipFit.objects.get(id=fit_id)
    except ShipFit.DoesNotExist:
        logger.warning(f"FC {request.user.username} tried to {action} non-existent fit {fit_id}")
        return JsonResponse({"status": "error", "message": "Fit not found"}, status=404)

    if action == 'approve':
        fit.status = 'APPROVED'
        
        if fit.category == ShipFit.FitCategory.NONE:
            fit.category = ShipFit.FitCategory.OTHER
            logger.debug(f"Fit {fit.id} approved, category set to OTHER")
        
        fit.save()
        
        logger.debug("Sending 'waitlist-updates' event")
        send_event('waitlist-updates', 'update', {
            'fit_id': fit.id,
            'action': 'approve'
        })
        
        logger.info(f"Fit {fit.id} ({fit.character.character_name}) approved by {request.user.username}")
        return JsonResponse({"status": "success", "message": "Fit approved"})
        
    elif action == 'deny':
        fit.status = 'DENIED'
        fit.denial_reason = "Denied by FC from waitlist."
        fit.save()
        
        logger.debug("Sending 'waitlist-updates' event")
        send_event('waitlist-updates', 'update', {
            'fit_id': fit.id,
            'action': 'deny'
        })
        
        logger.info(f"Fit {fit.id} ({fit.character.character_name}) denied by {request.user.username}")
        return JsonResponse({"status": "success", "message": "Fit denied"})

    logger.warning(f"FC {request.user.username} sent invalid action '{action}' for fit {fit_id}")
    return JsonResponse({"status": "error", "message": "Invalid action"}, status=400)


@login_required
def api_get_waitlist_html(request):
    """
    Returns just the HTML for the waitlist columns.
    Used by the live polling JavaScript.
    """
    logger.debug(f"Polling request received from {request.user.username}")
    
    open_waitlist = FleetWaitlist.objects.filter(is_open=True).first()
    
    if not open_waitlist:
        logger.debug("Polling request: Waitlist is closed")
        return HttpResponseBadRequest("Waitlist closed")

    all_fits = ShipFit.objects.filter(
        waitlist=open_waitlist,
        status__in=['PENDING', 'APPROVED']
    ).select_related('character').order_by('submitted_at')

    xup_fits = all_fits.filter(status='PENDING')
    dps_fits = all_fits.filter(status='APPROVED', category__in=['DPS', 'MAR_DPS'])
    logi_fits = all_fits.filter(status='APPROVED', category='LOGI')
    sniper_fits = all_fits.filter(status='APPROVED', category__in=['SNIPER', 'MAR_SNIPER'])
    other_fits = all_fits.filter(status='APPROVED', category='OTHER')
    
    is_fc = is_fleet_commander(request.user)

    context = {
        'xup_fits': xup_fits,
        'dps_fits': dps_fits,
        'logi_fits': logi_fits,
        'sniper_fits': sniper_fits,
        'other_fits': other_fits,
        'is_fc': is_fc,
    }
    
    logger.debug(f"Polling response: XUP:{xup_fits.count()}, LOGI:{logi_fits.count()}, DPS:{dps_fits.count()}, SNIPER:{sniper_fits.count()}, OTHER:{other_fits.count()}")
    return render(request, '_waitlist_columns.html', context)