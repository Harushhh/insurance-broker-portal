import pandas as pd
import io
import re
import difflib
from django.core.files.base import ContentFile
from django.utils import timezone
from django.db import connection
from .models import MISFile, RateMaster, RTOMaster, MakeModelMaster

def get_fuzzy_dict(source_list, target_list, threshold=0.6):
    """Rule 4: Fuzzy Insurer Mapping with >60% similarity threshold."""
    mapping = {}
    for src in source_list:
        if pd.isna(src) or not str(src).strip():
            mapping[src] = None
            continue
        src_str = str(src).strip().lower()
        best_match = None
        best_ratio = 0
        for tgt in target_list:
            if pd.isna(tgt) or not str(tgt).strip(): continue
            tgt_str = str(tgt).strip().lower()
            ratio = difflib.SequenceMatcher(None, src_str, tgt_str).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = tgt_str
        mapping[src] = best_match if best_ratio > threshold else None
    return mapping

def check_rto_fast(m_clean, g_str, rto_mapping):
    """Fast row-by-row RTO cluster containment check."""
    if pd.isna(g_str) or str(g_str).strip().lower() in ('', 'nan', 'none', 'na'): return True
    if not m_clean: return False
    
    for g_rto_name in str(g_str).lower().split(','):
        g_rto_name = g_rto_name.strip()
        if not g_rto_name: continue
        
        g_name_clean = re.sub(r'[^a-z0-9]', '', g_rto_name)
        if g_name_clean and (m_clean in g_name_clean or g_name_clean in m_clean):
            return True
            
        if g_rto_name in rto_mapping:
            cluster_str = rto_mapping[g_rto_name]
            if cluster_str and cluster_str != 'nan':
                for cluster_item in cluster_str.split(','):
                    c_clean = re.sub(r'[^a-z0-9]', '', cluster_item.strip())
                    if c_clean and (m_clean in c_clean or c_clean in m_clean):
                        return True
    return False

def check_make_fast(m_tokens, m_make_tokens, m_make_clean, m_raw_clean, g_str, make_mapping):
    """Fast row-by-row Make/Model >50% overlap and cluster check."""
    if pd.isna(g_str) or str(g_str).strip().lower() in ('', 'nan', 'none', 'na'): return True
    if not m_raw_clean: return False
    
    for g_make_name in str(g_str).lower().split(','):
        g_make_name = g_make_name.strip()
        if not g_make_name: continue
        
        if g_make_name in make_mapping:
            cluster_str = make_mapping[g_make_name]
            if cluster_str and cluster_str != 'nan':
                for cluster_item in cluster_str.split(','):
                    cluster_item = cluster_item.strip()
                    if not cluster_item: continue
                    
                    if cluster_item in m_raw_clean or m_raw_clean in cluster_item:
                        return True
                        
                    c_tokens = set(re.findall(r'[a-z0-9]+', cluster_item))
                    if c_tokens and m_tokens:
                        overlap = m_tokens.intersection(c_tokens)
                        if len(overlap) / len(c_tokens) > 0.5:
                            return True
        
        g_tokens = set(re.findall(r'[a-z0-9]+', g_make_name))
        if g_tokens and m_make_tokens:
            overlap = m_make_tokens.intersection(g_tokens)
            if len(overlap) / len(g_tokens) > 0.5:
                return True
                
        if m_make_clean and (m_make_clean in g_make_name or g_make_name in m_make_clean):
            return True
            
    return False

def find_col(df_columns, *targets):
    """Safely extracts mis columns whether 'Policy: ' prefix is used or not."""
    cols_clean = {c.lower().replace('policy: ', '').strip(): c for c in df_columns}
    for t in targets:
        t_clean = t.lower().replace('policy: ', '').strip()
        if t_clean in cols_clean:
            return cols_clean[t_clean]
        for c_clean, original in cols_clean.items():
            if t_clean in c_clean:
                return original
    return None

def safe_col(df, col_name, fill_val=None):
    if col_name and col_name in df.columns: return df[col_name]
    return pd.Series([fill_val] * len(df), index=df.index)

def process_mis_mapping(mis_file_id):
    # Always forcefully wipe the thread's DB connection state to prevent inheritance locks
    connection.close()
    
    try:
        mis_obj = MISFile.objects.get(id=mis_file_id)
        mis_obj.status = 'PROCESSING'
        mis_obj.save(update_fields=['status'])
    except Exception as init_err:
        print(f"Failed to initialize processing state: {init_err}")
        connection.close()
        return

    try:
        # 1. READ FILE
        file_ext = mis_obj.uploaded_file.name.split('.')[-1].lower()
        if file_ext == 'csv':
            df_mis = pd.read_csv(mis_obj.uploaded_file.path)
        else:
            df_mis = pd.read_excel(mis_obj.uploaded_file.path)
        
        # Safely convert column names, handling multi-index edge cases
        df_mis.columns = [str(col[0]).strip() if isinstance(col, tuple) else str(col).strip() for col in df_mis.columns]
        df_mis['Original_Row_ID'] = range(len(df_mis))
        mis_cols = df_mis.columns.tolist()

        # 2. FETCH CLUSTERS
        rto_qs = RTOMaster.objects.all().values('rto_name', 'rto_cluster')
        rto_mapping = {str(r['rto_name']).strip().lower(): str(r['rto_cluster']).strip().lower() for r in rto_qs if r['rto_name']}
        
        make_qs = MakeModelMaster.objects.all().values('make_model_name', 'make_model_cluster')
        make_mapping = {str(m['make_model_name']).strip().lower(): str(m['make_model_cluster']).strip().lower() for m in make_qs if m['make_model_name']}

        # 3. LOCATE UPLOADED COLUMNS
        c_cc = find_col(mis_cols, 'cc cubic capacity', 'cc')
        c_sc = find_col(mis_cols, 'seating capacity', 'sc')
        c_age = find_col(mis_cols, 'vehage', 'vehicle age')
        c_date = find_col(mis_cols, 'inception date', 'issue date')
        
        c_prod = find_col(mis_cols, 'vehproduct', 'product name')
        c_sub_prod = find_col(mis_cols, 'sub product')
        c_fuel = find_col(mis_cols, 'fuel')
        c_class = find_col(mis_cols, 'vehicle class', 'buss class')
        
        c_ins = find_col(mis_cols, 'insurance company', 'insurer')
        c_make = find_col(mis_cols, 'vehicle make', 'make')
        c_model = find_col(mis_cols, 'model')
        
        c_rto = find_col(mis_cols, 'rto no', 'rto city', 'rto')
        c_ncb = find_col(mis_cols, 'no claim bonus', 'ncb')
        c_cpa = find_col(mis_cols, 'cpa')
        c_zd = find_col(mis_cols, 'nil dep', 'zero dep')

        # 4. FETCH RATE MASTER
        fetch_fields = [
            'id', 'group_id', 'insurance_company', 'po_type', 'po_od_rate', 'po_tp_rate', 
            'po_net_rate', 'po_flat_amount', 'tariff_min', 'tariff_max', 
            'product__name', 'sub_product__name', 'fuel_type__name', 'make_model_class__name',
            'cc_min', 'cc_max', 'sc_min', 'sc_max', 'from_date', 'to_date', 
            'vehicle_age_min', 'vehicle_age_max', 'new_vehicle_makes', 'new_rto_list',
            'is_cpa__code', 'is_ncb__code', 'is_zd__code', 'add_tnc'
        ]
        qs = RateMaster.objects.filter(status="ACTIVE", is_deleted="NO").values(*fetch_fields)
        df_grid = pd.DataFrame(list(qs))
        if df_grid.empty: raise ValueError("RateMaster has no active records configured.")

        df_grid['_grid_ins'] = safe_col(df_grid, 'insurance_company', '').astype(str).str.strip().str.lower()
        df_grid['_grid_prod'] = safe_col(df_grid, 'product__name', '').astype(str).str.strip().str.lower()
        df_grid['_grid_sub'] = safe_col(df_grid, 'sub_product__name', '').astype(str).str.replace(r'\.0$', '', regex=True).str.strip().str.lower()
        df_grid['_grid_fuel'] = safe_col(df_grid, 'fuel_type__name', '').astype(str).str.strip().str.lower()
        df_grid['_grid_class'] = safe_col(df_grid, 'make_model_class__name', '').astype(str).str.strip().str.lower()
        
        df_grid['cc_min'] = pd.to_numeric(safe_col(df_grid, 'cc_min'), errors='coerce').fillna(0)
        df_grid['cc_max'] = pd.to_numeric(safe_col(df_grid, 'cc_max'), errors='coerce').fillna(999999)
        df_grid['sc_min'] = pd.to_numeric(safe_col(df_grid, 'sc_min'), errors='coerce').fillna(0)
        df_grid['sc_max'] = pd.to_numeric(safe_col(df_grid, 'sc_max'), errors='coerce').fillna(999999)
        df_grid['vehicle_age_min'] = pd.to_numeric(safe_col(df_grid, 'vehicle_age_min'), errors='coerce').fillna(0)
        df_grid['vehicle_age_max'] = pd.to_numeric(safe_col(df_grid, 'vehicle_age_max'), errors='coerce').fillna(99)
        df_grid['from_date'] = pd.to_datetime(safe_col(df_grid, 'from_date'), errors='coerce')
        df_grid['to_date'] = pd.to_datetime(safe_col(df_grid, 'to_date'), errors='coerce')

        # 5. PRE-COMPUTE MIS DATA
        if c_ins:
            fuzzy_ins_map = get_fuzzy_dict(safe_col(df_mis, c_ins, '').unique(), df_grid['_grid_ins'].unique(), threshold=0.6)
            df_mis['_mis_ins'] = safe_col(df_mis, c_ins).map(fuzzy_ins_map)
        else:
            df_mis['_mis_ins'] = None

        df_mis['_s_rto_clean'] = safe_col(df_mis, c_rto, '').apply(lambda x: re.sub(r'[^a-z0-9]', '', str(x).lower()) if pd.notna(x) else '')
        
        make_s = safe_col(df_mis, c_make, '').fillna('').astype(str)
        model_s = safe_col(df_mis, c_model, '').fillna('').astype(str)
        df_mis['_mis_make_model_raw'] = (make_s + " " + model_s).str.strip().str.lower()
        df_mis['_mis_mm_tokens'] = df_mis['_mis_make_model_raw'].apply(lambda x: set(re.findall(r'[a-z0-9]+', str(x))) if pd.notna(x) else set())
        df_mis['_mis_make_tokens'] = make_s.apply(lambda x: set(re.findall(r'[a-z0-9]+', str(x).lower())) if pd.notna(x) else set())
        df_mis['_s_make'] = make_s.str.lower()

        df_mis['_n_cc'] = pd.to_numeric(safe_col(df_mis, c_cc), errors='coerce').fillna(0)
        df_mis['_n_sc'] = pd.to_numeric(safe_col(df_mis, c_sc), errors='coerce').fillna(0)
        df_mis['_n_age'] = pd.to_numeric(safe_col(df_mis, c_age), errors='coerce').fillna(0)
        df_mis['_d_date'] = pd.to_datetime(safe_col(df_mis, c_date), errors='coerce', dayfirst=True)
        
        df_mis['_s_prod'] = safe_col(df_mis, c_prod, '').astype(str).str.strip().str.lower()
        df_mis['_s_sub'] = safe_col(df_mis, c_sub_prod, '').astype(str).str.replace(r'\.0$', '', regex=True).str.strip().str.lower()
        df_mis['_s_fuel'] = safe_col(df_mis, c_fuel, '').astype(str).str.strip().str.lower()
        df_mis['_s_class'] = safe_col(df_mis, c_class, '').astype(str).str.strip().str.lower()
        
        df_mis['_n_cpa'] = pd.to_numeric(safe_col(df_mis, c_cpa, '0'), errors='coerce').fillna(-1)
        df_mis['_n_ncb'] = pd.to_numeric(safe_col(df_mis, c_ncb, '0'), errors='coerce').fillna(-1)
        df_mis['_s_cpa'] = safe_col(df_mis, c_cpa, '').astype(str).str.strip().str.upper()
        df_mis['_s_ncb'] = safe_col(df_mis, c_ncb, '').astype(str).str.strip().str.upper()
        df_mis['_s_zd'] = safe_col(df_mis, c_zd, '').astype(str).str.strip().str.upper()

        # 6. ROW-BY-ROW MATRIX
        best_rows = []

        for idx, mis_row in df_mis.iterrows():
            orig_id = mis_row['Original_Row_ID']
            ins_match = mis_row['_mis_ins']
            
            if not ins_match:
                best_rows.append({
                    'Original_Row_ID': orig_id,
                    'Mapping Status': "❌ NO MATCH",
                    'Failure Reason': "Failed on: Insurance Company (Not found in grid >60%)" if c_ins else "Failed on: Insurance Company (Column Missing)",
                    'is_valid': False
                })
                continue
                
            grid_sub = df_grid[df_grid['_grid_ins'] == ins_match].copy()
            if grid_sub.empty:
                best_rows.append({
                    'Original_Row_ID': orig_id,
                    'Mapping Status': "❌ NO MATCH",
                    'Failure Reason': "Failed on: Insurance Company (No active rates for this insurer)",
                    'is_valid': False
                })
                continue

            g_prod = grid_sub['_grid_prod']
            m_prod = (g_prod == '') | (g_prod == 'nan') | (g_prod == 'na') | (g_prod == 'none') | (g_prod == mis_row['_s_prod'])
            
            g_sub = grid_sub['_grid_sub']
            m_sub = (g_sub == '') | (g_sub == 'nan') | (g_sub == 'na') | (g_sub == 'none') | (g_sub == mis_row['_s_sub'])
            
            g_fuel = grid_sub['_grid_fuel']
            m_fuel = (g_fuel == '') | (g_fuel == 'nan') | (g_fuel == 'na') | (g_fuel == 'none') | (g_fuel == mis_row['_s_fuel'])
            
            g_class = grid_sub['_grid_class']
            m_class = (g_class == '') | (g_class == 'nan') | (g_class == 'na') | (g_class == 'none') | (g_class == mis_row['_s_class'])

            m_cc = (grid_sub['cc_min'] == 0) | ((mis_row['_n_cc'] >= grid_sub['cc_min']) & (mis_row['_n_cc'] <= grid_sub['cc_max']))
            m_sc = (grid_sub['sc_min'] == 0) | ((mis_row['_n_sc'] >= grid_sub['sc_min']) & (mis_row['_n_sc'] <= grid_sub['sc_max']))
            m_age = (grid_sub['vehicle_age_min'] == 0) | ((mis_row['_n_age'] >= grid_sub['vehicle_age_min']) & (mis_row['_n_age'] <= grid_sub['vehicle_age_max']))
            m_date = grid_sub['from_date'].isna() | ((mis_row['_d_date'] >= grid_sub['from_date']) & (mis_row['_d_date'] <= grid_sub['to_date']))

            m_rto = grid_sub['new_rto_list'].apply(lambda g: check_rto_fast(mis_row['_s_rto_clean'], g, rto_mapping))
            m_make = grid_sub['new_vehicle_makes'].apply(lambda g: check_make_fast(mis_row['_mis_mm_tokens'], mis_row['_mis_make_tokens'], mis_row['_s_make'], mis_row['_mis_make_model_raw'], g, make_mapping))

            g_cpa = grid_sub['is_cpa__code'].fillna('NA')
            m_cpa = (g_cpa == 'NA') | ((g_cpa == 'YES') & (mis_row['_n_cpa'] >= 1) & (mis_row['_n_cpa'] <= 1000)) | ((g_cpa == 'NO') & (mis_row['_n_cpa'] == 0)) | (g_cpa == mis_row['_s_cpa'])

            g_ncb = grid_sub['is_ncb__code'].fillna('NA')
            m_ncb = (g_ncb == 'NA') | ((g_ncb == 'YES') & (mis_row['_n_ncb'] >= 1) & (mis_row['_n_ncb'] <= 99)) | ((g_ncb == 'NO') & (mis_row['_n_ncb'] == 0)) | (g_ncb == mis_row['_s_ncb'])

            g_zd = grid_sub['is_zd__code'].fillna('NA')
            m_zd = (g_zd == 'NA') | ((g_zd == 'YES') & (mis_row['_s_zd'] in ['1', 'YES', 'Y', 'TRUE'])) | ((g_zd == 'NO') & (mis_row['_s_zd'] in ['0', 'NO', 'N', 'FALSE'])) | (g_zd == mis_row['_s_zd'])

            is_valid = m_prod & m_sub & m_fuel & m_class & m_cc & m_sc & m_age & m_date & m_rto & m_make & m_cpa & m_ncb & m_zd
            match_score = m_prod.astype(int) + m_sub.astype(int) + m_fuel.astype(int) + m_class.astype(int) + \
                          m_cc.astype(int) + m_sc.astype(int) + m_age.astype(int) + m_date.astype(int) + \
                          m_rto.astype(int) + m_make.astype(int) + m_cpa.astype(int) + m_ncb.astype(int) + m_zd.astype(int)
            
            calc_rank = grid_sub['po_net_rate'].fillna(grid_sub['po_od_rate']).fillna(grid_sub['po_flat_amount']).fillna(0)

            grid_sub['is_valid'] = is_valid
            grid_sub['match_score'] = match_score
            grid_sub['Calculated Rank'] = calc_rank
            
            grid_sub['m_prod'] = m_prod; grid_sub['m_sub'] = m_sub; grid_sub['m_fuel'] = m_fuel; grid_sub['m_class'] = m_class
            grid_sub['m_cc'] = m_cc; grid_sub['m_sc'] = m_sc; grid_sub['m_age'] = m_age; grid_sub['m_date'] = m_date
            grid_sub['m_rto'] = m_rto; grid_sub['m_make'] = m_make
            grid_sub['m_cpa'] = m_cpa; grid_sub['m_ncb'] = m_ncb; grid_sub['m_zd'] = m_zd

            grid_sub = grid_sub.sort_values(by=['is_valid', 'match_score', 'Calculated Rank'], ascending=[False, False, False])
            best_row = grid_sub.iloc[0]

            if best_row['is_valid']:
                mapping_status = "✅ MATCH"
                failure_reason = "Matched Successfully"
            else:
                mapping_status = "❌ NO MATCH"
                fails = []
                if not best_row['m_prod']: fails.append("Vehicle Product")
                if not best_row['m_sub']: fails.append("Sub Product")
                if not best_row['m_fuel']: fails.append("Fuel Type")
                if not best_row['m_class']: fails.append("Vehicle Class")
                if not best_row['m_cc']: fails.append("CC Limit")
                if not best_row['m_sc']: fails.append("Seating Limit")
                if not best_row['m_age']: fails.append("Vehicle Age")
                if not best_row['m_date']: fails.append("Inception Date")
                if not best_row['m_rto']: fails.append("RTO Code")
                if not best_row['m_make']: fails.append("Make/Model")
                if not best_row['m_cpa']: fails.append("CPA")
                if not best_row['m_ncb']: fails.append("NCB")
                if not best_row['m_zd']: fails.append("Nil Dep")
                failure_reason = f"Failed on: {', '.join(fails)}"
                
            best_rows.append({
                'Original_Row_ID': orig_id,
                'Mapping Status': mapping_status,
                'Failure Reason': failure_reason,
                'Displaygroupid': best_row['group_id'] if pd.notna(best_row['group_id']) else best_row['id'],
                'Potype': best_row['po_type'],
                'Poodrate': best_row['po_od_rate'],
                'Potprate': best_row['po_tp_rate'],
                'Ponetrate': best_row['po_net_rate'],
                'Poflatamount': best_row['po_flat_amount'],
                'Addtnc': best_row['add_tnc']
            })

        # 7. ASSEMBLE EXPORT
        df_extracted = pd.DataFrame(best_rows)
        if not df_extracted.empty:
            df_final = df_mis.merge(df_extracted, on='Original_Row_ID', how='left')
        else:
            df_final = df_mis.copy()
            df_final['Mapping Status'] = "❌ NO MATCH"
            df_final['Failure Reason'] = "Failed on: System Error (No blocks evaluated)"

        df_final['Mapping Status'] = df_final['Mapping Status'].fillna("❌ NO MATCH")

        payout_cols = ['Displaygroupid', 'Potype', 'Poodrate', 'Potprate', 'Ponetrate', 'Poflatamount', 'Addtnc']
        for p_col in payout_cols:
            if p_col not in df_final.columns: df_final[p_col] = None
            
        df_final.loc[df_final['Mapping Status'] != "✅ MATCH", payout_cols] = None

        generated_cols = ['Mapping Status', 'Failure Reason'] + payout_cols
        original_cols = [c for c in mis_cols]
        df_final = df_final[generated_cols + original_cols]

        # 8. SAVE
        output = io.BytesIO()
        if file_ext == 'csv':
            df_final.to_csv(output, index=False)
        else:
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_final.to_excel(writer, index=False)

        new_filename = f"mapped_{mis_obj.uploaded_file.name.split('/')[-1]}"
        mis_obj.processed_file.save(new_filename, ContentFile(output.getvalue()))
        mis_obj.status = 'COMPLETED'
        mis_obj.processed_at = timezone.now()
        mis_obj.error_message = ""
        mis_obj.save()

    except BaseException as e: # Catch Memory Errors and Thread Deaths!
        import traceback
        error_trace = traceback.format_exc()
        print(f"\n❌ MIS Mapping Error: {str(e)}")
        print(error_trace)
        
        try:
            # Force connection clear before updating error to prevent transaction locks
            connection.close()
            mis_obj = MISFile.objects.get(id=mis_file_id)
            mis_obj.status = 'FAILED'
            mis_obj.error_message = str(e)[:1000] # Limit size for database safety
            mis_obj.processed_at = timezone.now()
            mis_obj.save()
        except BaseException as recovery_err:
            print(f"CRITICAL FAULT: Could not write Failure state to DB: {recovery_err}")
            
    finally:
        # Guarantee DB Thread Pool release
        connection.close()