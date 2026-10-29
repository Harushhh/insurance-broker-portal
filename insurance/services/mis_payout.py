"""Schema context for the MIS mapping configuration form."""


# --- JSON Schema injected dynamically into the Dropdowns for cross-table builder ---
def get_rule_schema_context():
    return {
        'MIS_File': [], 
        'RateMaster': [
            'insurance_company', 'product', 'sub_product', 'policy_type', 
            'fuel_type', 'make_model_class', 'new_vehicle_makes', 'new_rto_list', 
            'cc_range', 'sc_range', 'age_range', 'date_range', 
            'is_ncb', 'is_cpa', 'is_zd'
        ]
    }
