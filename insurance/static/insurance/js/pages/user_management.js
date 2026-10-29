
    const USERS_DATA = JSON.parse(document.getElementById('users-data').textContent);
    // Mirrors RATE_CHECKER_GROUPS in insurance/views.py.
    const RATE_CHECKER_GROUPS = ['Can_View_Policy_Locker', 'Can_View_Health_Payout_Rates'];

    function openModal(id) { document.getElementById(id).style.display = 'flex'; }
    function closeModal(id) { document.getElementById(id).style.display = 'none'; }

    function openEditModal(id) {
        const u = USERS_DATA.find(row => String(row.id) === String(id));
        if (!u) return;

        document.getElementById('edit_action_input').value = 'update_user_access';
        document.getElementById('editModalTitle').innerText = "⚙️ Edit: " + u.username;
        document.getElementById('edit_user_id').value = u.id;
        document.getElementById('edit_email').value = u.email;
        document.getElementById('edit_contact').value = u.contact_number;
        document.getElementById('edit_designation').value = u.designation;
        document.getElementById('edit_reports_to').value = u.reports_to || '';
        document.getElementById('edit_is_qc').checked = !!u.is_qc;
        document.getElementById('edit_is_plvc').checked = !!u.is_plvc;
        document.getElementById('edit_personal_qc').checked = !!u.personal_qc;

        JSON.parse(document.getElementById('hierarchy-field-names').textContent).forEach(([field]) => {
            const el = document.getElementById('edit_' + field);
            if (el) el.value = u[field] || '';
        });

        let checks = document.querySelectorAll('.perm-check');
        let activePages = u.pages || [];

        checks.forEach(c => {
            c.setAttribute('name', 'pages_' + id);
            c.checked = c.value === 'Rate_Checker'
                ? RATE_CHECKER_GROUPS.every(g => activePages.includes(g))
                : activePages.includes(c.value);
        });

        openModal('editUserModal');
    }

    function resetPasswordAndSubmit() {
        if (confirm("Reset this user's password to a new randomly generated one-time password? You'll need to share it with them directly.")) {
            document.getElementById('edit_action_input').value = 'reset_password';
            document.getElementById('editUserForm').submit();
        }
    }

    function makeAdminAndSubmit() {
        if (confirm("Are you sure you want to promote this user to FULL ADMIN? They will have access to all pages.")) {
            document.getElementById('edit_action_input').value = 'make_admin';
            document.getElementById('editUserForm').submit();
        }
    }

    function removeAccessAndSubmit() {
        if (confirm("Are you sure you want to completely remove this user's access? They will not be able to view any dashboards.")) {
            let checks = document.querySelectorAll('.perm-check');
            checks.forEach(c => c.checked = false);

            document.getElementById('edit_action_input').value = 'update_user_access';
            document.getElementById('editUserForm').submit();
        }
    }

    function grantAccessAndSubmit() {
        if (confirm("Grant this user access to every dashboard page? This does not make them a Full Admin.")) {
            let checks = document.querySelectorAll('.perm-check');
            checks.forEach(c => c.checked = true);

            document.getElementById('edit_action_input').value = 'update_user_access';
            document.getElementById('editUserForm').submit();
        }
    }

    function grantRateCheckerOnlyAndSubmit() {
        if (confirm("Grant this user access to ONLY the Rate Checker (Motor/Health/Life rate lookup), removing every other page permission they currently have? They won't be able to view any other dashboard.")) {
            let checks = document.querySelectorAll('.perm-check');
            checks.forEach(c => c.checked = (c.value === 'Rate_Checker'));

            document.getElementById('edit_action_input').value = 'update_user_access';
            document.getElementById('editUserForm').submit();
        }
    }

    // ✅ NEW DELETE FUNCTION
    function deleteUserAndSubmit() {
        if (confirm("🚨 CRITICAL WARNING: Are you sure you want to completely delete this user from the system? This action cannot be undone.")) {
            document.getElementById('edit_action_input').value = 'delete_user';
            document.getElementById('editUserForm').submit();
        }
    }

    document.getElementById('userSearchInput').addEventListener('keyup', function() {
        let filter = this.value.toLowerCase();
        document.querySelectorAll('.user-row').forEach(row => {
            let targets = row.querySelectorAll('.search-target');
            let found = false;
            targets.forEach(t => { if (t.innerText.toLowerCase().includes(filter)) found = true; });
            row.style.display = found ? "" : "none";
        });
    });
