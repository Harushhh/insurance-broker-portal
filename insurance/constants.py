"""Values that more than one module needs to agree on.

Each of these used to be written out twice (views.py and tasks.py, or views.py
and mapping_engine.py) with a comment asking the next editor to keep the copies
in sync. They live here once now.
"""

# Action types shown on the unified Points Search Logs page (Motor + Health
# "Check Eligibility" searches). The 7-day purge task deletes exactly these.
POINTS_SEARCH_ACTIONS = ["MOTOR_POINTS_SEARCH", "HEALTH_POINTS_SEARCH"]

# Action types shown on the Security Audit & History Log page - edit/update
# actions only. MOTOR_POINTS_SEARCH and the sso_ticket_* events have their own
# purposes and are never shown there. The 7-day purge task deletes exactly these.
SECURITY_AUDIT_LOG_ACTIONS = [
    "MANUAL EDIT", "BULK UPDATE", "HEALTH RATE EDIT", "HEALTH BULK UPDATE",
    "OVERLAP DEACTIVATE",
]

# Which HealthRateMaster rate column a policy term (in years) reads its payout
# rate from. The MIS mapping engine keys it by the computed tenure (int); the
# Health Payout Rates page keys it by the "N Yr Policy" radio value (str).
HEALTH_TENURE_RATE_COLUMNS = {
    1: "one_year_rate",
    2: "multi_year_2_rate",
    3: "multi_year_3_rate",
    4: "multi_year_4_rate",
    5: "multi_year_5_rate",
}
HEALTH_POLICY_TERM_FIELD = {str(term): column for term, column in HEALTH_TENURE_RATE_COLUMNS.items()}

# Insurer names offered on the Grid Management page; grid_archive.py reads the
# same list to label archived uploads.
GRID_INSURER_CHOICES = [
    "ACKO GENERAL INSURANCE LIMITED",
    "BAJAJ ALLIANZ GENERAL INSURANCE CO LTD",
    "CHOLAMANDALAM MS GENERAL INSURANCE COMPANY LTD",
    "FUTURE GENERALI INDIA INSURANCE COMPANY LTD",
    "GO DIGIT GENERAL INSURANCE LTD",
    "HDFC ERGO GENERAL INSURANCE CO LTD",
    "ICICI LOMBARD GENERAL INSURANCE CO LTD",
    "IFFCO-TOKIO GENERAL INSURANCE CO LTD",
    "INDUSIND GENERAL INSURANCE COMPANY LIMITED",
    "KIWI GENERAL INSURANCE LIMITED",
    "ZURICH KOTAK GENERAL INSURANCE COMPANY (I) LIMITED",
    "KSHEMA GENERAL INSURANCE COMPANY LIMITED",
    "LIBERTY GENERAL INSURANCE LTD",
    "MAGMA HDI GENERAL INSURANCE CO LTD",
    "NATIONAL INSURANCE CO LTD",
    "RAHEJA QBE GENERAL INSURANCE CO LTD",
    "RELIANCE GENERAL INSURANCE CO LTD",
    "ROYAL SUNDARAM GENERAL INSURANCE COMPANY LIMITED",
    "SBI GENERAL INSURANCE COMPANY LIMITED",
    "SHRIRAM GENERAL INSURANCE COMPANY LTD",
    "TATA AIG GENERAL INSURANCE CO LTD",
    "THE NEW INDIA ASSURANCE CO LTD",
    "THE ORIENTAL INSURANCE CO LTD",
    "UNITED INDIA INSURANCE CO LTD",
    "UNIVERSAL SOMPO GENERAL INSURANCE CO LTD",
    "ZUNO GENERAL INSURANCE LTD",
]
