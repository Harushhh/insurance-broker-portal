"""Life Mapping (MIS Payout Engine): the two small tables that tell the engine's Life route which Life insurer and which Life
grid products a MIS spelling stands for (services/life_mis.py holds the matching rules). A Life policy whose insurer or plan
nobody has mapped is never guessed: it ends as NO MATCH, and the spelling is listed here, from the stored failed rows of the
processed MIS files, until someone maps it."""
from django.contrib import messages
from django.db import IntegrityError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from ..forms import LifeInsurerAliasForm, LifePlanMappingForm
from ..models import LifeInsurerAlias, LifePlanMapping
from ..services import life_mis
from ..services.activity import audit_change

WAITING_LIMIT = 100          # the most spellings listed as still waiting to be mapped, most policies first


# -------------------------
# PLAN MAPPING
# -------------------------
def _plan_page(request, grid, form):
    waiting, _insurers = life_mis.unmapped_from_failed_rows()
    waiting = waiting[:WAITING_LIMIT]
    for item in waiting:
        item["close"] = life_mis.suggestions_for(item["plan"], item["insurer"], grid)

    q = (request.GET.get("q") or "").strip().lower()
    insurer_filter = (request.GET.get("insurer") or "").strip()
    mappings = []
    for mapping in LifePlanMapping.objects.select_related("created_by"):
        if insurer_filter and mapping.insurer != insurer_filter:
            continue
        lines = life_mis.grid_product_lines(mapping.grid_products)
        if q and q not in mapping.plan_name.lower() and not any(q in line.lower() for line in lines):
            continue
        products, rows = life_mis.mapping_reach(grid, mapping.insurer, mapping.grid_products)
        mappings.append({"mapping": mapping, "lines": lines, "products": products, "rows": rows})

    return render(request, "life_mis_plans.html", {
        "section": "plans",
        "grid": grid,
        "form": form,
        "waiting": waiting,
        "mappings": mappings,
        "total": LifePlanMapping.objects.count(),
        "q": request.GET.get("q", ""),
        "insurer_filter": insurer_filter,
        "insurers": sorted(grid.insurers) if grid is not None else [],
        "picker_products": life_mis.picker_products(grid),
    })


def life_plan_mapping(request):
    """The plans waiting to be mapped, the form to map one, and every mapping made so far."""
    grid = life_mis.load_life_grid()
    initial = {}
    edit = LifePlanMapping.objects.filter(pk=request.GET.get("edit")).first() if request.GET.get("edit", "").isdigit() else None
    if edit is not None:
        initial = {"id": edit.id, "insurer": edit.insurer, "plan_name": edit.plan_name,
                   "grid_products": edit.grid_products, "note": edit.note}
    elif request.GET.get("plan"):
        initial = {"insurer": request.GET.get("insurer", ""), "plan_name": request.GET.get("plan", "")}
    return _plan_page(request, grid, LifePlanMappingForm(initial=initial, grid=grid))


@require_POST
def life_plan_mapping_save(request):
    """Creates a plan mapping (no id) or updates one."""
    grid = life_mis.load_life_grid()
    form = LifePlanMappingForm(request.POST, grid=grid)
    if not form.is_valid():
        return _plan_page(request, grid, form)

    data = form.cleaned_data
    mapping = None
    if data.get("id"):
        mapping = LifePlanMapping.objects.filter(pk=data["id"]).first()
        if mapping is None:
            messages.error(request, "That mapping no longer exists.")
            return redirect("life_plan_mapping")
    created = mapping is None
    before = "" if created else f" Was: {mapping.insurer} / '{mapping.plan_name}' -> {_one_line(mapping.grid_products)}."
    old_values = None if created else {"insurer": mapping.insurer, "plan_name": mapping.plan_name,
                                       "grid_products": mapping.grid_products, "note": mapping.note}
    if created:
        mapping = LifePlanMapping(created_by=request.user)
    mapping.insurer = data["insurer"]
    mapping.plan_name = data["plan_name"]
    mapping.plan_key = life_mis.fold(data["plan_name"])
    mapping.grid_products = "\n".join(life_mis.grid_product_lines(data["grid_products"]))
    mapping.note = data.get("note") or ""
    try:
        mapping.save()
    except IntegrityError:
        messages.error(request, "Someone just mapped this plan for this insurer: refresh and edit that mapping.")
        return redirect("life_plan_mapping")

    new_values = {"insurer": mapping.insurer, "plan_name": mapping.plan_name,
                  "grid_products": mapping.grid_products, "note": mapping.note}
    audit_change(
        request.user, "LIFE PLAN MAPPING CREATE" if created else "LIFE PLAN MAPPING UPDATE",
        (f"{'Created' if created else 'Updated'} Life Plan Mapping {mapping.id}: {mapping.insurer} / "
         f"'{mapping.plan_name}' -> {_one_line(mapping.grid_products)}.{before}"),
        summary=f"{'Created' if created else 'Updated'} the Life plan mapping for {mapping.insurer} / '{mapping.plan_name}'",
        target=mapping,
        details={"mapping_id": mapping.id,
                 **({"values": new_values} if created else
                    {"changes": {name: {"from": old_values[name], "to": value}
                                 for name, value in new_values.items() if old_values[name] != value}})},
    )
    products, rows = life_mis.mapping_reach(grid, mapping.insurer, mapping.grid_products)
    messages.success(
        request,
        f"Saved: '{mapping.plan_name}' ({mapping.insurer}) now reaches {products} grid product(s), {rows} row(s) of the "
        f"live Life grid ({grid.label}). Re-run mapping for a MIS file to use it.")
    return redirect("life_plan_mapping")


@require_POST
def life_plan_mapping_delete(request, pk):
    mapping = LifePlanMapping.objects.filter(pk=pk).first()
    if mapping is None:
        messages.error(request, "That mapping no longer exists.")
        return redirect("life_plan_mapping")
    summary = f"{mapping.insurer} / '{mapping.plan_name}' -> {_one_line(mapping.grid_products)}"
    removed = {"insurer": mapping.insurer, "plan_name": mapping.plan_name, "grid_products": mapping.grid_products,
               "note": mapping.note}
    mapping.delete()
    audit_change(
        request.user, "LIFE PLAN MAPPING DELETE", f"Deleted Life Plan Mapping {pk}: {summary}.",
        summary=f"Deleted the Life plan mapping for {removed['insurer']} / '{removed['plan_name']}'",
        details={"mapping_id": pk, "values": removed})
    messages.success(request, f"Deleted the mapping for '{summary.split(' -> ')[0]}'.")
    return redirect("life_plan_mapping")


# -------------------------
# INSURER ALIASES
# -------------------------
def _insurer_page(request, grid, form):
    _plans, waiting = life_mis.unmapped_from_failed_rows()
    waiting = waiting[:WAITING_LIMIT]
    aliases = [
        {"alias": alias, "in_grid": grid is not None and alias.insurer in grid.insurers}
        for alias in LifeInsurerAlias.objects.select_related("created_by")
    ]
    return render(request, "life_mis_insurers.html", {
        "section": "insurers",
        "grid": grid,
        "form": form,
        "waiting": waiting,
        "aliases": aliases,
        "total": len(aliases),
    })


def life_insurer_aliases(request):
    """The insurer names waiting to be mapped, the form to map one, and every alias made so far."""
    grid = life_mis.load_life_grid()
    initial = {}
    edit = LifeInsurerAlias.objects.filter(pk=request.GET.get("edit")).first() if request.GET.get("edit", "").isdigit() else None
    if edit is not None:
        initial = {"id": edit.id, "mis_name": edit.mis_name, "insurer": edit.insurer}
    elif request.GET.get("name"):
        initial = {"mis_name": request.GET.get("name", "")}
    return _insurer_page(request, grid, LifeInsurerAliasForm(initial=initial, grid=grid))


@require_POST
def life_insurer_alias_save(request):
    """Creates an insurer alias (no id) or updates one."""
    grid = life_mis.load_life_grid()
    form = LifeInsurerAliasForm(request.POST, grid=grid)
    if not form.is_valid():
        return _insurer_page(request, grid, form)

    data = form.cleaned_data
    alias = None
    if data.get("id"):
        alias = LifeInsurerAlias.objects.filter(pk=data["id"]).first()
        if alias is None:
            messages.error(request, "That alias no longer exists.")
            return redirect("life_insurer_aliases")
    created = alias is None
    before = "" if created else f" Was: '{alias.mis_name}' -> {alias.insurer}."
    old_values = None if created else {"mis_name": alias.mis_name, "insurer": alias.insurer}
    if created:
        alias = LifeInsurerAlias(created_by=request.user)
    alias.mis_name = data["mis_name"]
    alias.name_key = life_mis.fold(data["mis_name"])
    alias.insurer = data["insurer"]
    try:
        alias.save()
    except IntegrityError:
        messages.error(request, "Someone just mapped this name: refresh and edit that alias.")
        return redirect("life_insurer_aliases")

    new_values = {"mis_name": alias.mis_name, "insurer": alias.insurer}
    audit_change(
        request.user, "LIFE INSURER ALIAS CREATE" if created else "LIFE INSURER ALIAS UPDATE",
        f"{'Created' if created else 'Updated'} Life Insurer Alias {alias.id}: '{alias.mis_name}' -> {alias.insurer}.{before}",
        summary=f"{'Created' if created else 'Updated'} the Life insurer alias '{alias.mis_name}' -> {alias.insurer}",
        target=alias,
        details={"alias_id": alias.id,
                 **({"values": new_values} if created else
                    {"changes": {name: {"from": old_values[name], "to": value}
                                 for name, value in new_values.items() if old_values[name] != value}})},
    )
    messages.success(request, f"Saved: '{alias.mis_name}' now means {alias.insurer}. Re-run mapping for a MIS file to use it.")
    return redirect("life_insurer_aliases")


@require_POST
def life_insurer_alias_delete(request, pk):
    alias = LifeInsurerAlias.objects.filter(pk=pk).first()
    if alias is None:
        messages.error(request, "That alias no longer exists.")
        return redirect("life_insurer_aliases")
    summary = f"'{alias.mis_name}' -> {alias.insurer}"
    removed = {"mis_name": alias.mis_name, "insurer": alias.insurer}
    alias.delete()
    audit_change(
        request.user, "LIFE INSURER ALIAS DELETE", f"Deleted Life Insurer Alias {pk}: {summary}.",
        summary=f"Deleted the Life insurer alias {summary}", details={"alias_id": pk, "values": removed})
    messages.success(request, f"Deleted the alias {summary}.")
    return redirect("life_insurer_aliases")


def _one_line(text):
    return "; ".join(life_mis.grid_product_lines(text))
