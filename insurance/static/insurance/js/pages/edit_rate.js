
$(document).ready(function() {
    // Helper function to inject checkboxes into the dropdown items
    function formatResult(state) {
        if (!state.id) { return state.text; }
        var isChecked = state.selected ? "checked" : "";
        var $state = $(
            '<div class="select2-checkbox-item">' +
                '<input type="checkbox" ' + isChecked + ' class="select2-fake-checkbox" /> ' +
                '<span class="select2-item-text">' + state.text + '</span>' +
            '</div>'
        );
        return $state;
    };

    // Apply the special pill + checkbox styling to ALL the multi-select dropdowns
    $('.select2-multiple').select2({
        closeOnSelect: false,
        templateResult: formatResult,
        width: '100%',

        // This sorting function pushes selected items to the top of the list!
        sorter: data => data.sort((a, b) => {
            if (a.selected == b.selected) {
                 return a.text.localeCompare(b.text);
            }
            return a.selected ? -1 : 1;
        })
    });

    // Once a field has more than a few selections, showing every pill inline makes the
    // box (and page) grow huge. Past this threshold, collapse them into one summary chip;
    // the full checklist is still there in the dropdown for viewing/editing.
    var PILL_COLLAPSE_THRESHOLD = 3;

    function updateSelectionSummary($select) {
        var $rendered = $select.next('.select2-container').find('.select2-selection__rendered');
        var $selectionBox = $select.next('.select2-container').find('.select2-selection--multiple');
        $rendered.find('.select2-summary-chip').remove();

        var count = ($select.val() || []).length;
        if (count > PILL_COLLAPSE_THRESHOLD) {
            $selectionBox.addClass('has-summary');
            $rendered.prepend(
                '<li class="select2-summary-chip">' + count + ' items selected — click to view/edit</li>'
            );
        } else {
            $selectionBox.removeClass('has-summary');
        }
    }

    // Automatically re-sort the list instantly upon checking/unchecking a box
    $('.select2-multiple').on("select2:select select2:unselect", function (e) {
        var $select = $(this);
        $select.select2("close");
        $select.select2("open");
        updateSelectionSummary($select);
    });

    // Collapse any fields that already have a long list selected on page load
    $('.select2-multiple').each(function() {
        updateSelectionSummary($(this));
    });

    // Default select2 for anything else (like numbers/dates if any)
    $('select:not(.select2-multiple)').select2({
        width: '100%'
    });
});
