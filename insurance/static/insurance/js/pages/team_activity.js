
    $(document).ready(function () {
        var box = $('#taDateRange');
        if (box.length) {
            box.daterangepicker({
                autoUpdateInput: false,
                locale: { format: 'YYYY-MM-DD', cancelLabel: 'Clear' },
                alwaysShowCalendars: true,
                ranges: {
                    'Today': [moment(), moment()],
                    'Last 7 days': [moment().subtract(6, 'days'), moment()],
                    'Last 30 days': [moment().subtract(29, 'days'), moment()],
                    'This month': [moment().startOf('month'), moment().endOf('month')],
                    'Last month': [moment().subtract(1, 'month').startOf('month'), moment().subtract(1, 'month').endOf('month')]
                }
            });

            box.on('apply.daterangepicker', function (ev, picker) {
                $(this).val(picker.startDate.format('YYYY-MM-DD') + ' - ' + picker.endDate.format('YYYY-MM-DD'));
                this.form.submit();
            });

            // "Clear" leaves the box empty, which the page reads as "no date limit".
            box.on('cancel.daterangepicker', function () {
                $(this).val('');
                this.form.submit();
            });
        }

        // The dropdowns narrow the list as soon as they change.
        $('select[data-autosubmit]').on('change', function () {
            this.form.submit();
        });
    });
