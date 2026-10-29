
    $(document).ready(function () {
        // Chrome sometimes fails to paint a number input's server-rendered
        // "value" attribute after a form-submit reload (autocomplete
        // restore stomps it back to blank right after first paint), even
        // though the value was received and applied correctly server-side.
        // Re-asserting it via JS from the hidden field sidesteps that.
        document.getElementById('ccInput').value = document.getElementById('ccHidden').value || '';
        document.getElementById('scInput').value = document.getElementById('scHidden').value || '';

        $('#date_range').daterangepicker({
            autoUpdateInput: false,
            locale: { format: 'YYYY-MM-DD', cancelLabel: 'Clear' }
        });

        $('#date_range').on('apply.daterangepicker', function (ev, picker) {
            $(this).val(picker.startDate.format('YYYY-MM-DD') + ' - ' + picker.endDate.format('YYYY-MM-DD'));
            document.getElementById('dateRangeForm').submit();
        });

        $('#date_range').on('cancel.daterangepicker', function () {
            $(this).val('');
            document.getElementById('dateRangeForm').submit();
        });

        // CC / seating-capacity are plain number filters that need the raw
        // per-row data to apply correctly, so (like the date range) they're
        // resolved server-side and reload the page rather than filtering
        // client-side against the already-merged heatmap cells.
        function submitCcSc(){
            document.getElementById('ccHidden').value = document.getElementById('ccInput').value.trim();
            document.getElementById('scHidden').value = document.getElementById('scInput').value.trim();
            document.getElementById('dateRangeForm').submit();
        }
        ['ccInput', 'scInput'].forEach(id => {
            const el = document.getElementById(id);
            el.addEventListener('change', submitCcSc);
            el.addEventListener('keydown', e => { if(e.key === 'Enter'){ e.preventDefault(); submitCcSc(); } });
        });

        // Default to today on a genuinely fresh visit only. auto_loaded_today
        // is echoed back from the query string (not a cookie/session flag),
        // so once it's set to "1" this won't fire again even if the user
        // later clears the date deliberately to see every date.
        const form = document.getElementById('dateRangeForm');
        const dateRangeInput = document.getElementById('date_range');
        const autoLoadedFlag = document.getElementById('auto_loaded_today');

        const today = moment().format('YYYY-MM-DD');
        const todayRange = `${today} - ${today}`;
        const hasDateRange = dateRangeInput && dateRangeInput.value.trim() !== '';
        const alreadyAutoLoaded = autoLoadedFlag && autoLoadedFlag.value === '1';

        if (dateRangeInput && !hasDateRange && !alreadyAutoLoaded) {
            dateRangeInput.value = todayRange;
            if (autoLoadedFlag) autoLoadedFlag.value = '1';
            form.submit();
        }
    });
