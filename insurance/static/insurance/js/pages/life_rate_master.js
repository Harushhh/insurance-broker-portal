/* Life Rate Master, Payout rows page: tick rows, choose "all rows matching the filters", and the bulk-edit dialog.
   The server checks everything again (which fields may change, the value, the 0-100 range); this only makes the form
   easy to fill in. */
(function () {
    'use strict';

    var form = document.getElementById('lifeBulkForm');
    if (!form) { return; }

    var rowChecks = Array.prototype.slice.call(form.querySelectorAll('.life-row-check'));
    var pageCheck = document.getElementById('lifeSelectPage');
    var badge = document.getElementById('lifeSelectedCount');
    var allLink = document.getElementById('lifeSelectAllMatching');
    var clearLink = document.getElementById('lifeClearSelection');
    var openBtn = document.getElementById('lifeBulkOpen');
    var scopeInput = document.getElementById('lifeBulkScope');
    var modalEl = document.getElementById('lifeBulkModal');
    var targetEl = document.getElementById('lifeBulkTarget');
    var fieldSel = document.getElementById('lifeBulkField');
    var opSel = document.getElementById('lifeBulkOp');
    var valueRow = document.getElementById('lifeBulkValueRow');
    var valueInput = document.getElementById('lifeBulkValue');
    var hintEl = document.getElementById('lifeBulkHint');
    var total = parseInt(form.getAttribute('data-total'), 10) || 0;

    var OPERATIONS = {
        payout: [
            ['set', 'Set to'],
            ['add', 'Add percentage points (a minus reduces)'],
            ['multiply', 'Multiply by a factor'],
            ['clear', 'Clear the value']
        ],
        text: [['set', 'Set to'], ['clear', 'Clear the value']],
        remarks: [['set', 'Set to'], ['append', 'Append to the existing text'], ['clear', 'Clear the value']]
    };
    var HINTS = {
        payout: {
            set: 'A percent between 0 and 100, for example 17.5',
            add: 'Points added to each payout, for example 0.5 or -0.25. Blank payouts are skipped.',
            multiply: 'A factor, for example 0.9 (10% less) or 1.05 (5% more). Blank payouts are skipped.'
        },
        text: { set: 'The new text for every row' },
        remarks: { set: 'The new remark for every row', append: 'Text added after each row\'s existing remark' }
    };

    function wholeFilter() { return scopeInput.value === 'filtered'; }
    function selectedCount() {
        return rowChecks.filter(function (box) { return box.checked; }).length;
    }
    function targetCount() { return wholeFilter() ? total : selectedCount(); }
    function plural(n) { return n + (n === 1 ? ' row' : ' rows'); }

    function refresh() {
        var n = selectedCount();
        if (pageCheck) {
            pageCheck.checked = n > 0 && n === rowChecks.length;
            pageCheck.indeterminate = n > 0 && n < rowChecks.length;
        }
        badge.textContent = wholeFilter() ? 'All ' + total + ' matching rows selected' : n + ' selected';
        allLink.hidden = wholeFilter() || n !== rowChecks.length || total <= rowChecks.length;
        clearLink.hidden = n === 0 && !wholeFilter();
        openBtn.disabled = targetCount() === 0;
    }

    function setAll(checked) {
        rowChecks.forEach(function (box) { box.checked = checked; });
        scopeInput.value = 'selected';
        refresh();
    }

    if (pageCheck) {
        pageCheck.addEventListener('change', function () { setAll(pageCheck.checked); });
    }
    rowChecks.forEach(function (box) {
        box.addEventListener('change', function () {
            scopeInput.value = 'selected';          // a hand-picked set is no longer "everything that matches"
            refresh();
        });
    });
    allLink.addEventListener('click', function (event) {
        event.preventDefault();
        scopeInput.value = 'filtered';
        refresh();
    });
    clearLink.addEventListener('click', function (event) {
        event.preventDefault();
        setAll(false);
    });

    function kind() {
        return fieldSel.options[fieldSel.selectedIndex].getAttribute('data-kind');
    }
    function rebuildOperations() {
        var current = opSel.value;
        opSel.innerHTML = '';
        OPERATIONS[kind()].forEach(function (pair) {
            var option = document.createElement('option');
            option.value = pair[0];
            option.textContent = pair[1];
            opSel.appendChild(option);
        });
        if (Array.prototype.some.call(opSel.options, function (o) { return o.value === current; })) {
            opSel.value = current;
        }
        updateValueRow();
    }
    function updateValueRow() {
        var clearing = opSel.value === 'clear';
        valueRow.hidden = clearing;
        valueInput.required = !clearing;
        hintEl.textContent = clearing ? '' : (HINTS[kind()][opSel.value] || '');
    }
    fieldSel.addEventListener('change', rebuildOperations);
    opSel.addEventListener('change', updateValueRow);

    openBtn.addEventListener('click', function () {
        if (targetCount() === 0) { return; }
        targetEl.textContent = wholeFilter() ? 'all ' + plural(total) + ' matching the filters' : plural(selectedCount());
        rebuildOperations();
        window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
    });

    form.addEventListener('submit', function (event) {
        var n = targetCount();
        var summary = opSel.options[opSel.selectedIndex].textContent + ': ' +
            fieldSel.options[fieldSel.selectedIndex].textContent +
            (opSel.value === 'clear' ? '' : ' (' + valueInput.value + ')');
        if (n === 0 || !window.confirm('Apply this change to ' + plural(n) + '?\n\n' + summary)) {
            event.preventDefault();
        }
    });

    refresh();
})();
