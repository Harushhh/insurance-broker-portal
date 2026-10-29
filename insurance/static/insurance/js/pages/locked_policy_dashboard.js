
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".smart-dropdown").forEach(function (el) {
      if (el.tomselect) return;
      new TomSelect(el, {
        create: false,
        maxItems: 1,
        allowEmptyOption: true,
        placeholder: el.getAttribute("data-placeholder") || "Select...",
        searchField: ["text"],
        sortField: [{field: "text", direction: "asc"}],
        dropdownClass: "ts-dropdown",
        controlInput: "<input>",
        render: {
          no_results: function () {
            return '<div class="no-results" style="padding:10px; color:#64748b; font-size:13px;">No results found</div>';
          }
        }
      });
    });
  });
