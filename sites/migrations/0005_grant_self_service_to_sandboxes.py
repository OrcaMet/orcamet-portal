"""
Give existing sandbox clients the self-service flag they used to get implicitly.

Until now, `sites/views.py` gated self-service site management on
`Client.is_sandbox`. It now gates on `Client.self_service_sites`, which
defaults to False — so without this, every existing trial workspace would
silently lose the ability to add its own sites the moment this deploys.

Staff-managed clients are deliberately left at False: that is the behaviour
they already have, and granting it here would open self-service to clients
who have never had it.
"""

from django.db import migrations


def grant(apps, schema_editor):
    Client = apps.get_model("sites", "Client")
    Client.objects.filter(is_sandbox=True).update(self_service_sites=True)


def revoke(apps, schema_editor):
    """
    Reverse cleanly, but only for the rows this migration could have set.

    A real client onboarded after this migration ran has
    self_service_sites=True and is_sandbox=False; clearing that here would
    take away access the invite deliberately granted.
    """
    Client = apps.get_model("sites", "Client")
    Client.objects.filter(is_sandbox=True).update(self_service_sites=False)


class Migration(migrations.Migration):

    dependencies = [
        ("sites", "0004_client_onboarding_completed_at_and_more"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
