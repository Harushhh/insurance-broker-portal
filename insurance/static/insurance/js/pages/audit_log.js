
    $(document).ready(function () {
        $('#auditDateRange').daterangepicker({
            autoUpdateInput: false,
            locale: { format: 'YYYY-MM-DD', cancelLabel: 'Clear' }
        });

        $('#auditDateRange').on('apply.daterangepicker', function (ev, picker) {
            $(this).val(picker.startDate.format('YYYY-MM-DD') + ' - ' + picker.endDate.format('YYYY-MM-DD'));
        });

        $('#auditDateRange').on('cancel.daterangepicker', function () {
            $(this).val('');
        });
    });
