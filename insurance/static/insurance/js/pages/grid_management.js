
    function openModal(id) { document.getElementById(id).style.display = 'flex'; }
    function closeModal(id) { document.getElementById(id).style.display = 'none'; }

    function gmEnforceWordLimit(el, counterId, limit) {
        let words = el.value.trim().length ? el.value.trim().split(/\s+/) : [];
        if (words.length > limit) {
            words = words.slice(0, limit);
            el.value = words.join(' ');
        }
        document.getElementById(counterId).textContent = words.length + ' / ' + limit + ' words';
    }

    // Searchable insurer dropdown
    function gmOpenInsurerDropdown() {
        document.getElementById('insurerDropdownList').classList.add('open');
    }

    function gmFilterInsurerOptions() {
        const query = document.getElementById('insurerSearchInput').value.trim().toLowerCase();
        const options = document.querySelectorAll('#insurerDropdownList .gm-combobox-option');
        let anyVisible = false;
        options.forEach(function (opt) {
            const match = opt.textContent.toLowerCase().includes(query);
            opt.classList.toggle('gm-hidden', !match);
            if (match) anyVisible = true;
        });
        document.getElementById('insurerEmptyState').style.display = anyVisible ? 'none' : 'block';
        document.getElementById('insurerNameHidden').value = '';
        gmOpenInsurerDropdown();
    }

    function gmSelectInsurer(el) {
        const value = el.getAttribute('data-value');
        document.getElementById('insurerSearchInput').value = value;
        document.getElementById('insurerNameHidden').value = value;
        document.getElementById('insurerSearchInput').style.borderColor = '';
        document.getElementById('insurerDropdownList').classList.remove('open');
    }

    function gmValidateGridUpload() {
        const hidden = document.getElementById('insurerNameHidden');
        if (!hidden.value) {
            const input = document.getElementById('insurerSearchInput');
            input.style.borderColor = '#ef4444';
            input.focus();
            return false;
        }
        return true;
    }

    // The month filter controls submit on change, so the Back button can bring
    // the page back with them at the value the user last picked (which already
    // navigated away) rather than the one this page is filtering by. The form's
    // autocomplete="off" covers a normal history load; this covers the
    // back/forward cache, which restores the live DOM as it was left.
    window.addEventListener('pageshow', function (e) {
        const filterForm = document.querySelector('.gm-month-filter-form');
        if (e.persisted && filterForm) filterForm.reset();
    });

    document.addEventListener('click', function (e) {
        const combobox = document.getElementById('insurerCombobox');
        if (combobox && !combobox.contains(e.target)) {
            document.getElementById('insurerDropdownList').classList.remove('open');
        }
    });
