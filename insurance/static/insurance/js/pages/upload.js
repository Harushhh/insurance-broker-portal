
    const CHUNK_SIZE = 2500; 
    const API_URL = '/api/upload-chunk/';

    async function startUpload() {
        const fileInput = document.getElementById('csvFileInput');
        const targetTable = document.getElementById('uploadTypeSelect').value;
        const csrfToken = document.querySelector('[name=csrfmiddlewaretoken]').value;
        
        if (!fileInput.files.length) {
            alert('Please select a CSV file first.');
            return;
        }

        const file = fileInput.files[0];
        const btn = document.getElementById('uploadBtn');
        const progressContainer = document.getElementById('progressContainer');
        const progressBar = document.getElementById('progressBar');
        const statusText = document.getElementById('statusText');
        const percentageText = document.getElementById('percentageText');

        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span> Checking file...';
        progressContainer.style.display = 'block';
        progressBar.style.width = '0%';
        progressBar.className = 'progress-bar progress-bar-striped progress-bar-animated bg-info';
        percentageText.innerText = '';
        statusText.innerText = "Checking whether this file was already uploaded...";

        // Fingerprints the raw bytes so a retry (after a timeout/error) or an
        // accidental re-pick of the same file is recognized as such regardless
        // of row content -- this is what api_check_duplicate_upload matches on.
        const fileBuffer = await file.arrayBuffer();
        const hashBuffer = await crypto.subtle.digest('SHA-256', fileBuffer);
        const fileHash = Array.from(new Uint8Array(hashBuffer)).map(b => b.toString(16).padStart(2, '0')).join('');

        let attemptId = null;
        try {
            const checkResponse = await fetch('/api/check-duplicate-upload/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify({ target_table: targetTable, file_hash: fileHash, file_name: file.name })
            });
            const checkData = await checkResponse.json();
            attemptId = checkData.attempt_id || null;

            if (checkData.prior_upload) {
                const p = checkData.prior_upload;
                const outcome = p.status === 'COMPLETED'
                    ? `completed successfully (${p.row_count != null ? p.row_count.toLocaleString() : 'unknown'} rows saved)`
                    : (p.status === 'FAILED' ? 'failed partway through' : 'never finished (crashed or was abandoned partway through)');
                const proceed = confirm(
                    `This exact file was already uploaded here on ${p.uploaded_at} by ${p.uploaded_by}, and ${outcome}.\n\n` +
                    `Uploading it again may create duplicate rows. Continue anyway?`
                );
                if (!proceed) {
                    btn.disabled = false;
                    btn.innerHTML = '<i class="bi bi-cloud-arrow-up me-2"></i> Start Upload';
                    progressContainer.style.display = 'none';
                    return;
                }
            }
        } catch (error) {
            console.error('Duplicate-upload check failed, proceeding without it:', error);
        }

        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span> Uploading...';
        progressBar.className = 'progress-bar progress-bar-striped progress-bar-animated bg-primary';
        statusText.innerText = "Reading file locally (No Server RAM used)...";

        Papa.parse(file, {
            header: true,
            skipEmptyLines: true,
            complete: async function(results) {
                const allRows = results.data;
                const totalRows = allRows.length;

                // One id per upload (this click of the button), sent with every
                // chunk of this file -- so rate_master rows split across chunk
                // requests still recognize each other as the same upload, while
                // a separate upload (even seconds later) always gets its own.
                const uploadBatchId = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

                // Sends every chunk of the file through the server once, either
                // as a read-only check (dryRun=true -- the server validates but
                // writes nothing) or the real save (dryRun=false). Stops and
                // reports the error on the first chunk that fails, without
                // sending any chunks after it.
                async function runPass(dryRun, label) {
                    let rowsProcessed = 0;
                    for (let i = 0; i < totalRows; i += CHUNK_SIZE) {
                        const chunk = allRows.slice(i, i + CHUNK_SIZE);

                        try {
                            const response = await fetch(API_URL, {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-CSRFToken': csrfToken
                                },
                                body: JSON.stringify({
                                    target_table: targetTable,
                                    rows: chunk,
                                    upload_batch_id: uploadBatchId,
                                    dry_run: dryRun
                                })
                            });

                            if (!response.ok) {
                                const errData = await response.json();
                                throw new Error(errData.message || 'Server rejected the batch.');
                            }

                            rowsProcessed += chunk.length;
                            const percentage = Math.round((rowsProcessed / totalRows) * 100);

                            progressBar.style.width = percentage + '%';
                            percentageText.innerText = percentage + '%';
                            statusText.innerText = `${label} (${rowsProcessed.toLocaleString()} / ${totalRows.toLocaleString()})`;

                        } catch (error) {
                            console.error('Error:', error);
                            statusText.innerText = `⚠️ Upload failed: ${error.message}`;
                            progressBar.className = 'progress-bar bg-danger';
                            btn.disabled = false;
                            btn.innerHTML = '<i class="bi bi-cloud-arrow-up me-2"></i> Retry Upload';
                            if (attemptId) {
                                fetch('/api/complete-upload/', {
                                    method: 'POST',
                                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                                    body: JSON.stringify({ attempt_id: attemptId, success: false, row_count: rowsProcessed })
                                }).catch(() => {});
                            }
                            return false;
                        }
                    }
                    return true;
                }

                // Pass 1: check every chunk, save nothing. If anything in the
                // file is invalid, this stops here with nothing written to the
                // database at all -- so fixing the bad rows and retrying can
                // never create duplicates from rows that "already went through",
                // because on a failed upload none of them ever did.
                progressBar.style.width = '0%';
                progressBar.className = 'progress-bar progress-bar-striped progress-bar-animated bg-info';
                const validated = await runPass(true, 'Validating file...');
                if (!validated) return;

                // Pass 2: the whole file already checked out clean, so this is
                // the real save.
                progressBar.style.width = '0%';
                progressBar.className = 'progress-bar progress-bar-striped progress-bar-animated bg-primary';
                const saved = await runPass(false, 'Saving to database...');
                if (!saved) return;

                statusText.innerText = "Upload Complete!";
                progressBar.className = 'progress-bar bg-success';
                btn.disabled = false;
                btn.innerHTML = '<i class="bi bi-check-circle me-2"></i> Upload Successful';

                if (attemptId) {
                    fetch('/api/complete-upload/', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                        body: JSON.stringify({ attempt_id: attemptId, success: true, row_count: totalRows })
                    }).catch(() => {});
                }

                fileInput.value = '';
            }
        });
    }
