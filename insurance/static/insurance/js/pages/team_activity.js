
    $(document).ready(function () {
        var box = $('#taDateRange');
        if (box.length) {
            // Events are kept from the 1st of last month (the page puts that date on the box), so nothing earlier can be picked.
            var firstKept = box.data('min');
            // Weeks run Monday to Sunday; "this week" and "this month" stop at today so a report has no empty future days.
            var lastWeek = moment().subtract(1, 'week');
            var options = {
                autoUpdateInput: false,
                locale: { format: 'YYYY-MM-DD', cancelLabel: 'Clear' },
                alwaysShowCalendars: true,
                maxDate: moment(),
                ranges: {
                    'Today': [moment(), moment()],
                    'Yesterday': [moment().subtract(1, 'days'), moment().subtract(1, 'days')],
                    'This week': [moment().startOf('isoWeek'), moment()],
                    'Last week': [lastWeek.clone().startOf('isoWeek'), lastWeek.clone().endOf('isoWeek')],
                    'Last 7 days': [moment().subtract(6, 'days'), moment()],
                    'Last 30 days': [moment().subtract(29, 'days'), moment()],
                    'This month': [moment().startOf('month'), moment()],
                    'Last month': [moment().subtract(1, 'month').startOf('month'), moment().subtract(1, 'month').endOf('month')]
                }
            };
            if (firstKept) {
                options.minDate = moment(String(firstKept), 'YYYY-MM-DD');
            }
            box.daterangepicker(options);

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
