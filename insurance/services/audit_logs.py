"""Parameter resolution for the points-search audit log page."""


def _resolve_points_log_param(flat_params, key, id_to_name):
    """Same numeric-id-to-name resolution points_audit_logs applies per row,
    factored out so the XLSX export (which has no display-side row cap) uses
    identical logic instead of drifting from the on-page table."""
    val = flat_params.get(key)
    if val and str(val).isdigit():
        return id_to_name.get(int(val), val)
    return val
