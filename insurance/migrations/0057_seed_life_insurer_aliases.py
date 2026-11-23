import re

from django.db import migrations

# How the real Life MIS exports (Sept 2026) spell the Life insurers, and the insurer name the Life grid uses for each.
# General-insurance names that turn up on Life rows (Bajaj Allianz General, Shriram General, Go-Digit General, TATA AIG
# General, Generali Central Insurance Company) are left out on purpose: the engine flags them for a person to decide.
SEEDS = [
    ("HDFC Life Insurance Co. Ltd.", "HDFC Life"),
    ("TATA AIA Life Insurance Co. Ltd.", "Tata AIA Life"),
    ("Bajaj Life Insurance Co. Ltd.", "Bajaj Allianz Life"),
    ("ICICI Prudential Life Insurance Co. Ltd", "ICICI Prudential Life"),
    ("Axis Max Life Insurance Co. Ltd.", "Axis Max Life"),
    ("Go-Digit Life Insurance Ltd.", "Digit Life"),
    ("Bharti Axa Life Insurance Co. Ltd.", "Bharti AXA Life"),
    ("India First Life Insurance Co.Ltd.", "IndiaFirst Life"),
    ("Aditya Birla Life Insurance Co. Ltd.", "Aditya Birla Sun Life (ABSLI)"),
    ("Aditya Birla SunLife Insurance Company Limited", "Aditya Birla Sun Life (ABSLI)"),
    ("Kotak Life Insurance Co. Ltd.", "Kotak Mahindra Life"),
    ("Shriram Life insurance Co. Ltd.", "Shriram Life"),
    ("Generali Central Life Insurance Co. Ltd.", "Generali Central (FGI)"),
    ("Premirica Life Insurance Co. Ltd.", "Pramerica Life"),
    ("PNB Metlife Insurance Co. Ltd.", "PNB MetLife"),
    ("Life Insurance Corporation of India", "LIC of India"),
]


def _key(text):
    # Kept in step with services/life_mis.py fold(): case, punctuation and spacing folded away.
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def seed_aliases(apps, schema_editor):
    LifeInsurerAlias = apps.get_model("insurance", "LifeInsurerAlias")
    for mis_name, insurer in SEEDS:
        LifeInsurerAlias.objects.get_or_create(
            name_key=_key(mis_name), defaults={"mis_name": mis_name, "insurer": insurer},
        )


def unseed_aliases(apps, schema_editor):
    LifeInsurerAlias = apps.get_model("insurance", "LifeInsurerAlias")
    LifeInsurerAlias.objects.filter(name_key__in=[_key(mis_name) for mis_name, _ in SEEDS]).delete()


class Migration(migrations.Migration):
    """Seeds the Life insurer aliases the real MIS needs. get_or_create, so staff edits are never overwritten."""

    dependencies = [
        ("insurance", "0056_life_mis_mapping"),
    ]

    operations = [
        migrations.RunPython(seed_aliases, unseed_aliases),
    ]
