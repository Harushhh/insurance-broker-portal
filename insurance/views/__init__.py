# ruff: noqa: F401
"""The views package.

One module per feature (rate_master.py, masters.py, policy_lock.py, ...). The logic those views share lives in
insurance/services/, forms in insurance/forms.py, the DRF classes in insurance/api/views.py, the page-permission
rules in insurance/access.py and constants several modules agree on in insurance/constants.py.

This file re-exports every name that used to live in the single views.py, so `views.<name>` in urls.py and
imports such as `from insurance.views import RateForm` keep working unchanged.
"""

# Defined in views.py before they moved to their own modules.
from ..access import PAGE_GROUPS, RATE_CHECKER_GROUPS, RATE_CHECKER_TABS
from ..constants import (
    GRID_INSURER_CHOICES,
    HEALTH_POLICY_TERM_FIELD,
    POINTS_SEARCH_ACTIONS,
    SECURITY_AUDIT_LOG_ACTIONS,
)

from ..api.views import (
    ExportRatesAPIView,
    HealthPayoutRatesAPIView,
    IssueSSOTicketAPIView,
    MakeModelMasterAPIView,
    PARTNER_ARHAMSECURE_ALLOWED_GROUPS,
    PolicyLockCheckerAPIView,
    SSO_SCOPE_BUNDLES,
)
from ..forms import (
    HealthRateForm,
    MakeModelForm,
    PincodeForm,
    PolicyDocumentUploadForm,
    RATE_DECIMAL_FIELDS,
    RTOForm,
    RateForm,
)
from ..services.analysis import (
    EVERGREEN,
    MONTH_NAMES,
    MONTH_TOKEN_RE,
    STATE_CODE_ALIASES,
    _build_rto_state_map,
    _row_states_and_months,
    _track_min_max,
    month_bucket_for,
    month_label_for,
)
from ..services.audit_logs import (
    _resolve_points_log_param,
)
from ..services.common import (
    _JSON_SCRIPT_ESCAPES,
    normalize,
    parse_date,
    parse_yes_no_na,
    safe_json_for_script,
    split_csv_values,
    unique_join,
)
from ..services.email import (
    send_email_background,
)
from ..services.health_payout import (
    HEALTH_ABOVE_25L_THRESHOLD,
    HEALTH_PAYOUT_MAX_RESULTS,
    _build_health_payout_queryset,
    _run_health_payout_search,
)
from ..services.make_model import (
    NA_MAKE_MODEL_MAP,
    apply_make_model_filter,
    get_dynamic_make_model_class_list,
    get_make_mapping_context,
    get_translated_make_model,
    resolve_make_model_groups,
    strict_match_in_cluster,
)
from ..services.make_model_mapping import (
    MAKE_MODEL_MAPPING_SUGGEST_CUTOFF,
    MAKE_MODEL_MAPPING_SUGGEST_LIMIT,
    _make_model_cluster_items,
    _make_model_mapping_suggestions,
    _mapping_lookup,
    _normalize_mapping_value,
)
from ..services.masters import (
    RTO_MAKE_CHOICES_CACHE_KEY,
    RTO_MAKE_CHOICES_CACHE_TTL,
    _sync_pincode_zones_from_health_data,
    get_rto_and_make_choices,
)
from ..services.mis_payout import (
    get_rule_schema_context,
)
from ..services.missing_make_model import (
    MISSING_MM_KEY_INSURER,
    MISSING_MM_KEY_MAKE,
    MISSING_MM_KEY_MODEL,
    MISSING_MM_KEY_PRODUCT,
    MISSING_MM_KEY_SUB_PRODUCT,
    MISSING_MM_KEY_VEHICLE_CLASS,
    MISSING_MM_RESOLVED_PREVIEW,
    _active_vehicle_make_scope,
    _annotate_make_model_resolution,
    _append_make_model_cluster_item,
    _filter_missing_make_model,
    _missing_make_model_groups,
    _missing_make_model_manual_key_from_post,
    _normalized_payload,
)
from ..services.motor_payout import (
    MOTOR_PAYOUT_MAX_RESULTS,
    _collect_motor_payout_rows,
)
from ..services.overlap import (
    OVERLAP_RULES,
    OVERLAP_RULES_MAP,
    OVERLAP_SCAN_STALE_AFTER,
    _active_overlap_scan,
    _active_rate_master_insurers,
    _active_rate_master_products,
    _fmt_axis_side,
    _fmt_bound,
    _latest_overlap_scan,
    _overlap_counts,
    _rate_group_rows,
    _scan_scope_label,
)
from ..services.points_audit import (
    _log_health_points_search,
    _log_motor_points_search,
)
from ..services.policy_documents import (
    process_policy_document,
    safe_date_multi,
    safe_decimal,
)
from ..services.policy_lock import (
    _run_policy_lock_checker_search,
)
from ..services.rate_master import (
    GROUP_FIELDS,
    _is_valid_range_number,
    apply_range_filter,
    build_key_hash,
)
from ..services.rate_master_checks import (
    EQUALITY_ERROR_RULES,
    EQUALITY_ERROR_RULES_MAP,
    PI_PO_ERROR_RULES,
    PI_PO_ERROR_RULES_MAP,
    PI_RATE_FIELDS,
    PI_TYPE_RULES,
    PI_TYPE_RULES_MAP,
    POLICY_DETAILS_SUMMARY_LIMIT,
    POLICY_DETAILS_SUMMARY_TARGETS,
    RANGE_ERROR_RULES,
    RANGE_ERROR_RULES_MAP,
    RATE_MASTER_PI_PO_MARGIN,
    STP_SUB_PRODUCT,
    STP_TARIFF_MAX,
    _rate_master_equality_error_counts,
    _rate_master_equality_violations_qs,
    _rate_master_grid_summary_qs,
    _rate_master_min_max_violations_qs,
    _rate_master_pi_po_error_counts,
    _rate_master_pi_po_rate_violations_qs,
    _rate_master_pi_po_type_violations_qs,
    _rate_master_pi_type_error_counts,
    _rate_master_pi_type_violations_qs,
    _rate_master_range_error_counts,
    _rate_master_tariff_violations_qs,
    _split_payload_summary,
    _sync_unsynced_mis_files,
)
from ..services.tickets import (
    MOTOR_PAYOUT_PARAM_NAMES,
    TICKET_TO_MOTOR_PAYOUT_PARAMS,
    _build_motor_payout_rates_url,
)
from .ai_rulebook import (
    delete_field,
    edit_field,
    field_configurator,
)
from .analysis import (
    MONTH_ORDER,
    RATE_FIELDS,
    _filtered_rate_master_qs,
    business_analysis,
)
from .audit_logs import (
    _filtered_audit_logs,
    audit_logs,
    export_audit_log_xlsx,
    export_points_audit_logs_xlsx,
    points_audit_logs,
)
from .grid_management import (
    grid_management,
)
from .health_payout_rates import (
    HEALTH_SUM_INSURED_OPTIONS,
    health_payout_rates,
)
from .health_rate_master import (
    ALLOWED_HEALTH_BULK_UPDATE_FIELDS,
    HEALTH_FLOAT_FIELDS,
    HEALTH_RATE_BATCH_SIZE,
    HEALTH_RATE_FIELD_NAMES,
    _build_health_rate_queryset,
    export_health_rates_xlsx,
    health_rate_master,
    health_rate_master_bulk_update,
    health_rate_master_edit,
)
from .home import (
    home_dashboard,
)
from .make_model_mapping import (
    MAKE_MODEL_MAPPING_MAX_VALUE_LEN,
    MAKE_MODEL_MAPPING_VALUES_LIMIT,
    delete_make_model_mapping,
    make_model_mapping,
    make_model_mapping_list,
    make_model_mapping_suggest,
    make_model_mapping_values,
    save_make_model_mapping,
)
from .masters import (
    MAKE_MODEL_DASHBOARD_DEFAULT_LIMIT,
    PINCODE_DASHBOARD_DEFAULT_LIMIT,
    RTO_DASHBOARD_DEFAULT_LIMIT,
    _filtered_make_model_qs,
    _filtered_pincode_qs,
    _filtered_rto_qs,
    create_make_model,
    create_pincode,
    create_rto,
    edit_make_model,
    edit_pincode,
    edit_rto,
    export_make_model_xlsx,
    export_pincode_xlsx,
    export_rto_xlsx,
    make_model_dashboard,
    make_model_dashboard_search,
    pincode_dashboard,
    pincode_dashboard_search,
    rto_dashboard,
    rto_dashboard_search,
)
from .mis_payout import (
    add_mis_mapping,
    cancel_mis_processing,
    delete_mis_mapping,
    download_processed_mis,
    edit_mis_mapping,
    mis_mapping_dashboard,
    mis_payout_automation,
)
from .missing_make_model import (
    MISSING_MAKE_MODEL_BATCH_SIZE,
    MISSING_MM_FILTER_KEYS,
    MISSING_MM_MAX_VALUE_LEN,
    _missing_make_model_redirect,
    _missing_make_model_selection,
    add_missing_make_model_to_master,
    bulk_add_missing_make_model_to_master,
    bulk_mark_missing_make_model_resolved,
    export_missing_make_model_xlsx,
    mark_missing_make_model_resolved,
    missing_make_model,
    unmark_missing_make_model_resolved,
)
from .motor_payout_rates import (
    MOTOR_PAYOUT_BATCH_SIZE,
    MOTOR_PAYOUT_FIELD_NAMES,
    _build_motor_payout_queryset,
    motor_payout_rates,
    motor_payout_rates_more,
)
from .ocr_pipeline import (
    mis_review,
    my_mis,
    upload_extract_pdf,
)
from .policy_lock import (
    lock_unlock_policy,
    locked_policy_dashboard,
    policy_lock_checker,
)
from .rate_checker import (
    rate_checker_entry,
)
from .rate_master import (
    ALLOWED_BULK_UPDATE_FIELDS,
    DASHBOARD_BATCH_SIZE,
    DASHBOARD_PAGE_SIZE_OPTIONS,
    RATE_STATUS_UI_TO_DB,
    bulk_update_rates,
    dashboard,
    edit_rate,
    export_rates_xlsx,
    get_cluster_details,
    update_cluster_details,
)
from .rate_master_health import (
    MIS_HEALTH_BATCH_SIZE,
    _overlap_redirect,
    deactivate_rate_group,
    export_grid_summary_xlsx,
    export_overlap_pairs_xlsx,
    rate_master_health,
    start_overlap_scan,
)
from .special_rates import (
    special_rate_requests,
    special_rate_requests_review,
    update_special_rate_request_status,
)
from .sso_handoff import (
    SSO_ALLOWED_LANDING_PAGES,
    sso_consume_view,
)
from .tickets import (
    create_ticket_api,
    ticket_dashboard,
    update_ticket_status,
)
from .upload import (
    api_check_duplicate_upload,
    api_complete_upload,
    api_upload_chunk,
    import_data_view,
)
from .user_management import (
    HIERARCHY_FIELDS,
    HIERARCHY_FIELD_LABELS,
    HIERARCHY_FLAGS,
    user_management,
)
