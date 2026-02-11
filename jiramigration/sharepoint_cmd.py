import argparse
import csv
import os
import shutil
import threading
import json
import platform
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.auth import HTTPBasicAuth
import pdfkit
from atlassian import Jira
from tqdm import tqdm

# Import modules - try relative import first (when used as package), then absolute (standalone)
try:
    from .modules import SharepointUpload, PatternMatcher
    from .modules.uploadsharepoint import normalize_sharepoint_path
except ImportError:
    from modules import SharepointUpload, PatternMatcher
    from modules.uploadsharepoint import normalize_sharepoint_path
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import time
from office365.runtime.client_request_exception import ClientRequestException

# Configure requests to use a larger connection pool to support high parallelism
requests.adapters.DEFAULT_POOLSIZE = 100  # Increase from default of 10
# Create a session with larger pool_maxsize
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
session.mount('http://', adapter)
session.mount('https://', adapter)

# Set up your app registration information
client_id = os.getenv('SHAREPOINT_CLIENT_ID', 'your-client-id')
authority = f"https://login.microsoftonline.com/{os.getenv('AZURE_TENANT_ID', 'your-tenant-id')}"

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
        return os.path.join(base_path, relative_path)
    except Exception:
        # Check multiple possible locations
        possible_paths = [
            os.path.join(os.path.dirname(__file__), relative_path),  # Same dir as this file
            os.path.join(os.path.abspath("."), "certs", relative_path),  # certs/ dir
            os.path.join(os.path.abspath("."), relative_path),       # Current working dir
            os.path.join(os.path.abspath("."), "jiramigration", relative_path),  # jiramigration subdir
            os.path.join("/app/certs", relative_path),               # Docker certs path
            os.path.join("/app/jiramigration", relative_path),       # Docker path
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
        # Return first option as default (will fail later with clear error)
        return possible_paths[0]

# Load patterns.json using resource path (now directly from root)
patterns_file = None # Initialize to prevent potential UnboundLocalError in except block
try:
    patterns_file = get_resource_path('patterns.json') # Removed 'modules/'
    with open(patterns_file) as json_file:
        patterns_data = json.load(json_file)
        patterns = patterns_data.get("patterns", [])
        matcher = PatternMatcher(patterns)
except Exception as e:
    print(f"Warning: Could not load patterns.json: {e}")
    if patterns_file:
        print(f"Looking for patterns.json at: {patterns_file}")
    else:
        print("Could not determine path for patterns.json")
    patterns = []
    # Initialize matcher even if patterns fail to load, maybe with empty patterns
    # depending on how PatternMatcher handles it.
    if 'PatternMatcher' in globals():
        matcher = PatternMatcher(patterns)
    else:
        # Handle case where PatternMatcher couldn't be imported (e.g., model download failed)
        print("Error: PatternMatcher class not available. Cannot initialize matcher.")
        sys.exit("Exiting due to missing PatternMatcher.") 

# Load your certificate and private key (now directly from root)
cert_file = get_resource_path('certificate.crt') # Removed 'modules/'
key_file = get_resource_path('certificate.pem')   # Removed 'modules/'

def normalize_filename(filename):
    """
    Normalize a filename by replacing spaces and removing special characters
    that could cause issues in various environments.
    
    Args:
        filename (str): The original filename
        
    Returns:
        str: A normalized filename safe for most file systems and processes
    """
    import re
    
    # Replace spaces with underscores
    normalized = filename.replace(' ', '_')
    
    # Replace multiple consecutive underscores with a single one
    normalized = re.sub(r'_{2,}', '_', normalized)
    
    # Remove special characters that can cause issues
    normalized = re.sub(r'[^\w\-\.\(\)_]', '', normalized)
    
    # Ensure filename doesn't start or end with underscore
    normalized = normalized.strip('_')
    
    # If the filename got emptied out, use a default
    if not normalized:
        normalized = "file"
        
    return normalized


def shorten_filename(name, max_length=200):
    """
    Shorten a filename if it exceeds max_length
    """
    if len(name) <= max_length:
        return name
    
    # Split the name into parts
    base, ext = os.path.splitext(name)
    
    # Determine the length to keep from base
    keep_length = max_length - len(ext) - 1
    
    # Ensure keep_length is positive
    keep_length = max(1, keep_length)
    
    # Shorten the base
    shortened_base = base[:keep_length]
    
    # Combine the shortened base and extension
    return shortened_base + ext


class JiraMigration:
    def __init__(self, args):
        self.args = args
        
        # Progress callback for reporting migration progress
        self._progress_callback = None
        
        # Create a Jira client with the session that has a larger connection pool
        self.jira = Jira(
            url=args.vault_url,
            username=args.vault_name,
            password=args.vault_token,
            session=session  # Use our custom session with larger pool
        )
        
        # Store max_retries for retry decorator
        self.max_retries = args.max_retries if hasattr(args, 'max_retries') else 3
        
        try:
            # Ensure cert_file and key_file paths are checked for existence before opening
            if not os.path.exists(cert_file):
                 raise FileNotFoundError(f"Certificate file not found at {cert_file}")
            if not os.path.exists(key_file):
                 raise FileNotFoundError(f"Key file not found at {key_file}")
                 
            with open(cert_file, 'r') as f:
                cert = f.read()
            with open(key_file, 'r') as f:
                certkey = f.read()
        except Exception as e:
            print(f"Error loading certificate files: {e}")
            print(f"Looking for certificates at:")
            print(f"cert_file: {cert_file}")
            print(f"key_file: {key_file}")
            print("Ensure these files were correctly bundled by PyInstaller.")
            raise # Re-raise the exception to stop execution

        self.vault_name_entry = args.vault_name
        self.vault_token_entry = args.vault_token
        self.vault_url_entry = args.vault_url
        # Ensure SharePoint is the default export method
        self.export_method = args.export_method if hasattr(args, 'export_method') else 'sharepoint'
        self.sharepoint_folder_entry = args.sharepoint_folder if hasattr(args, 'sharepoint_folder') else None
        self.project_key = args.project_key if hasattr(args, 'project_key') else None
        self.api_type = args.api_type if hasattr(args, 'api_type') else 'custom'
        self.custom_api_url = args.custom_api_url if hasattr(args, 'custom_api_url') else None
        self.custom_api_headers = args.custom_api_headers if hasattr(args, 'custom_api_headers') else None
        self.custom_api_params = args.custom_api_params if hasattr(args, 'custom_api_params') else None
        
        # Create a base temp directory for all file operations
        self.base_temp_dir = tempfile.mkdtemp(prefix="jira_migration_")
        print(f"Using temporary directory for file operations: {self.base_temp_dir}")
        
        # Get all field definitions once during initialization
        self.field_mapping = self._get_field_mapping()
        
        # Cache for project-specific custom fields
        self.project_custom_fields = {}
        
        # Initialize the SharePoint uploader only if export method is SharePoint
        self.uploadsharepoint = None  # Initialize to None first
        if self.export_method == 'sharepoint' and hasattr(args, 'sharepoint_site') and args.sharepoint_site:
            try:
                self.uploadsharepoint = SharepointUpload(args.sharepoint_site, client_id, certkey)
            except Exception as e:
                print(f"Warning: Could not initialize SharePoint uploader: {e}")
                self.uploadsharepoint = None  # Ensure it's None if initialization fails
        
        # Configure PDF generation based on operating system
        self.configure_pdfkit()
        
    def __del__(self):
        """Clean up temporary files when object is destroyed"""
        try:
            if hasattr(self, 'base_temp_dir') and os.path.exists(self.base_temp_dir):
                shutil.rmtree(self.base_temp_dir)
                print(f"Cleaned up temporary directory: {self.base_temp_dir}")
        except Exception as e:
            print(f"Warning: Could not clean up temporary directory: {e}")
        
    def configure_pdfkit(self):
        """Configure pdfkit based on operating system"""
        self.pdfkit_config = None
        
        # First, try to find the bundled wkhtmltopdf binary
        if platform.system() == 'Windows':
            # Check bundled binary first - for PyInstaller bundle
            bundled_path = get_resource_path(os.path.join('bin', 'windows', 'wkhtmltopdf.exe'))
            if os.path.exists(bundled_path):
                self.pdfkit_config = pdfkit.configuration(wkhtmltopdf=bundled_path)
                print(f"Using bundled wkhtmltopdf from: {bundled_path}")
                return
                
            # Common installation paths for wkhtmltopdf on Windows
            possible_paths = [
                'C:\\Program Files\\wkhtmltopdf\\bin\\wkhtmltopdf.exe',
                'C:\\Program Files (x86)\\wkhtmltopdf\\bin\\wkhtmltopdf.exe',
                self.args.wkhtmltopdf_path if hasattr(self.args, 'wkhtmltopdf_path') else None
            ]
        elif platform.system() == 'Darwin':  # macOS
            # Check bundled binary first - for PyInstaller bundle
            bundled_path = get_resource_path(os.path.join('bin', 'macos', 'wkhtmltopdf'))
            if os.path.exists(bundled_path):
                # Skip trying to set permissions on bundled binary - it's in a read-only location
                # Just use it as is
                self.pdfkit_config = pdfkit.configuration(wkhtmltopdf=bundled_path)
                print(f"Using bundled wkhtmltopdf from: {bundled_path}")
                return
                
            # Common installation paths for wkhtmltopdf on macOS
            possible_paths = [
                '/usr/local/bin/wkhtmltopdf',
                '/usr/bin/wkhtmltopdf',
                '/opt/homebrew/bin/wkhtmltopdf',
                self.args.wkhtmltopdf_path if hasattr(self.args, 'wkhtmltopdf_path') else None
            ]
        else:  # Linux or other
            # Detect architecture for Linux binary selection
            import struct
            arch = 'arm64' if struct.calcsize('P') * 8 == 64 and platform.machine() in ('aarch64', 'arm64') else 'amd64'
            binary_name = f'wkhtmltopdf_{arch}'
            
            # Check bundled binary first - multiple possible locations
            bundled_paths = [
                get_resource_path(os.path.join('bin', 'linux', binary_name)),
                get_resource_path(os.path.join('jiramigration', 'bin', 'linux', binary_name)),
                os.path.join(os.path.dirname(__file__), 'bin', 'linux', binary_name),
                f'/app/jiramigration/bin/linux/{binary_name}',
                # Also try the generic name as fallback
                get_resource_path(os.path.join('bin', 'linux', 'wkhtmltopdf')),
                os.path.join(os.path.dirname(__file__), 'bin', 'linux', 'wkhtmltopdf'),
                '/app/jiramigration/bin/linux/wkhtmltopdf',
            ]
            for bundled_path in bundled_paths:
                if os.path.exists(bundled_path):
                    # Skip trying to set permissions on bundled binary if in a read-only location
                    self.pdfkit_config = pdfkit.configuration(wkhtmltopdf=bundled_path)
                    print(f"Using bundled wkhtmltopdf from: {bundled_path}")
                    return
                
            # Common installation paths for wkhtmltopdf on Linux
            possible_paths = [
                '/usr/local/bin/wkhtmltopdf',
                '/usr/bin/wkhtmltopdf',
                self.args.wkhtmltopdf_path if hasattr(self.args, 'wkhtmltopdf_path') else None
            ]
            
        # If we get here, we didn't find a bundled binary, so try the system paths
        for path in possible_paths:
            if path and os.path.exists(path):
                self.pdfkit_config = pdfkit.configuration(wkhtmltopdf=path)
                print(f"Using wkhtmltopdf from: {path}")
                return
                
        # Try to find wkhtmltopdf in system PATH
        try:
            import subprocess
            # Check if wkhtmltopdf is available in PATH
            if platform.system() == 'Windows':
                process = subprocess.run(['where', 'wkhtmltopdf'], capture_output=True, text=True, check=False)
            else:
                process = subprocess.run(['which', 'wkhtmltopdf'], capture_output=True, text=True, check=False)
            
            if process.returncode == 0 and process.stdout.strip():
                path = process.stdout.strip().split('\n')[0] if '\n' in process.stdout.strip() else process.stdout.strip()
                self.pdfkit_config = pdfkit.configuration(wkhtmltopdf=path)
                print(f"Using wkhtmltopdf from PATH: {path}")
                return
        except Exception as e:
            print(f"Error finding wkhtmltopdf in PATH: {e}")
        
        print("Warning: wkhtmltopdf not found in bundled location or common system paths. " 
              "PDF generation may fail unless you provide a path using --wkhtmltopdf-path")
        print("PDF generation will attempt to use default system configuration, which may not work")

    def set_progress_callback(self, callback):
        """
        Set a callback function to report migration progress.
        
        The callback will be invoked after each issue is processed with the following signature:
        callback(issue_key, processed, total, status, error=None, extracted_identifier=None)

        Args:
            callback: A callable that accepts (issue_key, processed, total, status, error, extracted_identifier)
                - issue_key (str): The Jira issue key that was processed
                - processed (int): Number of issues processed so far
                - total (int): Total number of issues to process
                - status (str): 'success' or 'failed'
                - error (str, optional): Error message if status is 'failed'
                - extracted_identifier (str, optional): Extracted identifier found in the issue, if any
        """
        self._progress_callback = callback

    def _get_field_mapping(self):
        """Get mapping from field IDs to their display names."""
        field_mapping = {}
        try:
            # Get all field definitions from Jira
            fields = self.jira.get_all_fields()
            for field in fields:
                if 'id' in field and 'name' in field:
                    field_mapping[field['id']] = field['name']
        except Exception as e:
            print(f"Warning: Could not fetch field definitions: {e}")
        return field_mapping
    

    
    def _get_project_custom_fields(self, project_key):
        """Get custom fields that are associated with a specific project."""
        custom_fields = {}
        try:
            # Get all custom fields directly from Jira API
            all_custom_fields = self.jira.get_custom_fields() 
            
            # Store all custom fields
            for field in all_custom_fields:
                if 'id' in field and 'name' in field:
                    custom_fields[field['id']] = field['name']
            
            return custom_fields
        except Exception as e:
            print(f"Warning: Could not fetch custom fields for project {project_key}: {e}")
        
        return custom_fields

    def start_migration(self, issues_list=None):
        if issues_list is not None:
            # Use explicitly provided issues list (for retry-failed only mode)
            all_issues = issues_list
        elif self.args.csv_file:
            all_issues = self.read_issues_from_csv(self.args.csv_file)
        else:
            all_issues = self.get_jira_issues(self.args.jql)

        return self.run_migration(all_issues)

    def read_issues_from_csv(self, csv_file):
        with open(csv_file, 'r') as file:
            reader = csv.DictReader(file)
            issues = []
            issue_part_candidates = ['issue', 'number', 'issue number', 'issue_id', 'id', 'no', 'num', 'issue key']
            for row in reader:
                key = row.get('key', '').strip()
                if not key:
                    continue
                # If key doesn't look like a full Jira key (no hyphen), try combining with another column (e.g. Key=CI, Issue=T-96)
                if '-' not in key and reader.fieldnames:
                    header_lower = {h.strip().lower(): h for h in reader.fieldnames}
                    key_col_lower = 'key'
                    for cand in issue_part_candidates:
                        if cand in header_lower:
                            part = row.get(header_lower[cand], '').strip()
                            if part:
                                combined = key + part if part.startswith('-') else (key + '-' + part if part.isdigit() else key + part)
                                if combined and '-' in combined:
                                    key = combined
                                    break
                    if '-' not in key:
                        for fn in reader.fieldnames:
                            if fn.strip().lower() == key_col_lower:
                                continue
                            part = row.get(fn, '').strip()
                            if part:
                                combined = key + part if part.startswith('-') else (key + '-' + part if part.isdigit() else key + part)
                                if combined and '-' in combined:
                                    key = combined
                                    break
                if key:
                    issues.append(key)
            return issues

    def get_jira_issues(self, jql):
        all_issues = []
        start_at = 0
        max_results = 100  # You can adjust this value, but 100 is a good balance

        while True:
            issues_data = self.jira.jql(jql, start=start_at, limit=max_results)
            
            current_batch_issues = issues_data.get('issues', [])
            if not current_batch_issues:
                break
            
            all_issues.extend(current_batch_issues)
            
            if len(current_batch_issues) < max_results:
                break
            
            start_at += max_results

        # Safely extract keys from the fetched issues
        issue_keys = []
        for issue_dict in all_issues:
            key = issue_dict.get('key')
            if key:
                issue_keys.append(key)
        return issue_keys

    def run_migration(self, all_issues):
        max_parallel_tasks = self.args.parallelism
        total = len(all_issues)
        processed = 0
        
        # Create or open a file to track failed issues
        failed_issues_file = 'failed_issues.csv'
        failed_issues = []
        
        # Only retry failed issues if explicitly requested via --retry-failed flag
        # The web app handles resume/retry through explicit job actions, not automatic file reading
        if hasattr(self.args, 'retry_failed') and self.args.retry_failed and os.path.exists(failed_issues_file):
            try:
                with open(failed_issues_file, 'r') as file:
                    reader = csv.DictReader(file)
                    # Safely extract keys for previously_failed_issues
                    previous_failed_issues = []
                    for row in reader:
                        key = row.get('key')
                        # Ensure 'retried' field is also safely accessed
                        if key and row.get('retried', '').lower() != 'yes':
                            previous_failed_issues.append(key)
                if previous_failed_issues:
                    print(f"Found {len(previous_failed_issues)} previously failed issues to retry")
                    # Add previously failed issues to the current batch
                    all_issues = previous_failed_issues + all_issues
                    total = len(all_issues)
            except Exception as e:
                print(f"Error reading previous failed issues: {e}")
        
        print(f"Starting migration of {total} issues with parallelism of {max_parallel_tasks}")
        
        # Process the issues in batches to avoid potential thread pool issues
        with tqdm(total=total, desc="Migrating Issues") as pbar:
            # Use a single ThreadPoolExecutor for all jobs rather than creating one per batch
            with ThreadPoolExecutor(max_workers=max_parallel_tasks) as executor:
                # Create a list to track temp directories
                all_temp_dirs = []
                
                # Track all futures
                futures_to_issues = {}
                
                # Submit all jobs up front
                for issue in all_issues:
                    future = executor.submit(self.migrate_jira_issue, issue, all_temp_dirs)
                    futures_to_issues[future] = issue
                
                # Open log file once
                with open('migration_log.csv', 'a') as log_file:
                    writer = csv.writer(log_file)
                    writer.writerow(['Jira Key', 'Result'])
                    
                    # Process futures as they complete
                    for future in as_completed(futures_to_issues):
                        issue = futures_to_issues[future]
                        issue_status = 'failed'
                        issue_error = None
                        issue_extracted_identifier = None
                        issue_pdf_generated = False
                        issue_attachments_count = 0
                        issue_files_uploaded = 0
                        
                        try:
                            result = future.result()
                            # Handle both dict (new format) and boolean (legacy) return types
                            if isinstance(result, dict):
                                success = result.get('success', False)
                                issue_extracted_identifier = result.get('extracted_identifier')
                                issue_error = result.get('error')
                                issue_pdf_generated = result.get('pdf_generated', False)
                                issue_attachments_count = result.get('attachments_count', 0)
                                issue_files_uploaded = result.get('files_uploaded', 0)
                            else:
                                # Legacy boolean return
                                success = bool(result)
                                issue_error = 'Migration returned False' if not success else None
                            
                            if success:
                                pbar.set_postfix_str(f"Successfully migrated {issue}")
                                writer.writerow([issue, 'Success'])
                                issue_status = 'success'
                            else:
                                writer.writerow([issue, 'Failed'])
                                pbar.set_postfix_str(f"Failed to migrate {issue}")
                                issue_status = 'failed'
                                # Add to failed issues list
                                failed_issues.append({'key': issue, 'error': issue_error or 'Migration failed', 'retried': 'no'})
                        except Exception as err:
                            # Get detailed error info including traceback
                            import traceback
                            error_traceback = traceback.format_exc()
                            error_msg = str(err)
                            err_type = type(err).__name__
                            
                            # Write more detailed information to log file
                            writer.writerow([issue, f'Error: {err_type}: {error_msg[:100]}'])
                            
                            # Print detailed information to console
                            pbar.set_postfix_str(f"Error with {issue}: {err_type}")
                            print(f"Error migrating {issue}: {err_type}: {error_msg}")
                            
                            # Add to failed issues list with error message and type
                            failed_issues.append({
                                'key': issue, 
                                'error_type': err_type,
                                'error': error_msg[:200], 
                                'retried': 'no'
                            })
                            issue_status = 'failed'
                            issue_error = f"{err_type}: {error_msg[:200]}"
                        finally:
                            pbar.update(1)
                            processed += 1
                            
                            # Call progress callback if set
                            if self._progress_callback:
                                try:
                                    self._progress_callback(
                                        issue_key=issue,
                                        processed=processed,
                                        total=total,
                                        status=issue_status,
                                        error=issue_error,
                                        extracted_identifier=issue_extracted_identifier,
                                        pdf_generated=issue_pdf_generated,
                                        attachments_count=issue_attachments_count,
                                        files_uploaded=issue_files_uploaded
                                    )
                                except Exception as callback_err:
                                    print(f"Warning: Progress callback failed for {issue}: {callback_err}")
                
                # Clean up all temp directories after all jobs are done
                for temp_dir in all_temp_dirs:
                    try:
                        if os.path.exists(temp_dir):
                            shutil.rmtree(temp_dir)
                    except Exception as e:
                        print(f"Warning: Could not clean up temporary directory {temp_dir}: {e}")
                
                # Write failed issues to file after processing is complete
                if failed_issues:
                    try:
                        field_names = ['key', 'error_type', 'error', 'retried']
                        with open(failed_issues_file, 'w', newline='') as file:
                            writer = csv.DictWriter(file, fieldnames=field_names)
                            writer.writeheader()
                            for issue in failed_issues:
                                writer.writerow(issue)
                    except Exception as e:
                        print(f"Error writing failed issues to file: {e}")
        
        print(f"Migration completed for {processed} of {total} issues.")
        
        # Report on failed issues
        if failed_issues:
            print(f"Failed to migrate {len(failed_issues)} issues. Check {failed_issues_file} for details.")
        
        return {'total': total, 'processed': processed, 'failed': len(failed_issues)}

    def send_to_api(self, extracted_identifier, project, folder_path):
        """
        Send files to custom API endpoint.

        Args:
            extracted_identifier (str): The extracted identifier to use for the API call
            project (str): The project name/key
            folder_path (str): Path to the folder containing files to send

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if not os.path.exists(folder_path):
                print(f"Error: Folder {folder_path} does not exist")
                return False

            # Find PDF files in the folder
            pdf_files = [f for f in os.listdir(folder_path) if f.endswith('.pdf')]
            if not pdf_files and folder_path.endswith('.txt'):
                # If no PDF files but there's a text file, use that
                txt_files = [f for f in os.listdir(folder_path) if f.endswith('.txt')]
                if not txt_files:
                    print(f"Error: No PDF or text files found in {folder_path}")
                    return False

            return self.send_to_custom_api(extracted_identifier, project, folder_path)

        except Exception as e:
            print(f"Error sending to API: {e}")
            return False

    def send_to_custom_api(self, extracted_identifier, project, folder_path):
        """
        Send files to a custom API endpoint.

        Args:
            extracted_identifier (str): The extracted identifier to use for the API call
            project (str): The project name/key
            folder_path (str): Path to the folder containing files
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self.custom_api_url:
            print("Error: Custom API URL is required for custom API export")
            return False
            
        print(f"Sending files to custom API: {self.custom_api_url}")
        
        # Get all files in the folder
        files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
        
        # Prepare the files to send
        multi_files = []
        for file_name in files:
            file_path = os.path.join(folder_path, file_name)
            # Determine content type based on file extension
            content_type = "application/pdf" if file_name.endswith('.pdf') else \
                          "application/json" if file_name.endswith('.json') else \
                          "text/plain" if file_name.endswith('.txt') else \
                          "application/octet-stream"
            
            with open(file_path, 'rb') as f:
                multi_files.append(('files', (file_name, f.read(), content_type)))
        
        # Set up headers
        try:
            headers = self.custom_api_headers if isinstance(self.custom_api_headers, dict) else json.loads(self.custom_api_headers)
        except:
            headers = {"Accept": "*/*"}
        
        # Set up parameters
        try:
            params = self.custom_api_params if isinstance(self.custom_api_params, dict) else json.loads(self.custom_api_params)
        except:
            params = {}
            
        # Add extracted identifier and project to parameters if not present
        if params is None:
            params = {}
        if 'extracted_identifier' not in params:
            params['extracted_identifier'] = extracted_identifier
        if 'project' not in params:
            params['project'] = project
            
        # Send the request
        print(f"Sending request to: {self.custom_api_url}")
        print(f"Headers: {headers}")
        print(f"Parameters: {params}")
        
        response = requests.post(
            self.custom_api_url,
            files=multi_files,
            headers=headers,
            params=params
        )
        
        if response.status_code in [200, 201, 202]:
            print(f"Successfully sent files to custom API")
            return True
        else:
            print(f"Error sending files to custom API: {response.status_code} - {response.text}")
            return False

    @retry(
        retry=retry_if_exception_type((ClientRequestException, requests.exceptions.RequestException, AttributeError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=3, max=30),
        reraise=True
    )
    def _cleanup_issue_temp(self, local_vars):
        """Clean up temp directories for an issue to free disk space."""
        for name in ('issue_temp_dir', 'sp_temp_dir'):
            d = local_vars.get(name)
            if d and os.path.exists(d):
                try:
                    shutil.rmtree(d)
                except Exception:
                    pass

    def migrate_jira_issue(self, jirakey, batch_temp_dirs=None):
        """
        Use Atlassian Jira API to get a summary of an Issue
        Uses retry with self.max_retries for error handling
        
        Args:
            jirakey: The Jira issue key
            batch_temp_dirs: Optional list to track temporary directories for batch cleanup
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Get the ticket by ID with fields based on the approach we're using
            fields_param = "worklog,attachment,comment"  # Always include these
            
            # Get the issue with all needed fields
            # Define a more specific expand parameter to avoid issues with '*all'
            # 'renderedFields' gets HTML versions of rich text fields, 'changelog' for history, 'worklog' for time tracking.
            specific_expand = 'changelog,renderedFields,worklog'

            issue_data = None # Initialize issue_data
            if 'vault' in self.vault_url_entry:  
                # Fetch *all* fields, including custom ones, by default for Vault
                issue_data = self.jira.issue(jirakey, expand=specific_expand, fields='*all') 
            else:
                # For non-Vault, still try to get all fields with specific expansions.
                issue_data = self.jira.issue(jirakey, expand=specific_expand, fields='*all')
            
            if not issue_data:
                 raise ValueError(f"Failed to fetch issue data for {jirakey}")

            fields = issue_data.get('fields', {})
            
            # Get the basic issue info that needs special handling
            key = str(issue_data.get('key', f'UNKNOWN_JIRA_KEY_FOR_{jirakey}'))

            changelog = issue_data.get('changelog', {}).get('histories', [])
            comments_response = self.jira.issue_get_comments(key)
            comments = comments_response.get('comments', []) if comments_response else []
            
            # Always get attachments for both SharePoint and API exports
            attachments_data = self.jira.get_attachments_ids_from_issue(key)
            attachments = attachments_data if isinstance(attachments_data, list) else []

            # Safely get worklogs
            worklog_field_data = fields.get('worklog')
            worklogs = worklog_field_data.get('worklogs', []) if worklog_field_data else []
            link = f"{self.vault_url_entry}/browse/{key}"
            
            # If project_key is not set, get it from the issue
            project_key = self.project_key
            # Safely access project key from fields
            project_details = fields.get('project', {})
            if not project_key and project_details.get('key'):
                project_key = project_details['key'] # Safe due to .get('key') check
                # Refresh project-specific custom fields if needed
                if project_key not in self.project_custom_fields:
                    self.project_custom_fields[project_key] = self._get_project_custom_fields(project_key)
            
            # Process all attachments for download
            downloads = []
            if attachments:  # Only process if we have attachments
                for attachment_info in attachments:
                    if isinstance(attachment_info, dict):
                        attachment_id = attachment_info.get('attachment_id')
                        if attachment_id:
                            attachment_content_details = self.jira.get_attachment(attachment_id)
                            if attachment_content_details:
                                downloads.append(attachment_content_details)
            
            # Helper function to extract field values properly, using displayName where available
            def extract_field_value(field_key, field_value):
                if field_value is None:
                    return "None"
                
                # Skip these fields as they're handled separately
                if field_key in ['worklog', 'attachment', 'comment']:
                    return None
                    
                # Handle objects with displayName
                if isinstance(field_value, dict):
                    if 'displayName' in field_value:
                        return field_value.get('displayName')
                    elif 'name' in field_value:
                        return field_value.get('name')
                    elif 'value' in field_value:
                        return field_value['value']
                    else:
                        # Return a simplified string representation for complex objects
                        return str(field_value)
                # Handle arrays/lists
                elif isinstance(field_value, list):
                    if not field_value:
                        return "None"
                    result = []
                    for item in field_value:
                        if isinstance(item, dict):
                            if 'displayName' in item:
                                result.append(item.get('displayName'))
                            elif 'name' in item:
                                result.append(item.get('name'))
                            elif 'value' in item:
                                result.append(item.get('value'))
                            else:
                                result.append(str(item))
                        else:
                            result.append(str(item))
                    return ", ".join(result)
                else:
                    return str(field_value)
            
            # Extract all fields with values
            field_dict = {}
            for field_key, field_value in fields.items():
                # Skip fields that are just metadata about other fields
                if field_key.endswith('_navigable'):
                    continue
                    
                value = extract_field_value(field_key, field_value)
                # Include the field if it has a value (is not None and not the string "None")
                if value is not None and value != "None":
                    field_dict[field_key] = value # Directly add the field if it has a value
            
            # Generate HTML table rows for fields
            field_rows = ""
            for field_key, field_value in field_dict.items():
                # Get the display name for the field
                if project_key and field_key in self.project_custom_fields.get(project_key, {}):
                    field_display_name = self.project_custom_fields[project_key][field_key]
                elif field_key in self.field_mapping:
                    field_display_name = self.field_mapping[field_key]
                elif field_key.startswith('customfield_'):
                    field_display_name = f"Custom Field {field_key.replace('customfield_', '')}"
                else:
                    field_display_name = field_key
                    
                field_rows += f'''
    <tr>
    <th>{field_display_name}</th>
    <td>{field_value}</td>
    </tr>'''

            # Create the ticket summary in HTML table format with border and outline
            ticket_summary = f'''
            <!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Report</title>
        <style>
        body {{
        font-family: Arial, sans-serif;
        }}
        table {{
        border-collapse: collapse;
        width: 100%;
        margin-bottom: 20px;
        }}
        th, td {{
        border: 1px solid #dee2e6;
        text-align: left;
        padding: 8px;
        }}
        th {{
        background-color: #f8f9fa;
        font-weight: bold;
        }}
        h2 {{
        margin-bottom: 5px;
        }}
        </style>
        </head>
        <body>
        <table>
        <tr>
        <th>Key</th>
        <td>{key}</td>
        </tr>
        <tr>
        <th>Link</th>
        <td><a href="{link}">{link}</a></td>
        </tr>
        {field_rows}
        </table>

        <h2>Attachments</h2>
        <table>
        <tr>
        <th>Filename</th>
        </tr>
        {"".join([f"<tr><td>{attachment_file.get('filename', 'N/A')}</td></tr>" for attachment_file in attachments]) if attachments else "<tr><td> No Attachments Available</td></tr>"}
        </table>

        <h2>Worklog</h2>
        <table>
        <tr>
        <th>Author</th>
        <th>Comment</th>
        <th>Date</th>
        <th>Timespent</th>
        </tr>
        {"".join([
            f"<tr><td>{log.get('author', {}).get('displayName', 'N/A')}</td>"
            f"<td>{log.get('comment', '')}</td>"
            f"<td>{log.get('created', 'N/A')}</td>"
            f"<td>{log.get('timeSpent', 'N/A')}</td></tr>" 
            for log in worklogs
        ]) if worklogs else "<tr><td colspan='4'>No Worklog Available</td></tr>"}
        </table>

        <h2>Comments</h2>
        <table>
        <tr>
        <th>Author</th>
        <th>Comment</th>
        <th>Created</th>
        <th>Updated</th>
        </tr>
        {"".join([
            f"<tr><td>{comment.get('author', {}).get('displayName', 'N/A')} </td>"
            f"<td>{comment.get('body', '')} </td>"
            f"<td>{comment.get('created', 'N/A')}</td>"
            f"<td>{comment.get('updated', 'N/A')}</td></tr>" 
            for comment in comments
        ]) if comments else "<tr><td colspan='4'>No comments available</td></tr>"}
        </table>

        <h2>Changelog</h2>
        <table>
        <tr>
        <th>Author</th>
        <th>Created</th>
        <th>Change Items</th>
        </tr>
        {"".join(["<tr><td>" + str(log.get('author', {}).get('displayName', 'N/A')) + "</td><td>" + str(log.get('created', 'N/A')) + "</td><td>" + ",".join([
        'Field: ' + str(item.get('field', 'N/A')) + 
        ', FieldType: ' + str(item.get('fieldtype', 'N/A')) + 
        ', From: ' + str(item.get('from', 'N/A')) + 
        ', FromString: ' + str(item.get('fromString', 'N/A')) + 
        ', To: ' + str(item.get('to', 'N/A')) + 
        ', ToString: ' + str(item.get('toString', 'N/A')) + chr(10)
        for item in log.get('items', [])
        ]) + "</td></tr>" 
        for log in changelog]) if changelog else "<tr><td colspan='3'>No changelog available</td></tr>"}
        </table>
            '''
            options = {
                'encoding': "UTF-8"
            }

            credentials = (self.vault_name_entry, self.vault_token_entry)
            # Extract identifier from fields matching identifier_field_terms
            extracted_identifier = None
            issue_data['EXTRACTED_IDENTIFIER'] = None

            # Build text to search from fields matching identifier_field_terms
            identifier_field_terms = getattr(self.args, 'identifier_field_terms', ["identifier", "id", "reference", "account"])
            possible_texts = []

            # Scan all fields matching identifier_field_terms against the field mapping
            for field_id, field_name in self.field_mapping.items():
                if field_name:
                    lower_name = field_name.lower()
                    for term in identifier_field_terms:
                        if term and term.lower() in lower_name:
                            val = fields.get(field_id, '')
                            if val:
                                possible_texts.append(str(val))
                            break

            # Always include summary and description
            summary_value = fields.get('summary', '')
            description_value = fields.get('description', '')
            if summary_value:
                possible_texts.append(str(summary_value))
            if description_value:
                possible_texts.append(str(description_value))

            possiblecandidates = "\n".join(possible_texts)

            # Extract identifiers
            identifier_matches = matcher.extract_identifier(possiblecandidates)
            discovered_identifiers = identifier_matches if identifier_matches else []

            if discovered_identifiers:
                extracted_identifier = discovered_identifiers[0]
                issue_data['EXTRACTED_IDENTIFIER'] = extracted_identifier
                if len(discovered_identifiers) > 1:
                    print(f"Multiple identifiers ({len(discovered_identifiers)}) found for {key}: {discovered_identifiers}. First identifier '{extracted_identifier}' will be used for naming and API calls.")
                else:
                    print(f"Single identifier found for {key}: {extracted_identifier}")
            else:
                print(f"No identifier found for {key}. Issue-based folder structure will be used for naming.")
            
            # Safely access project key and issue type for filekey
            project_key_for_file = fields.get('project', {}).get('key', 'unknown_project')
            # Get the issue type and normalize it by removing parentheses, spaces, and forward slashes
            issue_type = fields.get('issuetype', {}).get('name', 'unknown_type')
            # Normalize issue type by removing parentheses, spaces, and forward slashes
            normalized_issue_type = issue_type.replace('(', '').replace(')', '').replace(' ', '').replace('/', '')
            
            # For issues with extracted identifier, use standard folder structure
            if extracted_identifier:
                filekey = f"{extracted_identifier}_{project_key_for_file}_{normalized_issue_type}"
            else:
                # For issues without extracted identifier, use just the issue key and project key
                filekey = f"{key}_{project_key_for_file}"
            
            # Create a temporary directory for this issue using the system temp directory
            if extracted_identifier:
                # Add Jira key to the temp directory name to ensure uniqueness
                issue_temp_dir = os.path.join(self.base_temp_dir, f"{filekey}_{key}")
            else:   
                issue_temp_dir = os.path.join(self.base_temp_dir, filekey)
            os.makedirs(issue_temp_dir, exist_ok=True)
            attachment_counter = 0
            for download in downloads: # 'downloads' now contains results from self.jira.get_attachment
                attachment_counter += 1
                # Shorten attachment filenames if they're too long and add Jira key to ensure uniqueness
                original_filename = download.get("filename", f"unnamed_attachment_{key}_{attachment_counter}")
                # Add Jira key as prefix to make the filename unique
                file_base, file_ext = os.path.splitext(original_filename)
                # Normalize the filename to remove spaces and special characters
                normalized_base = normalize_filename(file_base)
                # Also normalize the extension, but preserve the dot
                normalized_ext = file_ext.lower()
                if normalized_ext:
                    normalized_ext = '.' + normalize_filename(normalized_ext[1:])
                unique_filename = f"{key}_{normalized_base}{normalized_ext}"
                shortened_filename = f"{attachment_counter}_{shorten_filename(unique_filename)}"
                
                content_url = download.get("content")
                if content_url:
                    try:
                        # Use the session object for requests
                        response = session.get(content_url, auth=credentials)
                        response.raise_for_status() # Raise an exception for bad status codes
                        with open(os.path.join(issue_temp_dir, shortened_filename), 'wb') as ingestfile:
                            ingestfile.write(response.content)
                    except requests.exceptions.RequestException as e_req:
                        print(f"Warning: Could not download attachment content from {content_url} for {key}: {e_req}")
                else:
                    print(f"Warning: No content URL found for attachment in {key}, filename {original_filename}")

            # Generate PDF with cross-platform support
            pdf_generated = False
            try:
                # If we have a Windows-specific configuration, use it
                if extracted_identifier:
                    # Add Jira key to PDF filename to ensure uniqueness (uses filekey which includes first token)
                    pdf_filename = shorten_filename(f"{key}_{filekey}.pdf")
                    pdf_path = os.path.join(issue_temp_dir, pdf_filename)
                else:
                    pdf_filename = shorten_filename(f"{key}.pdf")
                    pdf_path = os.path.join(issue_temp_dir, pdf_filename)
                
                pdfkit.from_string(str(ticket_summary), pdf_path, options=options, configuration=self.pdfkit_config)
                pdf_generated = True
            except Exception as e:
                print(f"Error generating PDF for {key}: {e}")
                # Create a simple text file with issue information as a fallback
                if extracted_identifier:
                    # Add Jira key to text filename to ensure uniqueness
                    text_filename = shorten_filename(f"{key}_{filekey}.txt")
                    text_path = os.path.join(issue_temp_dir, text_filename)
                else:
                    text_filename = shorten_filename(f"{key}.txt")
                    text_path = os.path.join(issue_temp_dir, text_filename)
                with open(text_path, 'w', encoding='utf-8') as f:
                    f.write(f"Issue: {key}\nLink: {link}\n\nError generating PDF: {e}")

            # Handle export method
            if self.export_method == 'sharepoint' and self.sharepoint_folder_entry:
                # Normalize the SharePoint folder entry using the specialized SharePoint path normalizer
                # This prevents double encoding when used with the SharePoint API
                normalized_sharepoint_folder = normalize_sharepoint_path(self.sharepoint_folder_entry)
                
                # Create a copy of the directory for SharePoint upload to prevent race conditions during retries
                sp_temp_dir = os.path.join(self.base_temp_dir, f"sp_upload_{key}")
                os.makedirs(sp_temp_dir, exist_ok=True)
                
                # Add temp directories to batch tracking list if provided
                if batch_temp_dirs is not None:
                    if issue_temp_dir not in batch_temp_dirs:
                        batch_temp_dirs.append(issue_temp_dir)
                    if sp_temp_dir not in batch_temp_dirs:
                        batch_temp_dirs.append(sp_temp_dir)
                
                # Copy all files from the issue directory to the SharePoint upload directory
                for file_name in os.listdir(issue_temp_dir):
                    src_path = os.path.join(issue_temp_dir, file_name)
                    dst_path = os.path.join(sp_temp_dir, file_name)
                    if os.path.isfile(src_path):
                        shutil.copy2(src_path, dst_path)
                
                # Create consistent folder structure: issuekey_identifier(if exist)_projectkey
                if extracted_identifier:
                    # Include extracted identifier in the folder name
                    folder_key = shorten_filename(f"{key}_{extracted_identifier}_{project_key_for_file}", max_length=100)
                else:
                    # Just use issue key and project key
                    folder_key = shorten_filename(f"{key}_{project_key_for_file}", max_length=100)
                
                folder_path = f"{normalized_sharepoint_folder}/{folder_key}"
                
                # Check if SharePoint uploader is available
                if self.uploadsharepoint is None:
                    print(f"SharePoint uploader not initialized - cannot upload issue {key}")
                    return {"success": False, "extracted_identifier": extracted_identifier, "error": "SharePoint uploader not initialized. Check SharePoint site configuration."}
                
                status = self.uploadsharepoint.upload_to_sharepoint(sp_temp_dir, folder_path)
                
                # We no longer clean up here - directories are cleaned up after the entire batch is done
                
                # Check the status returned from SharePoint upload and return accordingly
                if not status:
                    print(f"SharePoint upload failed for issue {key}")
                    return {"success": False, "extracted_identifier": extracted_identifier, "error": "SharePoint upload failed"}

            elif self.export_method == 'api':
                project_name_for_api = fields.get('project', {}).get('key') if fields.get('project') else self.project_key

                # For custom API, use the extracted identifier if available
                print(f"Processing issue {key} for CUSTOM API with identifier: {extracted_identifier}")
                api_send_successful = self.send_to_custom_api(extracted_identifier, project_name_for_api, issue_temp_dir)

                if batch_temp_dirs is not None and issue_temp_dir not in batch_temp_dirs:
                    batch_temp_dirs.append(issue_temp_dir)

                if not api_send_successful:
                    print(f"CUSTOM API upload FAILED for issue {key} with identifier '{extracted_identifier}'")
                    return {"success": False, "extracted_identifier": extracted_identifier, "error": "API send failed", "pdf_generated": pdf_generated, "attachments_count": len(attachments), "files_uploaded": attachment_counter}
                else:
                    print(f"CUSTOM API upload SUCCEEDED for issue {key} with identifier '{extracted_identifier}'") 
            else:
                # This 'else' corresponds to self.export_method not being 'sharepoint' or 'api'.
                # Argument parsing should ensure export_method is one of the valid choices.
                # If it reaches here with an unexpected value, it's an unhandled configuration.
                # The original code did not have an explicit error here, assuming valid inputs post-argparse.
                # For robustness, if it's not 'sharepoint' and not 'api', and didn't raise an error for API no-token,
                # it implies an unhandled export method.
                if self.export_method not in ['sharepoint', 'api']:
                     print(f"Warning: Unrecognized export_method '{self.export_method}' for issue {key}. No export action taken.")
                     # Depending on desired strictness, this could be `return False` or raise an error.
                     # For now, just a warning, as the issue might still be considered "processed" if no export was intended.

            # Clean up temp directories immediately after processing to prevent /tmp filling up
            for d in [issue_temp_dir, sp_temp_dir if 'sp_temp_dir' in locals() else None]:
                if d and os.path.exists(d):
                    try:
                        shutil.rmtree(d)
                    except Exception:
                        pass

            return {"success": True, "extracted_identifier": extracted_identifier, "error": None, "pdf_generated": pdf_generated, "attachments_count": len(attachments), "files_uploaded": attachment_counter}

        except AttributeError as e:
            import traceback
            print(f"AttributeError in migrate_jira_issue for {jirakey}: {str(e)}")
            self._cleanup_issue_temp(locals())
            time.sleep(2)
            raise
        except Exception as e:
            import traceback
            print(f"!!! Initial unhandled error in migrate_jira_issue for {jirakey} (before retry) !!!")
            print(f"Error Type: {type(e).__name__}")
            print(f"Error Message: '{str(e)}'")
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
                print(f"  HTTP Status Code: {e.response.status_code}")
                print(f"  HTTP Response Text: {e.response.text[:500]}")
                print(f"  HTTP Response Reason: {e.response.reason}")
            print(f"Full Traceback of initial error:\n{traceback.format_exc()}")
            self._cleanup_issue_temp(locals())
            time.sleep(2)
            raise


def main():
    parser = argparse.ArgumentParser(description="Jira Migration Tool")
    parser.add_argument("--vault-name", required=True, help="Vault name")
    parser.add_argument("--vault-token", required=True, help="Vault token")
    parser.add_argument("--vault-url", required=True, help="Vault URL")
    parser.add_argument("--export-method", choices=['sharepoint', 'api'], default='sharepoint', help="Export method")
    parser.add_argument("--sharepoint-site", help="SharePoint site (required if export-method is sharepoint)")
    parser.add_argument("--sharepoint-folder", help="SharePoint folder (required if export-method is sharepoint)")
    parser.add_argument("--api-type", choices=['custom'], default='custom', help="API type (required if export-method is api)")
    parser.add_argument("--custom-api-url", help="Custom API URL (required if export-method is api)")
    parser.add_argument("--custom-api-headers", help="JSON string with custom headers for the API")
    parser.add_argument("--custom-api-params", help="JSON string with custom parameters for the API")
    parser.add_argument("--csv-file", help="Path to CSV file with issue keys")
    parser.add_argument("--jql", help="JQL query to fetch issues")
    parser.add_argument("--project-key", help="Project key to fetch issues and custom fields for")
    parser.add_argument("--parallelism", type=int, default=5, help="Number of parallel tasks")
    parser.add_argument("--wkhtmltopdf-path", help="Path to wkhtmltopdf executable (required for Windows)")
    parser.add_argument("--retry-failed", action="store_true", help="Retry previously failed issues from failed_issues.csv")
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum number of retries for transient errors")
    
    args = parser.parse_args()

    # Input validation logic
    if not args.csv_file and not args.jql and not args.project_key and not args.retry_failed:
        parser.error("One of --csv-file, --jql, --project-key, or --retry-failed must be provided")
        
    # Export method-specific validations
    if args.export_method == 'sharepoint':
        if not args.sharepoint_site or not args.sharepoint_folder:
            parser.error("--sharepoint-site and --sharepoint-folder are required when export-method is sharepoint")
    elif args.export_method == 'api':
        if not args.custom_api_url:
            parser.error("--custom-api-url is required when export-method is api")
            
    # Process JSON strings for custom API
    if args.custom_api_headers:
        try:
            import json
            args.custom_api_headers = json.loads(args.custom_api_headers)
        except:
            parser.error("--custom-api-headers must be a valid JSON string")
            
    if args.custom_api_params:
        try:
            import json
            args.custom_api_params = json.loads(args.custom_api_params)
        except:
            parser.error("--custom-api-params must be a valid JSON string")
            
    # Generate JQL from project key if jql not explicitly provided
    if args.project_key and not args.jql:
        args.jql = f"project = {args.project_key}"
        print(f"Generated JQL query: {args.jql}")

    # If we're only retrying failed issues and there's no input source specified, create an empty CSV file
    if args.retry_failed and not args.csv_file and not args.jql and not args.project_key:
        # Create an empty list to force using only the failed issues file in run_migration
        all_issues = []
        migration = JiraMigration(args)
        migration.start_migration()
    else:
        # Normal execution with specified input source
        migration = JiraMigration(args)
        migration.start_migration()

if __name__ == "__main__":
    main()
