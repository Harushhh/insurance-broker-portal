"""Make/Model Mapping page: link the different spellings of one vehicle across insurers' MakeModelMaster clusters."""
from django.utils import timezone
from django.shortcuts import render
from django.http import JsonResponse
from django.db.models import Q
from django.db import IntegrityError, transaction
import json
from ..models import AuditLog, MakeModelMapping, MakeModelMappingValue
from ..services.make_model_mapping import _make_model_cluster_items, _make_model_mapping_suggestions, _mapping_lookup, _normalize_mapping_value


# -------------------------
# MAKE MODEL MAPPING
# -------------------------
# Different insurers' MakeModelMaster clusters spell the same vehicle
# differently ("HERO SPLENDOR" in one group, "HERO MOTOR SPLENDOR" in
# another). This page records which spellings are the same vehicle, and
# resolve_make_model_groups expands a searched spelling to all of them so the
# search pages find every group listing any one. Clusters are never rewritten
# from here -- deleting a mapping only unlinks the spellings.
MAKE_MODEL_MAPPING_MAX_VALUE_LEN = 500   # MakeModelMapping.name / MakeModelMappingValue.value max_length


MAKE_MODEL_MAPPING_VALUES_LIMIT = 50


def make_model_mapping(request):
    """Renders the page shell only -- the table and modal are filled by JS from the JSON endpoints below."""
    return render(request, "make_model_mapping.html", {
        "total": MakeModelMapping.objects.count(),
    })


def make_model_mapping_list(request):
    """JSON: every mapping (optionally filtered by `q` on name or any spelling), most recently updated first."""
    q = (request.GET.get("q") or "").strip()
    qs = MakeModelMapping.objects.select_related("created_by").prefetch_related("values")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(values__value__icontains=q)).distinct()

    cluster_items = _make_model_cluster_items()
    results = [
        {
            "id": m.id,
            "name": m.name,
            "values": [
                {"value": v.value, "groups": cluster_items.get(v.value, [])}
                for v in m.values.all()
            ],
            "created_by": m.created_by.username if m.created_by else "",
            "updated_at": timezone.localtime(m.updated_at).strftime("%d %b %Y, %I:%M %p"),
        }
        for m in qs.order_by("-updated_at", "-id")
    ]
    return JsonResponse({"results": results, "total": len(results)})


def make_model_mapping_values(request):
    """JSON autocomplete: distinct cluster items containing `q` (prefix matches first), with their groups."""
    q = _normalize_mapping_value(request.GET.get("q"))
    if len(q) < 2:
        return JsonResponse({"results": []})

    cluster_items = _make_model_cluster_items()
    matches = sorted(
        (item for item in cluster_items if q in item),
        key=lambda item: (not item.startswith(q), item),
    )[:MAKE_MODEL_MAPPING_VALUES_LIMIT]
    mapped = _mapping_lookup(matches)

    results = [
        {
            "value": item,
            "groups": cluster_items[item],
            "mapping_id": mapped.get(item, (None, None))[0],
            "mapped_to": mapped.get(item, (None, None))[1],
        }
        for item in matches
    ]
    return JsonResponse({"results": results})


def make_model_mapping_suggest(request):
    """JSON: the picked spelling's own groups/mapping, plus other cluster items that look like the same vehicle."""
    value = _normalize_mapping_value(request.GET.get("value"))
    if not value:
        return JsonResponse({"success": False, "message": "Pick a make/model first."}, status=400)

    cluster_items = _make_model_cluster_items()
    suggestions = _make_model_mapping_suggestions(value, cluster_items)
    mapped = _mapping_lookup([value] + [item for item, _ in suggestions])

    return JsonResponse({
        "success": True,
        "value": value,
        "groups": cluster_items.get(value, []),
        "mapping_id": mapped.get(value, (None, None))[0],
        "mapped_to": mapped.get(value, (None, None))[1],
        "suggestions": [
            {
                "value": item,
                "score": round(score),
                "groups": cluster_items[item],
                "mapping_id": mapped.get(item, (None, None))[0],
                "mapped_to": mapped.get(item, (None, None))[1],
            }
            for item, score in suggestions
        ],
    })


def save_make_model_mapping(request):
    """
    Creates (no `id`) or updates a mapping from {id?, name, values[]}. `name`
    is always kept as one of the spellings. A spelling can belong to only one
    mapping -- one already under a different mapping is rejected here, and
    MakeModelMappingValue.value's unique constraint backs that up if two
    saves race.
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Invalid request method."}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"success": False, "message": "Invalid request body."}, status=400)

    raw_values = data.get("values") or []
    if not isinstance(raw_values, list):
        return JsonResponse({"success": False, "message": "Invalid spellings list."}, status=400)

    name = _normalize_mapping_value(data.get("name"))
    if not name:
        return JsonResponse({"success": False, "message": "Main make/model is required."}, status=400)

    values = []
    for raw in [name] + raw_values:
        v = _normalize_mapping_value(raw)
        if v and v not in values:
            values.append(v)

    too_long = [v for v in values if len(v) > MAKE_MODEL_MAPPING_MAX_VALUE_LEN]
    if too_long:
        return JsonResponse({
            "success": False,
            "message": f"Spellings must be at most {MAKE_MODEL_MAPPING_MAX_VALUE_LEN} characters.",
        }, status=400)
    # Clusters are comma-separated, so a spelling containing a comma can never
    # equal a cluster item and would silently match nothing.
    with_comma = [v for v in values if "," in v]
    if with_comma:
        return JsonResponse({
            "success": False,
            "message": f'Spellings can\'t contain commas: "{with_comma[0]}".',
        }, status=400)
    if len(values) < 2:
        return JsonResponse({
            "success": False,
            "message": "Pick at least one other spelling to map to the main make/model.",
        }, status=400)

    mapping = None
    mapping_id = data.get("id")
    if mapping_id:
        mapping = MakeModelMapping.objects.filter(id=mapping_id).first()
        if mapping is None:
            return JsonResponse({"success": False, "message": "This mapping no longer exists."}, status=404)

    conflicts = MakeModelMappingValue.objects.filter(value__in=values).select_related("mapping")
    if mapping is not None:
        conflicts = conflicts.exclude(mapping=mapping)
    conflicts = list(conflicts[:5])
    if conflicts:
        return JsonResponse({
            "success": False,
            "message": "; ".join(
                f'"{c.value}" is already mapped under "{c.mapping.name}"' for c in conflicts
            ) + ". Remove it from that mapping first.",
        }, status=400)

    is_update = mapping is not None
    old_values = []
    try:
        with transaction.atomic():
            if not is_update:
                mapping = MakeModelMapping.objects.create(name=name, created_by=request.user)
            else:
                old_values = list(mapping.values.values_list("value", flat=True))
                mapping.name = name
                mapping.save(update_fields=["name", "updated_at"])
                mapping.values.exclude(value__in=values).delete()
            MakeModelMappingValue.objects.bulk_create([
                MakeModelMappingValue(mapping=mapping, value=v)
                for v in values if v not in old_values
            ])
    except IntegrityError:
        return JsonResponse({
            "success": False,
            "message": "One of these spellings was just mapped by someone else -- refresh and try again.",
        }, status=400)

    if is_update:
        added = [v for v in values if v not in old_values]
        removed = [v for v in old_values if v not in values]
        details = (
            f"Updated Make/Model Mapping '{name}' (id {mapping.id}). Spellings: {', '.join(values)}."
            + (f" Added: {', '.join(added)}." if added else "")
            + (f" Removed: {', '.join(removed)}." if removed else "")
        )
        action = "MAKE MODEL MAPPING UPDATE"
    else:
        details = f"Created Make/Model Mapping '{name}' (id {mapping.id}). Spellings: {', '.join(values)}."
        action = "MAKE MODEL MAPPING CREATE"
    AuditLog.objects.create(user=request.user, action=action, details=details)

    return JsonResponse({"success": True, "id": mapping.id, "name": mapping.name, "values": values})


def delete_make_model_mapping(request):
    """Deletes one mapping and its spelling links. MakeModelMaster clusters are untouched."""
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "Invalid request method."}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"success": False, "message": "Invalid request body."}, status=400)

    mapping = MakeModelMapping.objects.filter(id=data.get("id")).first()
    if mapping is None:
        return JsonResponse({"success": False, "message": "This mapping no longer exists."}, status=404)

    name = mapping.name
    values = list(mapping.values.values_list("value", flat=True))
    mapping_pk = mapping.id
    mapping.delete()

    AuditLog.objects.create(
        user=request.user,
        action="MAKE MODEL MAPPING DELETE",
        details=f"Deleted Make/Model Mapping '{name}' (id {mapping_pk}). Spellings: {', '.join(values)}.",
    )
    return JsonResponse({"success": True})
