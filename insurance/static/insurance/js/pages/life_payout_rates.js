// Life Payout Checker. Category, Payment Mode, PPT and PT are fixed lists rendered by the server; the page only
//  - narrows the Product dropdown from the distinct [insurer, "CATEGORY|CATEGORY", product] rows of the live grid
//    (embedded as JSON) when an insurer or a category is picked, and
//  - hides the PPT field while Payment Mode is SP (single pay has no premium paying term),
// and the "Raise Issue / Ticket" modal posts the current search to the tickets API.
$(function () {
    const rows = JSON.parse(document.getElementById('life-filter-rows').textContent || '[]');
    const insurerSelect = $('#insurerSelect');
    const categorySelect = $('select[name="category"]');
    const productSelect = $('select[name="product"]');
    const modeSelect = $('#paymentMode');
    const pptSelect = $('#pptSelect');
    const pptGroup = $('#pptGroup');
    const natural = function (a, b) { return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }); };

    // Products of the picked insurers that belong to the picked category; a product stays chosen while it is listed.
    function rebuildProducts() {
        const insurers = (insurerSelect.val() || []).filter(Boolean);
        const category = categorySelect.val() || '';
        const keep = productSelect.val() || '';
        const products = new Set();
        rows.forEach(function (r) {
            const insurerOk = !insurers.length || insurers.indexOf(r[0]) !== -1;
            const categoryOk = !category || r[1].split('|').indexOf(category) !== -1;
            if (r[2] && insurerOk && categoryOk) products.add(r[2]);
        });
        productSelect.empty().append(new Option('', '', false, false));
        Array.from(products).sort(natural).forEach(function (v) {
            productSelect.append(new Option(v, v, false, v === keep));
        });
        productSelect.trigger('change.select2');
    }

    // Single pay has no PPT: hide the field and drop its value (a disabled field is not submitted).
    function syncPaymentMode() {
        const single = modeSelect.val() === 'SP';
        pptGroup.toggle(!single);
        pptSelect.prop('disabled', single);
        if (single) pptSelect.val('').trigger('change.select2');
    }

    $('.life-select').select2({ width: '100%', placeholder: 'All', allowClear: true });
    insurerSelect.select2({ width: '100%', placeholder: 'All insurers...', allowClear: true, closeOnSelect: false });
    rebuildProducts();
    syncPaymentMode();
    insurerSelect.on('change', rebuildProducts);
    categorySelect.on('change', rebuildProducts);
    modeSelect.on('change', syncPaymentMode);
});

// --- ticketing (same pattern as the Health and Motor checkers) ---
function openTicketModal() {
    document.getElementById('ticketModal').style.display = 'flex';
    // Select2 reads the element's width, which is 0 while the modal is display:none, so it is
    // started on first open, with the dropdown kept inside the modal's own stacking context.
    if (!$('#ticketInsurer').data('select2')) {
        $('#ticketInsurer').select2({ width: '100%', placeholder: 'Select...', dropdownParent: $('#ticketModal') });
    }
}

function closeTicketModal() {
    document.getElementById('ticketModal').style.display = 'none';
    document.getElementById('ticketRemarks').value = '';
    document.getElementById('ticketType').value = '';
    $('#ticketInsurer').val('').trigger('change');
}

async function submitTicket() {
    const ticketType = document.getElementById('ticketType').value;
    const ticketInsurer = document.getElementById('ticketInsurer').value;
    const remarks = document.getElementById('ticketRemarks').value.trim();
    if (!ticketType) { alert('Please select a ticket type before submitting.'); return; }
    if (!ticketInsurer) { alert('Please select an insurer before submitting.'); return; }
    if (!remarks) { alert('Please enter your remarks before submitting.'); return; }

    const labels = {
        insurer: 'Insurer', category: 'Category', product: 'Product', payment_mode: 'Payment Mode',
        ppt: 'PPT', pt: 'PT', target_date: 'Date', q: 'Search',
    };
    // Several entries can share a key (Insurer is a multi-select): join them instead of letting the last win.
    const collected = {};
    new FormData(document.getElementById('lifeCheckForm')).forEach(function (val, key) {
        val = String(val).trim();
        if (!val) return;
        collected[key] = collected[key] ? collected[key] + ', ' + val : val;
    });
    const formPayload = {};
    Object.keys(collected).forEach(function (key) { formPayload[labels[key] || key] = collected[key]; });
    // Named apart from the search form's own multi-select "Insurer" so it does not overwrite that.
    formPayload['Ticket Insurer'] = ticketInsurer;

    try {
        const response = await fetch('/api/create-ticket/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': document.getElementById('lifeCsrfToken').value,
            },
            body: JSON.stringify({ remarks: remarks, form_payload: formPayload, category: 'LIFE', ticket_type: ticketType }),
        });
        const result = await response.json();
        if (result.success) {
            alert('Ticket raised successfully! It will now appear in your Support Tickets dashboard.');
            closeTicketModal();
        } else {
            alert('Error: ' + result.message);
        }
    } catch (error) {
        alert('A network error occurred while submitting the ticket. Please try again.');
        console.error(error);
    }
}
