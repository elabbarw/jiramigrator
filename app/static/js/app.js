$(document).ready(function() {
    // Store issue keys from last CSV upload (used on submit when CSV option is selected)
    let csvUploadedIssues = [];
    // Guard: ensure only one submission is sent (prevents double-click / double submit)
    let migrationFormSubmitting = false;

    // CRITICAL: Bind form submission handler FIRST to prevent default GET submission
    // This must be registered before any other code that could throw and break the ready callback
    $('#migrationForm').submit(function(e) {
        e.preventDefault();
        if (migrationFormSubmitting) return;
        migrationFormSubmitting = true;

        // Show loading indicator
        const submitButton = $(this).find('button[type="submit"]');
        const originalText = submitButton.text();
        submitButton.html('<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Submitting...');
        submitButton.prop('disabled', true);
        
        // Prepare data based on selected issue method
        const formData = prepareFormData();
        if ($('#csvOption').is(':checked') && (!formData.csv_data || formData.csv_data.length === 0)) {
            submitButton.html(originalText);
            submitButton.prop('disabled', false);
            migrationFormSubmitting = false;
            alert('Please upload a CSV file first.');
            return;
        }
        if ($('#projectOption').is(':checked') && (!formData.project_key || !String(formData.project_key).trim())) {
            submitButton.html(originalText);
            submitButton.prop('disabled', false);
            migrationFormSubmitting = false;
            alert('Please enter a project key.');
            return;
        }
        if ($('#jqlOption').is(':checked') && (!formData.jql || !String(formData.jql).trim())) {
            submitButton.html(originalText);
            submitButton.prop('disabled', false);
            migrationFormSubmitting = false;
            alert('Please enter a JQL query.');
            return;
        }
        // Submit form data
        $.ajax({
            url: '/api/migration/submit',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify(formData),
            success: function(response) {
                submitButton.html(originalText);
                submitButton.prop('disabled', false);
                migrationFormSubmitting = false;
                showStatusModal(response);
            },
            error: function(xhr) {
                submitButton.html(originalText);
                submitButton.prop('disabled', false);
                migrationFormSubmitting = false;
                // Show error
                let errorMessage = 'An error occurred submitting the migration job.';
                try {
                    const response = JSON.parse(xhr.responseText);
                    errorMessage = response.detail || errorMessage;
                } catch (e) {
                    errorMessage = xhr.responseText || errorMessage;
                }
                
                alert('Error: ' + errorMessage);
            }
        });
    });

    // Mark that the jQuery submit handler was successfully bound
    window._migrationFormReady = true;

    // Initialize default values with URL parameter support
    try {
        initializeFormValues();
    } catch (err) {
        console.error('Error initializing form values:', err);
    }

    // Handle export method change
    $('#export_method').change(function() {
        const exportMethod = $(this).val();
        if (exportMethod === 'api') {
            $('#sharepointOptions').hide();
            $('#apiOptions').show();
            handleApiTypeChange();
        } else {
            $('#apiOptions').hide();
            $('#sharepointOptions').show();
        }
    });

    // Handle API type change
    $('#api_type').change(function() {
        handleApiTypeChange();
    });

    // Handle issue selection method change
    $('input[name="issueSelection"]').change(function() {
        const selection = $(this).val();
        
        // Hide all selection fields
        $('#jqlFields, #projectFields, #csvFields').hide();
        
        // Show the selected field
        if (selection === 'jql') {
            $('#jqlFields').show();
        } else if (selection === 'project') {
            $('#projectFields').show();
        } else if (selection === 'csv') {
            $('#csvFields').show();
        }
    });
    
    // Trigger initial state sync for radio buttons (handles case where user clicked before JS loaded)
    $('input[name="issueSelection"]:checked').trigger('change');

    // Toggle email field visibility based on checkbox
    $('#send_email').change(function() {
        if ($(this).is(':checked')) {
            $('#emailField').show();
        } else {
            $('#emailField').hide();
        }
    });

    // Toggle manual fields section visibility based on checkbox
    $('#include_manual_fields').change(function() {
        if ($(this).is(':checked')) {
            $('#manualFieldsSection').show();
        } else {
            $('#manualFieldsSection').hide();
        }
    });

    // Handle CSV file upload: store parsed keys and show count
    $('#csvFile').change(function() {
        const file = this.files[0];
        if (!file) return;

        const formData = new FormData();
        formData.append('file', file);

        const $input = $(this);
        $input.prop('disabled', true);
        $('#csvKeysStatus').hide();

        $.ajax({
            url: '/api/migration/csv-upload',
            method: 'POST',
            data: formData,
            processData: false,
            contentType: false,
            success: function(response) {
                $input.prop('disabled', false);
                csvUploadedIssues = response.issues || [];
                window._csvUploadedIssues = csvUploadedIssues;
                var n = csvUploadedIssues.length;
                $('#csvKeysCount').text(n);
                $('#csvKeysStatus').show();
            },
            error: function(xhr) {
                $input.prop('disabled', false);
                csvUploadedIssues = [];
                let errorMessage = 'Error uploading CSV file.';
                try {
                    const response = JSON.parse(xhr.responseText);
                    errorMessage = response.detail || errorMessage;
                } catch (e) {
                    errorMessage = xhr.responseText || errorMessage;
                }
                alert('Error: ' + errorMessage);
                $input.val('');
            }
        });
    });

    // Helper function to prepare form data
    function prepareFormData() {
        const formData = {
            vault_url: $('#vault_url').val(),
            vault_name: $('#vault_name').val(),
            vault_token: $('#vault_token').val(),
            export_method: $('#export_method').val(),
            parallelism: parseInt($('#parallelism').val()) || 5
        };
        
        // Set export method specific fields
        if (formData.export_method === 'sharepoint') {
            formData.sharepoint_site = $('#sharepoint_site').val();
            formData.sharepoint_folder = $('#sharepoint_folder').val();
        } else if (formData.export_method === 'api') {
            formData.api_type = 'custom';
            formData.custom_api_url = $('#custom_api_url').val();
        }
        
        // Set issue selection fields (use radio checked state so csv_data is never missed)
        if ($('#jqlOption').is(':checked')) {
            formData.jql = ($('#jql').val() || '').trim();
        }
        if ($('#projectOption').is(':checked')) {
            formData.project_key = ($('#project_key').val() || '').trim();
        }
        if ($('#csvOption').is(':checked')) {
            formData.csv_data = csvUploadedIssues;
        }
        
        // Set email settings
        if ($('#send_email').is(':checked')) {
            formData.send_email = true;
            formData.email = $('#email').val();
        }
        
        // Set custom fields options
        formData.analyze_fields = $('#analyze_fields').is(':checked');
        formData.show_all_fields = $('#show_all_fields').is(':checked');
        formData.use_view_screen = $('#use_view_screen').is(':checked');
        
        // Process identifier field terms as an array
        if ($('#identifier_field_terms').val()) {
            formData.identifier_field_terms = $('#identifier_field_terms').val()
                .split(',')
                .map(term => term.trim())
                .filter(term => term.length > 0);
        }
        
        // Process important field terms as an array
        if ($('#important_field_terms').val()) {
            formData.important_field_terms = $('#important_field_terms').val()
                .split(',')
                .map(term => term.trim())
                .filter(term => term.length > 0);
        }
        
        // Handle manual fields
        formData.include_manual_fields = $('#include_manual_fields').is(':checked');
        if (formData.include_manual_fields && $('#manual_fields').val()) {
            formData.manual_fields = $('#manual_fields').val()
                .split('\n')
                .map(field => field.trim())
                .filter(field => field.length > 0);
        }
        
        return formData;
    }

    // Function to show status modal
    function showStatusModal(response) {
        $('#jobId').text(response.job_id);
        $('#jobStatus').html(`<span class="badge bg-secondary">${response.status}</span>`);
        $('#statusPageLink').attr('href', `/status/${response.job_id}`);
        $('#resultSection, #errorSection').hide();
        
        // Show the modal
        const statusModal = new bootstrap.Modal(document.getElementById('statusModal'));
        statusModal.show();
        
        // Start polling for status
        pollJobStatus(response.job_id);
    }

    // Function to poll job status
    function pollJobStatus(jobId) {
        $.ajax({
            url: `/api/migration/status/${jobId}`,
            method: 'GET',
            success: function(response) {
                // Update status
                $('#jobStatus').html(`<span class="badge ${getStatusBadgeClass(response.status)}">${response.status}</span>`);
                
                // Update result or error if available
                if (response.result) {
                    $('#resultSection').show();
                    $('#jobResult').text(JSON.stringify(response.result, null, 2));
                } else {
                    $('#resultSection').hide();
                }
                
                if (response.error) {
                    $('#errorSection').show();
                    $('#jobError').text(response.error);
                } else {
                    $('#errorSection').hide();
                }
                
                // Continue polling if job is still running
                if (['PENDING', 'STARTED', 'RETRY'].includes(response.status)) {
                    setTimeout(function() {
                        pollJobStatus(jobId);
                    }, 2000);
                }
            },
            error: function() {
                // Stop polling on error
                $('#jobStatus').html(`<span class="badge bg-danger">Error fetching status</span>`);
            }
        });
    }

    // Helper function to get status badge class
    function getStatusBadgeClass(status) {
        switch(status) {
            case 'SUCCESS':
            case 'completed':
                return 'bg-success';
            case 'FAILURE':
            case 'failed':
                return 'bg-danger';
            case 'STARTED':
                return 'bg-primary';
            case 'RETRY':
                return 'bg-warning';
            case 'PENDING':
            case 'submitted':
            default:
                return 'bg-secondary';
        }
    }

    // Function to handle API type change (simplified - only custom API supported)
    function handleApiTypeChange() {
        // Custom API is the only option now
        $('#customApiOptions').show();
    }

    // Function to initialize form values from URL parameters or localStorage
    function initializeFormValues() {
        // Get URL parameters
        const urlParams = new URLSearchParams(window.location.search);
        
        // Set form values from URL parameters or localStorage
        $('#vault_url').val(urlParams.get('vault_url') || localStorage.getItem('vault_url') || '');
        $('#vault_name').val(urlParams.get('vault_name') || localStorage.getItem('vault_name') || '');
        
        const exportMethod = urlParams.get('export_method') || localStorage.getItem('export_method') || 'sharepoint';
        $('#export_method').val(exportMethod);
        
        // Trigger change event to update UI
        $('#export_method').trigger('change');
        
        // Remember these values for next time
        $('#vault_url, #vault_name, #export_method').change(function() {
            localStorage.setItem($(this).attr('id'), $(this).val());
        });
    }
}); 