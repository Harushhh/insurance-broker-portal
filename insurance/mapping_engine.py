import pandas as pd
import io
from django.core.files.base import ContentFile
from django.utils import timezone
from .models import MISFile, MappingConfiguration, RateMaster

def process_mis_mapping(mis_file_id):
    mis_obj = MISFile.objects.get(id=mis_file_id)
    mis_obj.status = 'PROCESSING'
    mis_obj.save(update_fields=['status'])

    try:
        # 1. Read MIS file (Supports both CSV and Excel seamlessly)
        file_ext = mis_obj.uploaded_file.name.split('.')[-1].lower()
        if file_ext == 'csv':
            df_mis = pd.read_csv(mis_obj.uploaded_file.path)
        else:
            df_mis = pd.read_excel(mis_obj.uploaded_file.path)
        
        # Keep track of original row order so we don't drop unmatched rows
        df_mis['Original_Row_ID'] = range(len(df_mis))

        mappings = MappingConfiguration.objects.filter(is_active=True)
        
        mis_cols_exact = []
        grid_cols_exact = []
        
        contains_mappings = [] # To hold ('mis_col', 'grid_col') for CONTAINS logic
        
        # Special variables to track range-based columns
        range_cc_col = None
        range_sc_col = None
        range_date_col = None
        range_age_col = None

        # 2. Separate Mappings by Type
        for m in mappings:
            if m.mis_column_name in df_mis.columns:
                if m.mapping_type == 'EXACT':
                    mis_cols_exact.append(m.mis_column_name)
                    grid_cols_exact.append(m.grid_field_name)
                elif m.mapping_type == 'CONTAINS':
                    contains_mappings.append((m.mis_column_name, m.grid_field_name))
                elif m.mapping_type == 'RANGE_CC':
                    range_cc_col = m.mis_column_name
                elif m.mapping_type == 'RANGE_SC':
                    range_sc_col = m.mis_column_name
                elif m.mapping_type == 'RANGE_DATE':
                    range_date_col = m.mis_column_name
                elif m.mapping_type == 'RANGE_AGE':
                    range_age_col = m.mis_column_name

        if not mis_cols_exact:
            raise ValueError("No EXACT match columns configured/found to perform the initial logic join.")

        # 3. Fetch Grid Data (Base output columns + dynamically mapped columns)
        fetch_fields = [
            'id', 'insurance_company', 'po_type', 'po_od_rate', 'po_tp_rate', 
            'po_net_rate', 'po_flat_amount', 'tariff_min', 'tariff_max', 
            'product__name', 'sub_product__name', 'cc_min', 'cc_max', 
            'sc_min', 'sc_max', 'from_date', 'to_date', 'vehicle_age_min', 'vehicle_age_max'
        ]
        
        for m in mappings:
            # We map directly to related table names for clean merging
            field_to_fetch = m.grid_field_name
            if field_to_fetch == 'product': field_to_fetch = 'product__name'
            if field_to_fetch == 'sub_product': field_to_fetch = 'sub_product__name'
            
            if field_to_fetch not in fetch_fields:
                fetch_fields.append(field_to_fetch)
                
            # Replace in our lists so pandas merges correctly
            if m.mapping_type == 'EXACT':
                idx = grid_cols_exact.index(m.grid_field_name)
                grid_cols_exact[idx] = field_to_fetch
            elif m.mapping_type == 'CONTAINS':
                idx = contains_mappings.index((m.mis_column_name, m.grid_field_name))
                contains_mappings[idx] = (m.mis_column_name, field_to_fetch)

        qs = RateMaster.objects.filter(status="ACTIVE", is_deleted="NO").values(*fetch_fields)
        df_grid = pd.DataFrame.from_records(qs)
        
        if df_grid.empty:
            raise ValueError("Grid model (RateMaster) has no active records.")

        # 4. Standardize text types for Exact matching
        for col in mis_cols_exact:
            df_mis[col] = df_mis[col].astype(str).str.strip().str.lower()
        for col in grid_cols_exact:
            df_grid[col] = df_grid[col].fillna("").astype(str).str.strip().str.lower()

        # 5. Standardize numeric/date types for Range matching
        if range_cc_col:
            df_mis[range_cc_col] = pd.to_numeric(df_mis[range_cc_col], errors='coerce')
            df_grid['cc_min'] = pd.to_numeric(df_grid['cc_min'], errors='coerce').fillna(0)
            df_grid['cc_max'] = pd.to_numeric(df_grid['cc_max'], errors='coerce').fillna(999999)

        if range_sc_col:
            df_mis[range_sc_col] = pd.to_numeric(df_mis[range_sc_col], errors='coerce')
            df_grid['sc_min'] = pd.to_numeric(df_grid['sc_min'], errors='coerce').fillna(0)
            df_grid['sc_max'] = pd.to_numeric(df_grid['sc_max'], errors='coerce').fillna(999999)
            
        if range_age_col:
            df_mis[range_age_col] = pd.to_numeric(df_mis[range_age_col], errors='coerce')
            df_grid['vehicle_age_min'] = pd.to_numeric(df_grid['vehicle_age_min'], errors='coerce').fillna(0)
            df_grid['vehicle_age_max'] = pd.to_numeric(df_grid['vehicle_age_max'], errors='coerce').fillna(99)

        if range_date_col:
            df_mis[range_date_col] = pd.to_datetime(df_mis[range_date_col], errors='coerce')
            df_grid['from_date'] = pd.to_datetime(df_grid['from_date'], errors='coerce')
            df_grid['to_date'] = pd.to_datetime(df_grid['to_date'], errors='coerce')

        # 6. Broadcast Merge (Links every possible exact match)
        df_merged = df_mis.merge(
            df_grid,
            left_on=mis_cols_exact,
            right_on=grid_cols_exact,
            how='left'
        )

        # 7. Apply Range Filters
        if range_cc_col:
            valid_match = df_merged['cc_min'].isna() | ((df_merged[range_cc_col] >= df_merged['cc_min']) & (df_merged[range_cc_col] <= df_merged['cc_max']))
            df_merged = df_merged[valid_match]

        if range_sc_col:
            valid_match = df_merged['sc_min'].isna() | ((df_merged[range_sc_col] >= df_merged['sc_min']) & (df_merged[range_sc_col] <= df_merged['sc_max']))
            df_merged = df_merged[valid_match]
            
        if range_age_col:
            valid_match = df_merged['vehicle_age_min'].isna() | ((df_merged[range_age_col] >= df_merged['vehicle_age_min']) & (df_merged[range_age_col] <= df_merged['vehicle_age_max']))
            df_merged = df_merged[valid_match]

        if range_date_col:
            valid_from = df_merged['from_date'].isna() | (df_merged[range_date_col] >= df_merged['from_date'])
            valid_to = df_merged['to_date'].isna() | (df_merged[range_date_col] <= df_merged['to_date'])
            valid_match = df_merged['po_net_rate'].isna() | (valid_from & valid_to)
            df_merged = df_merged[valid_match]
            
        # 8. Apply CONTAINS logic (e.g., MIS 'mumbai' inside Grid 'new_rto_list' -> 'mumbai, pune')
        for mis_col, grid_col in contains_mappings:
            def is_contained(row):
                if pd.isna(row.get(grid_col)) or pd.isna(row.get(mis_col)):
                    return True # If grid rule is blank, it applies to all. If MIS is blank, we don't drop.
                grid_val = str(row[grid_col]).strip().lower()
                mis_val = str(row[mis_col]).strip().lower()
                return mis_val in grid_val

            df_merged = df_merged[df_merged.apply(is_contained, axis=1)]

        # 9. Select the Best Rate Match (Prioritize Net -> OD -> Flat)
        df_merged['Calculated Rank'] = df_merged['po_net_rate'].fillna(df_merged['po_od_rate']).fillna(df_merged['po_flat_amount']).fillna(0)
        df_merged = df_merged.sort_values(by=['Calculated Rank'], ascending=False)
        df_merged = df_merged.drop_duplicates(subset=['Original_Row_ID'], keep='first')
        df_merged = df_merged.sort_values(by=['Original_Row_ID'])

        # 10. Generate Final Output matching user requirement specs
        df_final = df_mis.copy()
        
        # Set index to align the merge perfectly
        df_merged.set_index('Original_Row_ID', inplace=True)
        df_final.set_index('Original_Row_ID', inplace=True)

        df_final['Payout: insurance_company'] = df_merged['insurance_company']
        df_final['Payout: po_type'] = df_merged['po_type']
        df_final['Payout: po_od_rate'] = df_merged['po_od_rate']
        df_final['Payout: po_tp_rate'] = df_merged['po_tp_rate']
        df_final['Payout: po_net_rate'] = df_merged['po_net_rate']
        df_final['Payout: po_flat_amount'] = df_merged['po_flat_amount']
        df_final['Payout: tariff_min'] = df_merged['tariff_min']
        df_final['Payout: tariff_max'] = df_merged['tariff_max']
        df_final['Payout: product'] = df_merged['product__name']
        df_final['Payout: sub_product'] = df_merged['sub_product__name']
        
        # Placeholders for Custom Business Logic Columns
        df_final['Payout: One Year Policy'] = ""
        df_final['Payout: Multi Year Policy(2Y & 3Y)'] = ""
        
        # Clean up output
        df_final.reset_index(drop=True, inplace=True)

        # 11. Save output file
        output = io.BytesIO()
        if file_ext == 'csv':
            df_final.to_csv(output, index=False)
            new_filename = f"mapped_{original_filename.replace('.csv', '')}.csv"
        else:
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_final.to_excel(writer, index=False)
            new_filename = f"mapped_{original_filename.replace('.xlsx', '')}.xlsx"

        mis_obj.processed_file.save(new_filename, ContentFile(output.getvalue()))
        mis_obj.status = 'COMPLETED'
        mis_obj.processed_at = timezone.now()
        mis_obj.error_message = ""
        mis_obj.save()

    except Exception as e:
        print(f"\n❌ MIS Mapping Error: {str(e)}\n")
        mis_obj.status = 'FAILED'
        mis_obj.error_message = str(e)
        mis_obj.processed_at = timezone.now()
        mis_obj.save()