
    // Handles visual updates when table switches are clicked
    function updateRow(el) {
        if (el.type === 'checkbox' && el.closest('td').querySelector('.active-label')) {
            const labelSpan = el.closest('td').querySelector('.active-label');
            labelSpan.textContent = el.checked ? 'ACTIVE' : 'HIDDEN';
            labelSpan.style.color = el.checked ? '#059669' : '#94a3b8';
        }
        
        const toast = document.getElementById('saveToast');
        toast.style.transform = 'translateY(0)';
        toast.style.opacity = '1';
        
        setTimeout(() => {
            toast.style.transform = 'translateY(100px)';
            toast.style.opacity = '0';
        }, 2500);
    }
