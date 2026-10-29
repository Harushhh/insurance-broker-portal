"""CSV import: the upload page and the chunked-upload endpoints behind it (Rate Master and the master tables)."""
from django.utils import timezone
from django.shortcuts import render
from django.http import JsonResponse
from django.core.cache import cache
from django.db import transaction
import json
from datetime import datetime
from collections import defaultdict
from ..models import RTOMaster, MakeModelMaster, PincodeMaster, RateMaster, YesNoNAMaster, ProductMaster, SubProductMaster, PolicyTypeMaster, FuelTypeMaster, MakeModelClassMaster, RateGroup, HealthRateMaster, CsvUploadAttempt
from ..health_grid_utils import parse_number as parse_health_number, parse_percent as parse_health_percent, parse_grid_date as parse_health_date, upsert_health_rate_row
from ..services.common import parse_date
from ..services.masters import RTO_MAKE_CHOICES_CACHE_KEY
from ..services.rate_master import build_key_hash
import logging


logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# NEW ENTERPRISE STREAMING CSV UPLOAD & API CHUNKING LOGIC
# ---------------------------------------------------------

def import_data_view(request):
    """
    Renders the beautiful SaaS upload interface.
    The heavy lifting is handled via JS (PapaParse) to bypass Server RAM and timeouts.
    """
    return render(request, "upload.html")


def api_check_duplicate_upload(request):
    """
    Called by upload.html right before it starts reading/chunking a file,
    with a SHA-256 hash of the raw file bytes computed client-side. Records
    this attempt and reports back the most recent PRIOR attempt for the same
    (target_table, file_hash), if any -- so the frontend can warn "this exact
    file was already uploaded" before a retry or an accidental re-pick
    reprocesses a file that (fully or partially) already landed in the DB.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)
    try:
        data = json.loads(request.body)
        target_table = data.get('target_table')
        file_hash = data.get('file_hash')
        file_name = data.get('file_name', '')
        if not target_table or not file_hash:
            return JsonResponse({'status': 'error', 'message': 'target_table and file_hash are required'}, status=400)

        prior = CsvUploadAttempt.objects.filter(
            target_table=target_table, file_hash=file_hash
        ).order_by('-created_at').first()

        prior_info = None
        if prior:
            prior_info = {
                'uploaded_at': timezone.localtime(prior.created_at).strftime('%d %b %Y, %I:%M %p'),
                'uploaded_by': prior.uploaded_by.get_full_name() or prior.uploaded_by.username if prior.uploaded_by else 'Unknown user',
                'status': prior.status,
                'row_count': prior.row_count,
            }

        attempt = CsvUploadAttempt.objects.create(
            target_table=target_table,
            file_hash=file_hash,
            file_name=file_name[:255],
            uploaded_by=request.user if request.user.is_authenticated else None,
        )
        return JsonResponse({'status': 'ok', 'attempt_id': attempt.id, 'prior_upload': prior_info})
    except Exception as e:
        logger.exception("Duplicate-upload check failed")
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


def api_complete_upload(request):
    """
    Called by upload.html once a chunked upload's real (non-dry-run) pass
    finishes, success or failure, so later duplicate-upload checks can report
    an accurate outcome instead of every prior attempt looking identically
    "in progress" forever.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)
    try:
        data = json.loads(request.body)
        attempt_id = data.get('attempt_id')
        if not attempt_id:
            return JsonResponse({'status': 'error', 'message': 'attempt_id is required'}, status=400)
        CsvUploadAttempt.objects.filter(id=attempt_id).update(
            status='COMPLETED' if data.get('success') else 'FAILED',
            row_count=data.get('row_count'),
        )
        return JsonResponse({'status': 'ok'})
    except Exception as e:
        logger.exception("Could not record upload completion")
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


def api_upload_chunk(request):
    """
    Receives JSON chunks from the PapaParse frontend uploader.
    Uses bulk_create to efficiently insert data while mapping your specific schemas.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            rows = data.get('rows', [])
            target_table = data.get('target_table', 'rate_master')
            # One id per upload, shared by every chunk of the same file (see
            # upload.html) -- falls back to "now" only if a request somehow
            # arrives without it, so grouping never breaks, it just loses the
            # "chunks of this file recognize each other" guarantee for that
            # one request.
            upload_batch_id = data.get('upload_batch_id') or datetime.now().isoformat()
            # Set by the frontend's first (validation) pass over the whole
            # file, see upload.html -- every branch below still runs its full
            # validation either way, it just skips the actual database writes
            # when this is true, so a bad row anywhere in the file is caught
            # before a single row from it is saved.
            dry_run = bool(data.get('dry_run'))

            if not rows:
                return JsonResponse({'status': 'error', 'message': 'No data provided'}, status=400)

            inserted = 0

            with transaction.atomic():
                if target_table == 'rto_master':
                    for row in rows:
                        rto_name = (row.get("rto_name") or "").strip()
                        rto_cluster = (row.get("rto_cluster") or "").strip()
                        if not rto_name:
                            raise ValueError("rto_name is blank")
                        if not dry_run:
                            # __iexact, not update_or_create(rto_name=...): an
                            # exact-case match let the same group get re-uploaded
                            # under different casing (e.g. "ZYX" vs "Zyx") and
                            # silently create a second RTOMaster row instead of
                            # updating the existing one -- the split then breaks
                            # every case-sensitive comparison downstream.
                            existing = RTOMaster.objects.filter(rto_name__iexact=rto_name).first()
                            if existing:
                                existing.rto_cluster = rto_cluster or None
                                existing.save(update_fields=["rto_cluster", "updated_at"])
                            else:
                                RTOMaster.objects.create(rto_name=rto_name, rto_cluster=rto_cluster or None)
                        inserted += 1
                    if not dry_run:
                        cache.delete(RTO_MAKE_CHOICES_CACHE_KEY)

                elif target_table == 'make_model_master':
                    for row in rows:
                        make_model_name = (row.get("make_model_name") or "").strip()
                        make_model_cluster = (row.get("make_model_cluster") or "").strip()
                        if not make_model_name:
                            raise ValueError("make_model_name is blank")
                        if not dry_run:
                            # Same __iexact reasoning as rto_master above -- this is
                            # exactly how "Private_Car_ALL" and "Private_Car_All" end
                            # up as two separate MakeModelMaster rows for the same
                            # group after two insurers' files spell it differently.
                            existing = MakeModelMaster.objects.filter(make_model_name__iexact=make_model_name).first()
                            if existing:
                                existing.make_model_cluster = make_model_cluster or None
                                existing.save(update_fields=["make_model_cluster", "updated_at"])
                            else:
                                MakeModelMaster.objects.create(make_model_name=make_model_name, make_model_cluster=make_model_cluster or None)
                        inserted += 1
                    if not dry_run:
                        cache.delete(RTO_MAKE_CHOICES_CACHE_KEY)

                elif target_table == 'pincode_master':
                    for row in rows:
                        pincode_zone = (row.get("pincode_zone") or "").strip()
                        pincode_cluster = (row.get("pincode_cluster") or "").strip()
                        if not pincode_zone:
                            raise ValueError("pincode_zone is blank")
                        if not dry_run:
                            PincodeMaster.objects.update_or_create(
                                pincode_zone=pincode_zone,
                                defaults={"pincode_cluster": pincode_cluster or None}
                            )
                        inserted += 1

                elif target_table == 'rate_master':
                    valid_rtos = {str(rto).lower() for rto in RTOMaster.objects.values_list("rto_name", flat=True) if rto}
                    valid_makes = {str(make).lower() for make in MakeModelMaster.objects.values_list("make_model_name", flat=True) if make}

                    # Loaded once per chunk and indexed both by name and by id --
                    # a CSV that carries numeric master ids instead of names (a
                    # common export format) used to send every single row back
                    # to the DB for a fresh .filter(id=...).first(), since the
                    # id branch below never consulted the name-keyed cache. With
                    # these tables already fully loaded here, indexing by id too
                    # is free and turns that per-row query into a dict lookup.
                    products_all = list(ProductMaster.objects.all())
                    products_map = {p.name.lower(): p for p in products_all}
                    products_by_id = {p.id: p for p in products_all}
                    sub_products_all = list(SubProductMaster.objects.all())
                    sub_products_map = {sp.name.lower(): sp for sp in sub_products_all}
                    sub_products_by_id = {sp.id: sp for sp in sub_products_all}
                    policy_types_all = list(PolicyTypeMaster.objects.all())
                    policy_types_map = {pt.name.lower(): pt for pt in policy_types_all}
                    policy_types_by_id = {pt.id: pt for pt in policy_types_all}
                    fuel_types_all = list(FuelTypeMaster.objects.all())
                    fuel_types_map = {ft.name.lower(): ft for ft in fuel_types_all}
                    fuel_types_by_id = {ft.id: ft for ft in fuel_types_all}
                    mmc_classes_all = list(MakeModelClassMaster.objects.all())
                    mmc_classes_map = {m.name.lower(): m for m in mmc_classes_all}
                    mmc_classes_by_id = {m.id: m for m in mmc_classes_all}
                    
                    ynn_map = {y.code.lower(): y for y in YesNoNAMaster.objects.all()}
                    for code in ["YES", "NO", "NA"]:
                        if code.lower() not in ynn_map:
                            ynn_map[code.lower()] = YesNoNAMaster.objects.create(code=code)

                    def get_ynn(val):
                        if not val: return ynn_map["na"]
                        v = str(val).strip().lower()
                        if v in ["yes", "y", "true", "1"]: return ynn_map["yes"]
                        if v in ["no", "n", "false", "0"]: return ynn_map["no"]
                        return ynn_map["na"]

                    def get_master(val, mapping_dict, ModelClass, by_id_dict=None):
                        if not val: return None
                        v = str(val).strip()
                        if not v: return None
                        if v.isdigit() and ModelClass != ProductMaster:
                            obj = by_id_dict.get(int(v)) if by_id_dict is not None else ModelClass.objects.filter(id=int(v)).first()
                            if obj:
                                mapping_dict[obj.name.lower()] = obj
                                return obj
                        v_low = v.lower()
                        if v_low in mapping_dict:
                            return mapping_dict[v_low]
                        obj = ModelClass.objects.create(name=v)
                        mapping_dict[v_low] = obj
                        return obj

                    instances_to_create = []

                    # Cross-upload duplicate guard, batched: the per-row .exists()
                    # query this replaced (one complex ~30-column WHERE per row)
                    # took long enough over a 2500-row chunk to blow past Railway's
                    # proxy timeout and 500 the whole request. Every field below is
                    # a FloatField (see models.py) so these DB-fetched values
                    # compare exactly against the same float(row[...]) parses done
                    # per row further down -- no Decimal/float mismatch risk.
                    chunk_companies = {str(r.get("insurance_company", "")).strip() for r in rows}
                    existing_active_keys = set(
                        RateMaster.objects.filter(
                            status="ACTIVE",
                            is_deleted="NO",
                            insurance_company__in=chunk_companies,
                        ).values_list(
                            "insurance_company", "insurer_vertical", "new_vehicle_makes", "new_rto_list",
                            "product_id", "sub_product_id", "policy_type_id", "fuel_type_id", "make_model_class_id",
                            "vehicle_age_min", "vehicle_age_max",
                            "pi_od_rate", "pi_tp_rate", "pi_tp_2", "pi_tp_3", "pi_tp_4", "pi_tp_5",
                            "pi_net_rate", "pi_flat_amount", "pi_vli", "pi_type",
                            "tariff_min", "tariff_max",
                            "is_ncb_id", "is_cpa_id", "is_zd_id",
                            "cc_min", "cc_max", "from_date", "to_date", "sc_min", "sc_max", "add_tnc",
                        )
                    )

                    row_records = []
                    for row in rows:
                        raw_rtos = row.get("new_rto_list") or ""
                        rto_items = [x.strip() for x in raw_rtos.split(",") if x.strip()]
                        for rto in rto_items:
                            if rto.lower() not in valid_rtos:
                                raise ValueError(f"RTO '{rto}' does not exist in RTOMaster. Please add it first.")

                        raw_makes = row.get("new_vehicle_makes") or ""
                        make_items = [x.strip() for x in raw_makes.split(",") if x.strip()]
                        for make in make_items:
                            if make.lower() not in valid_makes:
                                raise ValueError(f"Vehicle Make '{make}' does not exist in MakeModelMaster. Please add it first.")

                        product_val = str(row.get("product", "")).strip()
                        product_obj = None
                        if product_val:
                            if product_val.isdigit():
                                product_obj = products_by_id.get(int(product_val))
                            else:
                                if product_val.lower() in products_map:
                                    product_obj = products_map[product_val.lower()]
                                else:
                                    product_obj = ProductMaster.objects.create(name=product_val)
                                    products_map[product_val.lower()] = product_obj

                        sub_product_obj = get_master(row.get("sub_product"), sub_products_map, SubProductMaster, sub_products_by_id)
                        policy_type_obj = get_master(row.get("policy_type"), policy_types_map, PolicyTypeMaster, policy_types_by_id)
                        fuel_type_obj = get_master(row.get("fuel_type"), fuel_types_map, FuelTypeMaster, fuel_types_by_id)
                        mmc_obj = get_master(row.get("make_model_class"), mmc_classes_map, MakeModelClassMaster, mmc_classes_by_id)
                        
                        is_ncb_obj = get_ynn(row.get("is_ncb"))
                        is_cpa_obj = get_ynn(row.get("is_cpa"))
                        is_zd_obj = get_ynn(row.get("is_zd"))

                        cleaned = {
                            "new_vehicle_makes": row.get("new_vehicle_makes") or None,
                            "insurer_vertical": row.get("insurer_vertical") or None,
                            "insurance_company": str(row.get("insurance_company", "")).strip(),
                            "product": product_obj,
                            "sub_product": sub_product_obj,
                            "policy_type": policy_type_obj,
                            "vehicle_age_min": float(row.get("vehicle_age_min") or 0),
                            "vehicle_age_max": float(row.get("vehicle_age_max") or 0),
                            "make_model_class": mmc_obj,
                            "fuel_type": fuel_type_obj,
                            "pi_od_rate": float(row.get("pi_od_rate") or 0),
                            "pi_tp_rate": float(row.get("pi_tp_rate") or 0),
                            "pi_tp_2": float(row.get("pi_tp_2") or 0),
                            "pi_tp_3": float(row.get("pi_tp_3") or 0),
                            "pi_tp_4": float(row.get("pi_tp_4") or 0),
                            "pi_tp_5": float(row.get("pi_tp_5") or 0),
                            "pi_net_rate": float(row.get("pi_net_rate") or 0),
                            "pi_flat_amount": float(row.get("pi_flat_amount") or 0),
                            "pi_vli": float(row.get("pi_vli") or 0),
                            "pi_type": row.get("pi_type") or None,
                            "tariff_min": float(row.get("tariff_min") or 0),
                            "tariff_max": float(row.get("tariff_max") or 0),
                            "is_ncb": is_ncb_obj,
                            "is_cpa": is_cpa_obj,
                            "is_zd": is_zd_obj,
                            "cc_min": float(row.get("cc_min") or 0),
                            "cc_max": float(row.get("cc_max") or 0),
                            "from_date": parse_date(row.get("from_date")),
                            "to_date": parse_date(row.get("to_date")),
                            "sc_min": float(row.get("sc_min") or 0),
                            "sc_max": float(row.get("sc_max") or 0),
                            "user_id": int(float(row.get("user_id") or 0)) if row.get("user_id") else None,
                            "veh_use": row.get("veh_use") or None,
                            "add_tnc": row.get("add_tnc") or None,
                            "remarks": row.get("remarks") or None,
                            "po_type": row.get("po_type") or None,
                            "po_od_rate": float(row.get("po_od_rate") or 0),
                            "po_tp_rate": float(row.get("po_tp_rate") or 0),
                            "po_net_rate": float(row.get("po_net_rate") or 0),
                            "po_flat_amount": float(row.get("po_flat_amount") or 0),
                            # Must match the hardcoded values the RateMaster(...)
                            # call below actually saves for a new upload, so the
                            # grouping hash reflects what the row really ends up
                            # with rather than always hashing an empty string here.
                            "status": "INACTIVE",
                            "is_deleted": "NO",
                            # Every upload gets its own groups, even if the
                            # content is byte-for-byte identical to something
                            # already in the database -- upload_batch_id is
                            # unique per click of the Upload button (shared
                            # only by chunks of that same file), so it never
                            # matches a different upload, past or future.
                            "upload_batch": upload_batch_id,
                        }

                        # Hashed even during dry_run, so a row that would fail
                        # here -- it can't, build_key_hash is pure computation --
                        # is still exercised by the validation pass. Group
                        # lookup/creation happens in a second pass below, batched
                        # across the whole chunk instead of per row.
                        key_hash, key_text = build_key_hash(cleaned)
                        if dry_run:
                            continue
                        row_records.append({
                            "row": row, "cleaned": cleaned, "key_hash": key_hash, "key_text": key_text,
                            "product_obj": product_obj, "sub_product_obj": sub_product_obj,
                            "policy_type_obj": policy_type_obj, "fuel_type_obj": fuel_type_obj, "mmc_obj": mmc_obj,
                            "is_ncb_obj": is_ncb_obj, "is_cpa_obj": is_cpa_obj, "is_zd_obj": is_zd_obj,
                        })

                    # Batched for the same reason existing_active_keys above is: a
                    # get_or_create() and a RateMaster.objects.filter(group=...) per
                    # *distinct group* in the chunk -- not per row, but rate cards
                    # routinely carry hundreds of distinct rate combinations
                    # (age/fuel/cc/tariff bands) per chunk, which was still enough
                    # individual round trips to blow the gunicorn worker timeout and
                    # get the worker SIGKILLed mid-request (seen in production on
                    # /api/upload-chunk/).
                    #
                    # Deliberately scoped to only the hashes this chunk's own rows
                    # need (not RateGroup.objects.all()) -- upload_batch_id is part
                    # of the hash, so a new upload must never silently attach its
                    # rows to a group left over from a past, unrelated upload just
                    # because the content hash happens to match. Any genuine
                    # cross-upload duplicates get consolidated later, deliberately,
                    # by regroup_rate_master.py instead of invisibly at import time.
                    needed_hashes = {rec["key_hash"] for rec in row_records}
                    existing_groups = {g.key_hash: g for g in RateGroup.objects.filter(key_hash__in=needed_hashes)}
                    missing_hashes = needed_hashes - existing_groups.keys()
                    if missing_hashes:
                        hash_to_text = {}
                        for rec in row_records:
                            hash_to_text.setdefault(rec["key_hash"], rec["key_text"])
                        RateGroup.objects.bulk_create(
                            [RateGroup(key_hash=h, key_text=hash_to_text[h]) for h in missing_hashes],
                            ignore_conflicts=True,
                        )
                        # ignore_conflicts rows don't come back with a pk, and a
                        # concurrent request could have created one of these hashes
                        # in the gap above -- refetch rather than assume our own
                        # bulk_create is what's now there.
                        existing_groups.update(
                            {g.key_hash: g for g in RateGroup.objects.filter(key_hash__in=missing_hashes)}
                        )

                    # group_obj.id -> set of new_rto_list values already present for
                    # that group, whether already committed to the DB (an earlier
                    # chunk of this same upload, or a retried request resending a
                    # chunk that already succeeded) or staged earlier in this same
                    # chunk. Preloaded for every group this chunk touches in one
                    # query -- see the skip check below, which is what makes a row
                    # upload idempotent instead of blindly inserting a fresh copy
                    # every time identical content is seen.
                    existing_rto_by_group = defaultdict(set)
                    group_ids = {g.id for g in existing_groups.values()}
                    for gid, rto in RateMaster.objects.filter(group_id__in=group_ids).values_list("group_id", "new_rto_list"):
                        existing_rto_by_group[gid].add(rto)

                    for rec in row_records:
                        row = rec["row"]
                        cleaned = rec["cleaned"]
                        product_obj = rec["product_obj"]
                        sub_product_obj = rec["sub_product_obj"]
                        policy_type_obj = rec["policy_type_obj"]
                        fuel_type_obj = rec["fuel_type_obj"]
                        mmc_obj = rec["mmc_obj"]
                        is_ncb_obj = rec["is_ncb_obj"]
                        is_cpa_obj = rec["is_cpa_obj"]
                        is_zd_obj = rec["is_zd_obj"]
                        group_obj = existing_groups[rec["key_hash"]]

                        # Cross-upload duplicate guard: an ACTIVE, non-deleted row
                        # with this exact content (same business terms *and* RTO)
                        # already existing means some *past* upload already put
                        # this live rate in the system -- re-uploading it now (e.g.
                        # exporting a grid and re-importing it unchanged) would
                        # otherwise create a second, parallel group carrying the
                        # same rate rather than recognizing it's already covered.
                        # Only checked against ACTIVE/NO rows, not INACTIVE or
                        # soft-deleted ones -- re-introducing a rate that was
                        # deliberately turned off is a legitimate thing to upload.
                        dup_key = (
                            cleaned["insurance_company"], cleaned["insurer_vertical"], cleaned["new_vehicle_makes"], row.get("new_rto_list") or None,
                            product_obj.id if product_obj else None,
                            sub_product_obj.id if sub_product_obj else None,
                            policy_type_obj.id if policy_type_obj else None,
                            fuel_type_obj.id if fuel_type_obj else None,
                            mmc_obj.id if mmc_obj else None,
                            cleaned["vehicle_age_min"], cleaned["vehicle_age_max"],
                            cleaned["pi_od_rate"], cleaned["pi_tp_rate"], cleaned["pi_tp_2"], cleaned["pi_tp_3"], cleaned["pi_tp_4"], cleaned["pi_tp_5"],
                            cleaned["pi_net_rate"], cleaned["pi_flat_amount"], cleaned["pi_vli"], cleaned["pi_type"],
                            cleaned["tariff_min"], cleaned["tariff_max"],
                            is_ncb_obj.id, is_cpa_obj.id, is_zd_obj.id,
                            cleaned["cc_min"], cleaned["cc_max"], cleaned["from_date"], cleaned["to_date"], cleaned["sc_min"], cleaned["sc_max"], cleaned["add_tnc"],
                        )
                        if dup_key in existing_active_keys:
                            continue

                        # Within one group, new_rto_list is what distinguishes one
                        # rate row from another -- every other field is already
                        # identical by construction (that's what put them in the
                        # same group_hash). So a second row with the same
                        # new_rto_list in the same group is never a legitimate
                        # second rate rule, only a duplicate: the same source row
                        # appearing twice in the file, the same chunk arriving
                        # twice (client retry), or the whole upload being resent.
                        # Skip it instead of inserting another exact copy.
                        row_rto = row.get("new_rto_list") or None
                        if row_rto in existing_rto_by_group[group_obj.id]:
                            continue
                        existing_rto_by_group[group_obj.id].add(row_rto)

                        instances_to_create.append(
                            RateMaster(
                                group=group_obj,
                                status="INACTIVE",
                                is_deleted="NO",
                                new_vehicle_makes=cleaned["new_vehicle_makes"],
                                new_rto_list=row.get("new_rto_list") or None,
                                insurer_vertical=cleaned["insurer_vertical"],
                                insurance_company=cleaned["insurance_company"],
                                product=product_obj,
                                sub_product=sub_product_obj,
                                policy_type=policy_type_obj,
                                fuel_type=fuel_type_obj,
                                make_model_class=mmc_obj,
                                vehicle_age_min=cleaned["vehicle_age_min"],
                                vehicle_age_max=cleaned["vehicle_age_max"],
                                pi_od_rate=cleaned["pi_od_rate"],
                                pi_tp_rate=cleaned["pi_tp_rate"],
                                pi_tp_2=cleaned["pi_tp_2"],
                                pi_tp_3=cleaned["pi_tp_3"],
                                pi_tp_4=cleaned["pi_tp_4"],
                                pi_tp_5=cleaned["pi_tp_5"],
                                pi_net_rate=cleaned["pi_net_rate"],
                                pi_flat_amount=cleaned["pi_flat_amount"],
                                pi_vli=cleaned["pi_vli"],
                                pi_type=cleaned["pi_type"],
                                tariff_min=cleaned["tariff_min"],
                                tariff_max=cleaned["tariff_max"],
                                is_ncb=is_ncb_obj,
                                is_cpa=is_cpa_obj,
                                is_zd=is_zd_obj,
                                cc_min=cleaned["cc_min"],
                                cc_max=cleaned["cc_max"],
                                from_date=cleaned["from_date"],
                                to_date=cleaned["to_date"],
                                user_id=cleaned["user_id"],
                                sc_min=cleaned["sc_min"],
                                sc_max=cleaned["sc_max"],
                                veh_use=cleaned["veh_use"],
                                add_tnc=cleaned["add_tnc"],
                                remarks=cleaned["remarks"],
                                po_type=cleaned["po_type"],
                                po_od_rate=cleaned["po_od_rate"],
                                po_tp_rate=cleaned["po_tp_rate"],
                                po_net_rate=cleaned["po_net_rate"],
                                po_flat_amount=cleaned["po_flat_amount"],
                            )
                        )

                    if dry_run:
                        inserted = len(rows)
                    else:
                        RateMaster.objects.bulk_create(instances_to_create, ignore_conflicts=True)
                        inserted = len(instances_to_create)

                elif target_table == 'health_rate_master':
                    for row in rows:
                        insurance_company = (row.get("insurance_company") or "").strip()
                        if not insurance_company:
                            raise ValueError("insurance_company is blank")

                        cleaned = {
                            "insurance_company": insurance_company,
                            "product_name": (row.get("product_name") or "").strip() or None,
                            "policy_category": (row.get("policy_category") or "").strip() or None,
                            "plan_names": (row.get("plan_names") or "").strip() or None,
                            "business_type": (row.get("business_type") or "").strip() or None,
                            "min_deductible": parse_health_number(row.get("min_deductible")),
                            "max_deductible": parse_health_number(row.get("max_deductible")),
                            "min_sum_insured": parse_health_number(row.get("min_sum_insured")),
                            "max_sum_insured": parse_health_number(row.get("max_sum_insured")),
                            "min_age": parse_health_number(row.get("min_age")),
                            "max_age": parse_health_number(row.get("max_age")),
                            "pincode_zone": (row.get("pincode_zone") or "").strip() or None,
                            "from_date": parse_health_date(row.get("from_date")),
                            "to_date": parse_health_date(row.get("to_date")),
                            "payin_rate": parse_health_percent(row.get("payin_rate")),
                            "one_year_rate": parse_health_percent(row.get("one_year_rate")),
                            "multi_year_2_rate": parse_health_percent(row.get("multi_year_2_rate")),
                            "multi_year_3_rate": parse_health_percent(row.get("multi_year_3_rate")),
                            "multi_year_4_rate": parse_health_percent(row.get("multi_year_4_rate")),
                            "multi_year_5_rate": parse_health_percent(row.get("multi_year_5_rate")),
                        }

                        # status/is_deleted/remarks are optional columns — only
                        # touch them on update if this row actually provided a
                        # value, so a re-upload that omits them doesn't clobber
                        # manual edits made afterwards in the UI (upsert_health_
                        # rate_row only defaults them ACTIVE/NO on creation).
                        status_val = (row.get("status") or "").strip().upper()
                        if status_val:
                            if status_val not in dict(HealthRateMaster.STATUS_CHOICES):
                                raise ValueError(f"'{status_val}' is not a valid Status (ACTIVE or INACTIVE).")
                            cleaned["status"] = status_val
                        is_deleted_val = (row.get("is_deleted") or "").strip().upper()
                        if is_deleted_val:
                            if is_deleted_val not in dict(HealthRateMaster.IS_DELETED_CHOICES):
                                raise ValueError(f"'{is_deleted_val}' is not a valid Is Deleted value (YES or NO).")
                            cleaned["is_deleted"] = is_deleted_val
                        remarks_val = (row.get("remarks") or "").strip()
                        if remarks_val:
                            cleaned["remarks"] = remarks_val

                        if not dry_run:
                            upsert_health_rate_row(cleaned)
                        inserted += 1

                else:
                    return JsonResponse({'status': 'error', 'message': 'Invalid table selected'}, status=400)

            return JsonResponse({'status': 'success', 'inserted': inserted})

        except Exception as e:
            logger.exception("Chunk upload failed")
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

    return JsonResponse({'status': 'error', 'message': 'Method not allowed'}, status=405)
