"""Helpers for the Make/Model Mapping page: value normalisation, cluster items and suggestions."""
from collections import defaultdict
from ..models import MakeModelMaster, MakeModelMappingValue


# rapidfuzz token_set_ratio floor for a suggestion. On real cluster data every
# candidate scoring 78-89 was a different model of the same make ("HONDA CLIQ"
# for "HONDA CITY" = 80, "HYUNDAI XCENT" for "HYUNDAI CRETA" = 85), while a
# one-letter typo ("HERO SPLENDER" for "HERO SPLENDOR") still scores 92.
MAKE_MODEL_MAPPING_SUGGEST_CUTOFF = 90


MAKE_MODEL_MAPPING_SUGGEST_LIMIT = 50


def _normalize_mapping_value(value):
    # Same normalization strict_match_in_cluster applies to cluster items.
    return str(value or "").strip().upper()


def _make_model_cluster_items():
    """{UPPER cluster item: sorted [group names listing it]} over every MakeModelMaster row, in one query."""
    items = defaultdict(set)
    rows = (
        MakeModelMaster.objects.exclude(make_model_cluster__isnull=True)
        .exclude(make_model_cluster="")
        .values_list("make_model_name", "make_model_cluster")
    )
    for name, cluster in rows:
        for item in str(cluster).split(","):
            item = _normalize_mapping_value(item)
            if item:
                items[item].add(name)
    return {item: sorted(names) for item, names in items.items()}


def _make_model_mapping_suggestions(value, cluster_items):
    """
    Cluster items that look like another spelling of `value`, best first.

    token_set_ratio after default_process (lower-cases, strips punctuation, so
    "SPLENDOR+" compares as "splendor") scores "HERO MOTOR SPLENDOR" 100
    against "HERO SPLENDOR". It also scores any word-subset 100, though, so a
    lone "HERO" would top the list -- one-word candidates are dropped. And
    since most real candidates tie at 100, token_sort_ratio (which penalizes
    extra words) breaks the tie, putting "HERO SPLENDOR +" ahead of "HERO
    SPLENDOR PRO" ahead of "HERO MOTOCORP SUPER SPLENDOR".
    """
    from rapidfuzz import fuzz, process as rf_process, utils as rf_utils

    choices = [item for item in cluster_items if item != value]
    matches = rf_process.extract(
        value, choices,
        scorer=fuzz.token_set_ratio,
        processor=rf_utils.default_process,
        score_cutoff=MAKE_MODEL_MAPPING_SUGGEST_CUTOFF,
        limit=None,
    )
    matches = [
        (item, score) for item, score, _ in matches
        if len(rf_utils.default_process(item).split()) >= 2
    ]
    matches.sort(key=lambda m: (
        -m[1],
        -fuzz.token_sort_ratio(value, m[0], processor=rf_utils.default_process),
        m[0],
    ))
    return matches[:MAKE_MODEL_MAPPING_SUGGEST_LIMIT]


def _mapping_lookup(values):
    """{value: (mapping_id, mapping name)} for whichever of `values` already belong to a mapping."""
    return {
        value: (mapping_id, name)
        for value, mapping_id, name in MakeModelMappingValue.objects.filter(value__in=values)
        .values_list("value", "mapping_id", "mapping__name")
    }
