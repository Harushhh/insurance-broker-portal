
    $(document).ready(function () {
        $('.select2-searchable').select2({ width: '100%', placeholder: "Search..." });
    });

    function togglePolicyDetails(btn) {
        var list = btn.nextElementSibling;
        list.hidden = !list.hidden;
        btn.classList.toggle('expanded', !list.hidden);
    }

    // The per-axis breakdown lives in the <tr class="ovl-detail"> immediately
    // after the pair's own row, so walk up to that row and toggle its sibling.
    // The field-by-field grid is the .ovl-fields block inside the same pair card.
    function toggleOverlapDetail(btn) {
        var fields = btn.closest('.ovl-card').querySelector('.ovl-fields');
        if (!fields) { return; }
        fields.hidden = !fields.hidden;
        btn.textContent = fields.hidden ? 'All fields' : 'Hide fields';
    }

    (function () {
        var views = {
            filter: { tab: document.getElementById('tabFilterRecords'), panel: document.getElementById('filterRecordsView') },
            error: { tab: document.getElementById('tabErrorDashboard'), panel: document.getElementById('errorDashboardView') },
            grid: { tab: document.getElementById('tabGridSummary'), panel: document.getElementById('gridSummaryView') },
            overlap: { tab: document.getElementById('tabOverlaps'), panel: document.getElementById('overlapView') },
        };

        function setActiveView(view) {
            Object.keys(views).forEach(function (key) {
                var isActive = key === view;
                views[key].tab.setAttribute('aria-pressed', String(isActive));
                views[key].panel.hidden = !isActive;
            });
        }

        Object.keys(views).forEach(function (key) {
            views[key].tab.addEventListener('click', function () { setActiveView(key); });
        });

        var urlView = new URLSearchParams(window.location.search).get('view');
        if (Object.prototype.hasOwnProperty.call(views, urlView) && urlView !== 'filter') {
            setActiveView(urlView);
        }
    })();
