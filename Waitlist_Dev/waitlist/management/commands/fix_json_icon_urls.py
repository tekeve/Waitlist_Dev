from django.core.management.base import BaseCommand
from waitlist.models import DoctrineFit, ShipFit
import json

class Command(BaseCommand):
    help = 'Finds and fixes broken "https." icon URLs in parsed_fit_json fields.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("--- Starting to fix broken icon URLs ---"))
        
        # --- Fix Doctrine Fits ---
        self.stdout.write("Scanning DoctrineFits...")
        doctrine_fits_to_update = []
        for fit in DoctrineFit.objects.filter(parsed_fit_json__isnull=False):
            if '"icon_url": "https.' in fit.parsed_fit_json:
                self.stdout.write(self.style.WARNING(f"  Found broken URL in: {fit.name}"))
                fit.parsed_fit_json = fit.parsed_fit_json.replace(
                    '"icon_url": "https.', 
                    '"icon_url": "https://'
                )
                doctrine_fits_to_update.append(fit)
        
        if doctrine_fits_to_update:
            DoctrineFit.objects.bulk_update(doctrine_fits_to_update, ['parsed_fit_json'], batch_size=100)
            self.stdout.write(self.style.SUCCESS(f"Fixed {len(doctrine_fits_to_update)} DoctrineFits."))
        else:
            self.stdout.write("No broken DoctrineFits found.")

        # --- Fix Submitted ShipFits ---
        self.stdout.write("\nScanning ShipFits...")
        ship_fits_to_update = []
        for fit in ShipFit.objects.filter(parsed_fit_json__isnull=False):
            if '"icon_url": "https.' in fit.parsed_fit_json:
                # No need to log every single one, just fix them
                fit.parsed_fit_json = fit.parsed_fit_json.replace(
                    '"icon_url": "https.', 
                    '"icon_url": "https://'
                )
                ship_fits_to_update.append(fit)
        
        if ship_fits_to_update:
            ShipFit.objects.bulk_update(ship_fits_to_update, ['parsed_fit_json'], batch_size=100)
            self.stdout.write(self.style.SUCCESS(f"Fixed {len(ship_fits_to_update)} ShipFits."))
        else:
            self.stdout.write("No broken ShipFits found.")

        self.stdout.write(self.style.SUCCESS("\n--- Icon URL fix complete ---"))