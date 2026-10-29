
    // Intercept form submission to show loading spinner and prevent double clicks
    document.getElementById('extractionForm').addEventListener('submit', function(e) {
        const fileInput = document.querySelector('input[type="file"]');
        if (!fileInput.value) return; // Let Django handle the standard "required" validation if empty

        const btn = document.getElementById('extractBtn');
        if (btn.disabled) {
            e.preventDefault();
            return;
        }
        
        // Disable the button and swap the UI state
        btn.disabled = true;
        document.getElementById('btnIcon').innerHTML = '<span class="spinner-icon"></span>';
        document.getElementById('btnText').innerHTML = 'AI Processing...';
    });
