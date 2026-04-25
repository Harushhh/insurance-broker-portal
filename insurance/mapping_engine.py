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
        # 1. Read MIS file
        df_mis = pd.read_excel(mis_obj.uploaded_file.path)
        
        # Keep track of original row order so we don't drop unmatched rows
        df_mis['Original_Row_ID'] = range(len(df_mis))

        mappings = MappingConfiguration.objects.filter(is_active=True)
        
        mis_cols_exact = []
        grid_cols_exact = []
        
        # Special variables to track range-based columns
        range_cc_col = None
        range_sc_col = None
        range_date_col = None

        # 2. Separate Exact Matches from Range Matches
        for m in mappings:
            if m.mis_column_name in df_mis.columns:
                if m.grid_field_name == 'cc_range':
                    range_cc_col = m.mis_column_name
                elif m.grid_field_name == 'sc_range':
                    range_sc_col = m.mis_column_name
                elif m.grid_field_name == 'date_range':
                    range_date_col = m.mis_column_name
                else:
                    mis_cols_exact.append(m.mis_column_name)
                    grid_cols_exact.append(m.grid_field_name)

        if not mis_cols_exact:
            raise ValueError("No exact match columns found to perform the initial join.")

        # 3. Fetch Grid Data
        fetch_fields = grid_cols_exact + ['po_net_rate', 'po_od_rate', 'po_tp_rate']
        if range_cc_col: fetch_fields += ['cc_min', 'cc_max']
        if range_sc_col: fetch_fields += ['sc_min', 'sc_max']
        if range_date_col: fetch_fields += ['from_date', 'to_date']
        fetch_fields = list(set(fetch_fields)) # Remove duplicates

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

        if range_date_col:
            df_mis[range_date_col] = pd.to_datetime(df_mis[range_date_col], errors='coerce')
            df_grid['from_date'] = pd.to_datetime(df_grid['from_date'], errors='coerce')
            df_grid['to_date'] = pd.to_datetime(df_grid['to_date'], errors='coerce')

        # 6. Broadcast Merge (This links every possible exact match, creating a temporary Cartesian product)
        df_merged = df_mis.merge(
            df_grid,
            left_on=mis_cols_exact,
            right_on=grid_cols_exact,
            how='left'
        )

        # 7. Apply Range Filters (Drop the grids that fall outside the bounds)
        if range_cc_col:
            # Keep if CC is within bounds OR if there was no grid match at all (so we don't lose the row)
            valid_match = df_merged['cc_min'].isna() | ((df_merged[range_cc_col] >= df_merged['cc_min']) & (df_merged[range_cc_col] <= df_merged['cc_max']))
            df_merged = df_merged[valid_match]

        if range_sc_col:
            valid_match = df_merged['sc_min'].isna() | ((df_merged[range_sc_col] >= df_merged['sc_min']) & (df_merged[range_sc_col] <= df_merged['sc_max']))
            df_merged = df_merged[valid_match]

        if range_date_col:
            valid_from = df_merged['from_date'].isna() | (df_merged[range_date_col] >= df_merged['from_date'])
            valid_to = df_merged['to_date'].isna() | (df_merged[range_date_col] <= df_merged['to_date'])
            valid_match = df_merged['po_net_rate'].isna() | (valid_from & valid_to)
            df_merged = df_merged[valid_match]

        # 8. Calculate Rate
        df_merged['Calculated Rate'] = df_merged['po_net_rate'].fillna(df_merged['po_od_rate']).fillna(0)

        # 9. Cleanup: Sort by highest rate, drop duplicates to restore original row count, and restore original order
        df_merged = df_merged.sort_values(by=['Calculated Rate'], ascending=False)
        df_merged = df_merged.drop_duplicates(subset=['Original_Row_ID'], keep='first')
        df_merged = df_merged.sort_values(by=['Original_Row_ID'])

        # 10. Generate Final Output
        columns_to_keep = [col for col in df_mis.columns if col != 'Original_Row_ID'] + ['Calculated Rate']
        df_final = df_merged[columns_to_keep]

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df_final.to_excel(writer, index=False)

        original_filename = mis_obj.uploaded_file.name.split('/')[-1]
        new_filename = f"mapped_{original_filename}"

        mis_obj.processed_file.save(new_filename, ContentFile(output.getvalue()))
        mis_obj.status = 'COMPLETED'
        mis_obj.processed_at = timezone.now()
        mis_obj.error_message = ""
        mis_obj.save()

    except Exception as e:
        mis_obj.status = 'FAILED'
        mis_obj.error_message = str(e)
        mis_obj.processed_at = timezone.now()
        mis_obj.save()