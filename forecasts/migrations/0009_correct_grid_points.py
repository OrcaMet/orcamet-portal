"""
Correct grid_points on existing runs to the coverage actually achieved.

The field was written once at run creation with the intended grid size and
never corrected, so a run that lost points to rate limiting still reported
the full grid to the map. risk_grid now writes the achieved count at
finalise; this backfills the rows already in the database so the map stops
overstating coverage before the next scheduled run rather than after it.

Runs with no points are left alone: there is nothing to count, and their
stored value is still the honest record of what was attempted.
"""

from django.db import migrations


def correct_grid_points(apps, schema_editor):
    UKRiskGridRun = apps.get_model("forecasts", "UKRiskGridRun")
    UKRiskGridPoint = apps.get_model("forecasts", "UKRiskGridPoint")

    for run in UKRiskGridRun.objects.all():
        # Distinct coordinate pairs, not distinct rows: every point carries
        # one row per forecast hour.
        achieved = (
            UKRiskGridPoint.objects.filter(run=run)
            .values("latitude", "longitude")
            .distinct()
            .count()
        )
        if achieved and achieved != run.grid_points:
            run.grid_points = achieved
            run.save(update_fields=["grid_points"])


def noop(apps, schema_editor):
    """Nothing to undo: the corrected value is the more accurate one, and the
    intended grid size stays derivable from each run's bounds and resolution."""


class Migration(migrations.Migration):

    dependencies = [
        ("forecasts", "0008_ukriskgridpoint_ensemble_members_and_more"),
    ]

    operations = [
        migrations.RunPython(correct_grid_points, noop),
    ]
