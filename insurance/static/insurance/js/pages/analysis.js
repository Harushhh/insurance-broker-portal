
(function(){
    const RAW = JSON.parse(document.getElementById('chart-data').textContent);
    const RECORDS = RAW.records;
    const PRODUCTS = RAW.products;
    const SUB_PRODUCTS = RAW.sub_products;
    const INSURERS = RAW.insurers;
    const STATES = RAW.states;
    const MONTHS = RAW.months;

    const INS_COLORS = ['--ins-1','--ins-2','--ins-3','--ins-4','--ins-5','--ins-6','--ins-7'];
    document.documentElement.style.setProperty('--ins-1', '#2563eb');
    document.documentElement.style.setProperty('--ins-2', '#d97706');
    document.documentElement.style.setProperty('--ins-3', '#7c3aed');
    document.documentElement.style.setProperty('--ins-4', '#e11d48');
    document.documentElement.style.setProperty('--ins-5', '#0891b2');
    document.documentElement.style.setProperty('--ins-6', '#4f46e5');
    document.documentElement.style.setProperty('--ins-7', '#ea580c');

    const STATE_NAMES = {
        AN:'Andaman & Nicobar', AP:'Andhra Pradesh', AR:'Arunachal Pradesh', AS:'Assam', BR:'Bihar',
        CG:'Chhattisgarh', CH:'Chandigarh', DD:'Daman & Diu', DL:'Delhi', DN:'Dadra & Nagar Haveli',
        GA:'Goa', GJ:'Gujarat', HP:'Himachal Pradesh', HR:'Haryana', JH:'Jharkhand', JK:'Jammu & Kashmir',
        KA:'Karnataka', KL:'Kerala', LA:'Ladakh', LD:'Lakshadweep', MH:'Maharashtra', ML:'Meghalaya',
        MN:'Manipur', MP:'Madhya Pradesh', MZ:'Mizoram', NL:'Nagaland', OD:'Odisha',
        PB:'Punjab', PY:'Puducherry', RJ:'Rajasthan', SK:'Sikkim', TN:'Tamil Nadu', TR:'Tripura',
        TS:'Telangana', UA:'Uttarakhand', UK:'Uttarakhand', UP:'Uttar Pradesh', WB:'West Bengal'
    };
    function stateName(code){ return STATE_NAMES[code] || code; }

    const TITLECASE_EXCEPTIONS = new Set(['NEW','AND','THE','OF','FOR']);
    function shortName(full){
        let s = full.replace(/^THE\s+/i, '');
        s = s.replace(/\b(GENERAL\s+)?(INSURANCE|ASSURANCE)\s+(COMPANY\s+LIMITED|CO\.?\s*LTD\.?|LIMITED|LTD\.?)\b/gi, '');
        s = s.replace(/\s+/g, ' ').trim();
        s = s || full;
        return s.split(' ').map(w => {
            if(w.length <= 3 && !TITLECASE_EXCEPTIONS.has(w.toUpperCase())) return w;
            return w.charAt(0) + w.slice(1).toLowerCase();
        }).join(' ');
    }

    const insMeta = INSURERS.map((full, i) => ({ id: full, full: full, short: shortName(full), colorVar: INS_COLORS[i % INS_COLORS.length] }));

    // Labels for the per-field breakdown shown in the hover card. There's no
    // user-facing rate-type picker any more - the heatmap always shows the
    // single highest rate across all of these; the tooltip shows the rest.
    const RATE_FIELD_LABELS = { net: 'Net', od: 'OD', tp: 'TP', tp2: 'TP2', tp3: 'TP3', tp4: 'TP4', tp5: 'TP5' };
    const RATE_KEYS = Object.keys(RATE_FIELD_LABELS);

    let state = { product: PRODUCTS[0] || '', subProduct: SUB_PRODUCTS[0] || '', query: '', selected: null };

    function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
    function fmtPct(v){ return (v === null || v === undefined) ? '&mdash;' : (Math.round(v*100)/100).toFixed(2) + '%'; }
    function fmtNum(v){
        if(v === null || v === undefined) return null;
        const r = Math.round(v*100)/100;
        return Number.isInteger(r) ? String(r) : r.toFixed(2);
    }
    function fmtRatePct(range){
        if(!range) return '&mdash;';
        if(range.min === range.max) return fmtPct(range.min);
        return fmtPct(range.min) + ' - ' + fmtPct(range.max);
    }
    function fmtRange(range, unit){
        unit = unit || '';
        const hasMin = range && range.min !== null && range.min !== undefined;
        const hasMax = range && range.max !== null && range.max !== undefined;
        if(!hasMin && !hasMax) return '&mdash;';
        const a = fmtNum(range.min), b = fmtNum(range.max);
        if(hasMin && hasMax) return (range.min === range.max) ? (a + unit) : (a + unit + ' - ' + b + unit);
        return hasMax ? ('up to ' + b + unit) : (a + unit + '+');
    }

    // Spans whichever of Net/OD/TP/TP2-5 exist for the cell - the widest
    // actual range across those fields, not an average of them.
    function bestRange(entry){
        if(!entry) return null;
        const parts = RATE_KEYS.map(k => entry[k]).filter(Boolean);
        if(!parts.length) return null;
        return {
            min: Math.min(...parts.map(p => p.min)),
            max: Math.max(...parts.map(p => p.max)),
            n: parts.reduce((a, p) => a + p.n, 0)
        };
    }
    function topValue(entry){
        const r = bestRange(entry);
        return r ? r.max : null;
    }

    function filteredRecords(){
        return RECORDS.filter(r => r.product === state.product && r.sub_product === state.subProduct);
    }

    // Merges the (possibly several) month-bucket records for one insurer x
    // state into one effective cell: widest min/max range, summed sample
    // count, union of NCB/CPA/ZD values.
    function mergeRecords(recs){
        const m = { age_min:null, age_max:null, cc_min:null, cc_max:null, tariff_min:null, tariff_max:null, ncb:new Set(), cpa:new Set(), zd:new Set() };
        RATE_KEYS.forEach(k => { m[k] = null; });
        recs.forEach(r => {
            RATE_KEYS.forEach(k => {
                if(r[k]){
                    if(!m[k]) m[k] = { min:r[k].min, max:r[k].max, n:r[k].n };
                    else { m[k].min = Math.min(m[k].min, r[k].min); m[k].max = Math.max(m[k].max, r[k].max); m[k].n += r[k].n; }
                }
            });
            [['age_min','age_max'], ['cc_min','cc_max'], ['tariff_min','tariff_max']].forEach(([lo, hi]) => {
                if(r[lo] !== null && r[lo] !== undefined) m[lo] = (m[lo] === null) ? r[lo] : Math.min(m[lo], r[lo]);
                if(r[hi] !== null && r[hi] !== undefined) m[hi] = (m[hi] === null) ? r[hi] : Math.max(m[hi], r[hi]);
            });
            (r.ncb || []).forEach(v => m.ncb.add(v));
            (r.cpa || []).forEach(v => m.cpa.add(v));
            (r.zd || []).forEach(v => m.zd.add(v));
        });
        m.ncb = [...m.ncb]; m.cpa = [...m.cpa]; m.zd = [...m.zd];
        return m;
    }

    function buildCellIndex(){
        const relevant = filteredRecords();
        const groups = new Map();
        relevant.forEach(r => {
            const key = r.insurer + '|' + r.state;
            if(!groups.has(key)) groups.set(key, []);
            groups.get(key).push(r);
        });
        const index = new Map();
        groups.forEach((recs, key) => index.set(key, mergeRecords(recs)));
        return index;
    }
    function cellFor(index, insurerFull, stateCode){
        return index.get(insurerFull + '|' + stateCode) || null;
    }

    function parseSearchTerms(){
        return state.query.split(',').map(t => t.trim().toLowerCase()).filter(Boolean);
    }
    function visibleStates(){
        const terms = parseSearchTerms();
        if(!terms.length) return STATES;
        // State codes are always exactly 2 letters, so a short term (like
        // "GA") means the user is typing a code and wants that state only -
        // substring-matching it against full names would also catch "GA"
        // inside Chhattisgarh, Nagaland, Bengal, etc. Longer terms are
        // clearly a name fragment (e.g. "Maharashtra"), so those still
        // match anywhere in the name.
        return STATES.filter(code => terms.some(t => {
            if(t.length <= 2) return code.toLowerCase() === t;
            return stateName(code).toLowerCase().includes(t);
        }));
    }

    function computeColorRange(index){
        const vals = [];
        index.forEach(entry => { const v = topValue(entry); if(v !== null) vals.push(v); });
        if(!vals.length) return null;
        return { min: Math.min(...vals), max: Math.max(...vals) };
    }
    function bucketFor(v, range){
        if(range.max === range.min) return 3;
        const t = (v - range.min) / (range.max - range.min);
        return Math.max(0, Math.min(5, Math.floor(t * 6)));
    }

    function pickDefaultState(index, cols){
        let best = null, bestCount = -1;
        cols.forEach(code => {
            const count = INSURERS.filter(full => topValue(cellFor(index, full, code)) !== null).length;
            if(count > bestCount){ bestCount = count; best = code; }
        });
        return best;
    }

    // Insurer rows are ranked by the average of their (already actual, not
    // averaged) highest rates across whichever state columns are visible.
    function sortedInsurers(index, cols){
        const scored = insMeta.map(m => {
            const vals = cols.map(code => topValue(cellFor(index, m.full, code))).filter(v => v !== null);
            const avg = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
            return { m, avg };
        });
        scored.sort((a, b) => {
            if(a.avg === null && b.avg === null) return 0;
            if(a.avg === null) return 1;
            if(b.avg === null) return -1;
            return b.avg - a.avg;
        });
        return scored;
    }

    // ---- hover detail card ----
    const tooltipEl = document.getElementById('cellTooltip');
    let currentCellIndex = null;

    function tooltipRow(label, value){
        return `<div class="tt-row"><span class="tt-lbl">${esc(label)}</span><span class="tt-val">${value}</span></div>`;
    }
    function buildTooltipHtml(m, code, entry){
        const overall = bestRange(entry);
        let html = `<div class="tt-title">${esc(m.short)} &middot; ${esc(stateName(code))}</div>`;
        if(!overall){
            html += tooltipRow('Rate', 'No data on file');
        } else {
            html += tooltipRow('Highest rate', fmtPct(overall.max));
            html += tooltipRow('Lowest rate', fmtPct(overall.min));
            html += tooltipRow('Rate cards', String(overall.n));
            RATE_KEYS.forEach(k => {
                const r = entry[k];
                if(r) html += tooltipRow(RATE_FIELD_LABELS[k], fmtRatePct(r) + ' (n=' + r.n + ')');
            });
        }
        if(entry){
            html += tooltipRow('Vehicle age', fmtRange({ min: entry.age_min, max: entry.age_max }, ' yrs'));
            html += tooltipRow('Engine CC', fmtRange({ min: entry.cc_min, max: entry.cc_max }, ' cc'));
            html += tooltipRow('Tariff', fmtRange({ min: entry.tariff_min, max: entry.tariff_max }, ''));
            html += tooltipRow('NCB', entry.ncb.length ? esc(entry.ncb.join(', ')) : '&mdash;');
            html += tooltipRow('CPA', entry.cpa.length ? esc(entry.cpa.join(', ')) : '&mdash;');
            html += tooltipRow('ZD', entry.zd.length ? esc(entry.zd.join(', ')) : '&mdash;');
        }
        return html;
    }
    function positionTooltip(x, y){
        const pad = 14;
        const rect = tooltipEl.getBoundingClientRect();
        let left = x + pad, top = y + pad;
        if(left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
        if(top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
        tooltipEl.style.left = Math.max(4, left) + 'px';
        tooltipEl.style.top = Math.max(4, top) + 'px';
    }
    const chartBodyEl = document.getElementById('chartBody');
    chartBodyEl.addEventListener('mouseover', e => {
        const box = e.target.closest('.box[data-ins]');
        if(!box || !currentCellIndex) return;
        const insurerFull = box.dataset.ins, code = box.dataset.state;
        const m = insMeta.find(mm => mm.full === insurerFull);
        const entry = cellFor(currentCellIndex, insurerFull, code);
        tooltipEl.innerHTML = buildTooltipHtml(m, code, entry);
        tooltipEl.hidden = false;
        positionTooltip(e.clientX, e.clientY);
    });
    chartBodyEl.addEventListener('mousemove', e => {
        if(!tooltipEl.hidden) positionTooltip(e.clientX, e.clientY);
    });
    chartBodyEl.addEventListener('mouseout', e => {
        const leavingBox = e.target.closest('.box[data-ins]');
        const enteringBox = e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('.box[data-ins]');
        if(leavingBox && !enteringBox) tooltipEl.hidden = true;
    });

    function renderControls(){
        const sel = document.getElementById('productSelect');
        sel.innerHTML = PRODUCTS.map(p => `<option value="${esc(p)}"${p===state.product?' selected':''}>${esc(p)}</option>`).join('');
        sel.addEventListener('change', () => {
            state.product = sel.value;
            state.selected = null;
            renderBody();
        });

        const subSel = document.getElementById('subProductSelect');
        subSel.innerHTML = SUB_PRODUCTS.map(sp => `<option value="${esc(sp)}"${sp===state.subProduct?' selected':''}>${esc(sp)}</option>`).join('');
        subSel.addEventListener('change', () => {
            state.subProduct = subSel.value;
            state.selected = null;
            renderBody();
        });

        const search = document.getElementById('stateSearch');
        search.addEventListener('input', () => { state.query = search.value; renderBody(); });
    }

    function renderStats(){
        const host = document.getElementById('statStrip');
        host.innerHTML = `
            <div class="analysis-stat"><b>${RECORDS.length}</b><span>insurer &times; state &times; month cells</span></div>
            <div class="analysis-stat"><b>${INSURERS.length}</b><span>insurers</span></div>
            <div class="analysis-stat"><b>${STATES.length}</b><span>states covered</span></div>
            <div class="analysis-stat"><b>${PRODUCTS.length}</b><span>products</span></div>
            <div class="analysis-stat"><b>${MONTHS.length}</b><span>dated months tracked</span></div>
        `;
    }

    function renderBody(){
        const host = document.getElementById('chartBody');
        const cellIndex = buildCellIndex();
        currentCellIndex = cellIndex;
        const cols = visibleStates();

        if(state.selected === null || !cols.includes(state.selected)){
            state.selected = pickDefaultState(cellIndex, cols);
        }
        const range = computeColorRange(cellIndex);

        if(!cellIndex.size || !range){
            host.innerHTML = `<div class="analysis-empty">No resolvable state data for ${esc(state.product)}.</div>`;
            return;
        }
        if(!cols.length){
            host.innerHTML = `<div class="analysis-empty">No states match &ldquo;${esc(state.query)}&rdquo;. Try a different search.</div>`;
            return;
        }

        const ranked = sortedInsurers(cellIndex, cols);

        let html = '<div class="analysis-table-scroll"><table class="heat-table"><caption class="visually-hidden">Insurer payout rate by state, insurers ranked highest to lowest</caption><thead><tr><th class="heat-row-head"></th>';
        cols.forEach(code => {
            const isSel = state.selected === code;
            html += `<th class="heat-col-head"><button type="button" class="col-btn" data-state="${esc(code)}" aria-pressed="${isSel}" title="${esc(stateName(code))}">${esc(code)}</button></th>`;
        });
        html += '</tr></thead><tbody>';

        ranked.forEach(({ m }) => {
            html += `<tr><th class="heat-row-head" scope="row"><span class="dot" style="background:var(${m.colorVar})"></span><span class="ins-name">${esc(m.short)}</span></th>`;
            cols.forEach(code => {
                const entry = cellFor(cellIndex, m.full, code);
                const r = bestRange(entry);
                if(!r){
                    html += `<td class="heat-cell"><div class="box cell-nodata" data-ins="${esc(m.full)}" data-state="${esc(code)}" title="No rate on file">&mdash;</div></td>`;
                } else {
                    const b = bucketFor(r.max, range);
                    html += `<td class="heat-cell"><div class="box cell-b${b}" data-ins="${esc(m.full)}" data-state="${esc(code)}" title="${esc(m.short)} in ${esc(stateName(code))} &middot; highest of ${r.n} rate cards">${fmtPct(r.max)}</div></td>`;
                }
            });
            html += '</tr>';
        });
        html += '</tbody></table></div>';

        html += `<div class="analysis-legend">
            <div class="analysis-legend-ins">${insMeta.map(m => `<span class="analysis-legend-item"><span class="dot" style="background:var(${m.colorVar})"></span>${esc(m.short)}</span>`).join('')}</div>
            <div class="analysis-legend-seq"><span>${fmtPct(range.min)}</span><span class="ramp">${[0,1,2,3,4,5].map(b=>`<span class="cell-b${b}"></span>`).join('')}</span><span>${fmtPct(range.max)}</span></div>
        </div>`;

        html += '<div class="analysis-drilldown" id="drilldownHost"></div>';
        host.innerHTML = html;

        host.querySelectorAll('.col-btn').forEach(btn => {
            btn.addEventListener('click', () => { state.selected = btn.dataset.state; renderBody(); });
            btn.addEventListener('keydown', e => { if(e.key==='Enter'||e.key===' '){ e.preventDefault(); btn.click(); } });
        });

        renderDrilldown(cellIndex);
    }

    function renderDrilldown(index){
        const host = document.getElementById('drilldownHost');
        if(!host || !state.selected) return;
        const code = state.selected;
        const entries = insMeta.map(m => ({ m, r: bestRange(cellFor(index, m.full, code)) }))
            .filter(e => e.r !== null)
            .sort((a, b) => b.r.max - a.r.max);

        let html = `<h4>${esc(stateName(code))} (${esc(code)}) &mdash; insurers ranked</h4>`;
        if(!entries.length){
            html += `<p style="color:#64748b;font-size:13px;margin:0;">No insurer has a rate on file here for ${esc(state.product)} / ${esc(state.subProduct)}.</p>`;
        } else {
            const max = entries[0].r.max;
            entries.forEach(e => {
                const pct = max ? Math.max(6, (e.r.max/max)*100) : 0;
                html += `<div class="dd-bar-row">
                    <span class="lbl"><span class="dot" style="background:var(${e.m.colorVar})"></span>${esc(e.m.short)}</span>
                    <span class="dd-track"><span class="dd-fill" style="width:${pct}%;background:var(${e.m.colorVar})"></span></span>
                    <span class="val">${fmtPct(e.r.max)}</span>
                </div>`;
            });
        }
        host.innerHTML = html;
    }

    renderControls();
    renderStats();
    renderBody();
})();
