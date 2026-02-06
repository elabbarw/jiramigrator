import os
import json
import requests
import glob
import shutil
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import csv
from atlassian import Jira
import logging
import re
from modules import PatternMatcher

# Configure requests to use a larger connection pool for API exporter
session_api = requests.Session()
adapter_api = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
session_api.mount('http://', adapter_api)
session_api.mount('https://', adapter_api)

# Load patterns for PatternMatcher
try:
    with open(os.path.join(os.path.dirname(__file__), "patterns.json")) as json_file:
        patterns_data = json.load(json_file)
        PATTERNS = patterns_data.get("patterns", [])
except Exception as e:
    logging.warning(f"Could not load patterns.json, using empty patterns list: {e}")
    PATTERNS = []

class ApiExporter:
    """
    Export Jira tickets to an API endpoint.
    This class provides functionality to export Jira data to APIs and is designed
    to be used either independently or integrated with the JiraMigration system.

    Supports custom API endpoints for flexible integration.
    """

    def __init__(self, vault_url, vault_name, vault_token, api_type='custom', environment=None, 
                 custom_api_url=None, custom_api_headers=None, custom_api_params=None, analyze_fields=True,
                 show_all_fields=False, identifier_field_terms=None, important_field_terms=None,
                 include_manual_fields=False, manual_fields=None, use_view_screen=True):
        """Initialize the API exporter
        
        Args:
            vault_url (str): URL of the Jira instance
            vault_name (str): Jira username
            vault_token (str): Jira API token
            api_type (str): Type of API to use ('custom')
            environment (str): Environment (deprecated, not used)
            custom_api_url (str): URL to use for custom API
            custom_api_headers (dict): Headers to use for custom API
            custom_api_params (dict): Parameters to use for custom API
            analyze_fields (bool): Whether to analyze fields for identifier extraction
            show_all_fields (bool): Whether to show all fields in the PDF
            identifier_field_terms (list): List of terms to use when identifying identifier fields
            important_field_terms (list): List of terms to use when identifying important fields
            include_manual_fields (bool): Whether to include manually specified fields
            manual_fields (list): List of field display names to include
            use_view_screen (bool): Whether to use view screens for field visibility
        """
        self.jira = Jira(
            url=vault_url,
            username=vault_name,
            password=vault_token,
            session=session_api  # Use the session with increased pool size
        )
        
        self.vault_url = vault_url
        self.vault_name = vault_name
        self.vault_token = vault_token
        self.api_type = api_type
        self.environment = environment
        self.custom_api_url = custom_api_url
        self.custom_api_headers = custom_api_headers or {}
        self.custom_api_params = custom_api_params or {}
        
        # Analysis settings
        self.analyze_fields = analyze_fields
        self.show_all_fields = show_all_fields
        self.identifier_field_terms = identifier_field_terms or ["identifier", "id", "reference", "account"]
        self.important_field_terms = important_field_terms or [
            "type", "priority", "label", "epic", "parent", "link",
            "domain", "benefit", "customer", "capex", "opex",
            "team", "business", "value"
        ]
        
        # Manually specified fields
        self.include_manual_fields = include_manual_fields
        self.manual_fields = manual_fields or []
        
        # Whether to use view screens for field visibility
        self.use_view_screen = use_view_screen
        
        # Field mapping cache
        self.field_mapping = self.get_all_field_mapping()
        
        # Project custom fields cache
        self.project_custom_fields = {}
        
        # Cache for visible fields per issue type in a project
        self.visible_fields_cache = {}
        
        # Initialize PatternMatcher for identifier extraction
        self.matcher = PatternMatcher(PATTERNS)

    def get_all_field_mapping(self):
        """
        Retrieves all field definitions from Jira and creates a mapping
        from field IDs to their display names.
        
        Returns:
            dict: A dictionary mapping field IDs to their display names
        """
        try:
            field_mapping = {}
            all_fields = self.jira.get_all_fields()
            
            for field in all_fields:
                field_id = field.get('id')
                field_name = field.get('name')
                
                if field_id and field_name:
                    field_mapping[field_id] = field_name
                    
            logging.info(f"Retrieved {len(field_mapping)} fields from Jira")
            return field_mapping
        except Exception as e:
            logging.error(f"Error retrieving field mapping: {e}")
            return {}

    def get_project_custom_fields(self, project_key):
        """
        Retrieves custom fields associated with a specific project.
        
        Args:
            project_key (str): The project key to get custom fields for
            
        Returns:
            dict: A dictionary of custom fields for the project
        """
        try:
            # Skip detailed analysis if not explicitly enabled
            if hasattr(self, 'analyze_fields') and not self.analyze_fields:
                logging.info(f"Skipping detailed custom field analysis for project {project_key} (analyze_fields is disabled)")
                return {}
                
            logging.info(f"Retrieving custom fields for project {project_key}")
            
            # Get all custom fields directly from Jira API
            all_custom_fields = self.jira.get_custom_fields() 
            
            # Create mapping of custom field IDs to display names
            custom_fields = {}
            for field in all_custom_fields:
                if 'id' in field and 'name' in field:
                    field_id = field['id']
                    field_name = field['name']
                    if field_id.startswith('customfield_'):
                        custom_fields[field_id] = field_name
            
            logging.info(f"Found {len(custom_fields)} custom fields for project {project_key}")
            return custom_fields
        except Exception as e:
            logging.error(f"Error retrieving project custom fields: {e}")
            return {}

    def get_timestamp(self):
        """Get the current timestamp formatted as a string"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def get_jira_issues(self, jql):
        """
        Get a list of Jira issue keys based on JQL query.
        
        Args:
            jql (str): JQL query to find issues
            
        Returns:
            list: List of issue keys
        """
        issues = []
        count = 0
        while True:
            tmp_issues = self.jira.jql_get_list_of_tickets(jql, fields="key", start=count, limit=count + 999)
            if len(tmp_issues) == 0:
                break
            issues.extend(tmp_issues)
            count += 999
        
        return issues

    def send_to_api(self, extracted_identifier, project, folder_path):
        """
        Send files from a folder to API endpoint based on the configured API type.

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
                
            # Find PDF and JSON files in the folder
            pdf_files = [f for f in os.listdir(folder_path) if f.endswith('.pdf')]
            json_files = [f for f in os.listdir(folder_path) if f.endswith('.json')]
            
            if not pdf_files:
                print(f"Error: No PDF files found in {folder_path}")
                return False
            
            # Use the first PDF file found
            pdf_file = os.path.join(folder_path, pdf_files[0])
            
            # If there's a JSON file, use it, otherwise we'll proceed without metadata
            json_file = os.path.join(folder_path, json_files[0]) if json_files else None
            
            return self.send_to_custom_api(pdf_file, json_file, extracted_identifier, project)
        except Exception as e:
            print(f"Error sending to API: {e}")
            return False

    def send_to_custom_api(self, pdf_file, json_file, extracted_identifier, project):
        """
        Send files to a custom API endpoint.

        Args:
            pdf_file (str): Path to the PDF file
            json_file (str): Path to the JSON file or None if no JSON file exists
            extracted_identifier (str): The extracted identifier to use for the API call
            project (str): The project name/key
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self.custom_api_url:
            print("Error: Custom API URL is required for custom API export")
            return False
            
        print(f"Sending files to custom API: {self.custom_api_url}")
        
        import requests
        import json as json_lib
        
        # Prepare the files to send
        multi_files = []
        
        # Add PDF file
        with open(pdf_file, 'rb') as pdf:
            multi_files.append(('files', (os.path.basename(pdf_file), pdf, 'application/pdf')))
            
            # Add JSON file if it exists
            if json_file and os.path.exists(json_file):
                with open(json_file, 'rb') as json:
                    multi_files.append(('files', (os.path.basename(json_file), json, 'application/json')))
            
            # Set up headers
            headers = self.custom_api_headers
            if isinstance(headers, str):
                try:
                    headers = json_lib.loads(headers)
                except:
                    headers = {"Accept": "*/*"}
            
            # Set up parameters
            params = self.custom_api_params
            if isinstance(params, str):
                try:
                    params = json_lib.loads(params)
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

    def extract_identifier(self, issue, project_custom_fields=None):
        """
        Extract identifier from the issue using PatternMatcher.
        Uses NLP and regex patterns defined in patterns.json.

        Args:
            issue: The Jira issue object with fields
            project_custom_fields: Dictionary of project-specific custom fields

        Returns:
            tuple: (identifier, found_field_name) or (None, None) if not found
        """
        if issue is None:
            print("Error: Issue object is None")
            return None, None
            
        # Safely get fields
        try:
            if hasattr(issue, 'fields'):
                fields = issue.fields
            elif isinstance(issue, dict) and 'fields' in issue:
                fields = issue['fields']
            else:
                print(f"Warning: Issue has no fields attribute or key")
                return None, None
                
            # Further verify fields is not None
            if fields is None:
                print(f"Warning: Issue fields is None")
                return None, None
        except Exception as e:
            print(f"Error accessing issue fields: {e}")
            return None, None

        # Extract potential identifier-containing text from multiple fields
        try:
            # Gather field values that might contain identifiers
            possible_id_texts = []

            # Check custom fields with identifier-related names first
            field_mapping = self.field_mapping or {}
            for field_id, field_name in field_mapping.items():
                if not field_id or not field_name:
                    continue
                    
                lower_name = field_name.lower()
                is_id_field = False

                for term in self.identifier_field_terms:
                    if term and term.lower() in lower_name:
                        is_id_field = True
                        break

                if is_id_field:
                    # Get the field value
                    try:
                        field_value = None
                        
                        # Try direct attribute access
                        if hasattr(fields, field_id):
                            field_value = getattr(fields, field_id)
                        # Try dictionary access
                        elif hasattr(fields, '__getitem__'):
                            try:
                                field_value = fields[field_id]
                            except (KeyError, TypeError):
                                try:
                                    field_value = fields.get(field_name)
                                except (AttributeError, KeyError, TypeError):
                                    pass
                        
                        if field_value is not None:
                            # Extract text value based on field type
                            if hasattr(field_value, 'value'):
                                possible_id_texts.append(str(field_value.value))
                            elif hasattr(field_value, 'displayName'):
                                possible_id_texts.append(str(field_value.displayName))
                            elif isinstance(field_value, dict) and 'value' in field_value:
                                possible_id_texts.append(str(field_value['value']))
                            else:
                                possible_id_texts.append(str(field_value))
                    except Exception as field_err:
                        print(f"Warning: Error processing field {field_id} ({field_name}): {field_err}")
            
            # Always check description and summary
            try:
                # Get description
                description = None
                if hasattr(fields, 'description'):
                    description = fields.description
                elif hasattr(fields, '__getitem__') and 'description' in fields:
                    description = fields['description']
                
                if description:
                    possible_id_texts.append(str(description))
            except Exception as desc_err:
                print(f"Warning: Error accessing description: {desc_err}")
            
            try:
                # Get summary
                summary = None
                if hasattr(fields, 'summary'):
                    summary = fields.summary
                elif hasattr(fields, '__getitem__') and 'summary' in fields:
                    summary = fields['summary']
                
                if summary:
                    possible_id_texts.append(str(summary))
            except Exception as summary_err:
                print(f"Warning: Error accessing summary: {summary_err}")
            
            # Combine all the possible identifier-containing texts
            if not possible_id_texts:
                print("No potential identifier fields found in issue")
                return None, None
                
            combined_text = "\n".join(possible_id_texts)
            
            # Use PatternMatcher to extract identifiers
            id_matches = self.matcher.extract_identifier(combined_text)

            if id_matches and len(id_matches) > 0:
                extracted_id = id_matches[0]
                found_field_name = "ML Pattern Match"
                print(f"Found identifier using ML/pattern matching: {extracted_id}")
                return extracted_id, found_field_name
            
            # If no identifiers found with PatternMatcher, try basic regex as backup
            # Build patterns from identifier_field_terms
            for text in possible_id_texts:
                if not text:
                    continue

                for term in self.identifier_field_terms:
                    if not term:
                        continue
                    escaped_term = re.escape(term)
                    pattern = rf'{escaped_term}\s*[=:]\s*(\S+)'
                    try:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            found_id = match.group(1)
                            found_field_name = "Regex Pattern Match"
                            print(f"Found identifier using regex: {found_id}")
                            return found_id, found_field_name
                    except Exception as pattern_err:
                        print(f"Warning: Error in regex pattern {pattern}: {pattern_err}")
        
        except Exception as extract_err:
            print(f"Error extracting identifier: {extract_err}")

        print("No identifier found in issue")
        return None, None

    def migrate_jira_issue(self, jirakey, patterns=None):
        """
        Migrate a Jira issue to the API. This leverages the same extraction logic as the SharePoint method.
        
        Args:
            jirakey (str): The Jira issue key
            patterns (list, optional): List of regex patterns to use for identifier extraction
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            print(f"Processing issue {jirakey}...")
            
            # Get project key from issue key
            project_key = jirakey.split('-')[0] if '-' in jirakey else None
            if not project_key:
                print(f"Error: Could not determine project key from issue key {jirakey}")
                return False
            
            # Get the issue with all necessary fields
            issue = self.jira.issue(jirakey, expand='changelog', fields='*')
            
            # Check if custom fields should be analyzed for this project
            if self.analyze_fields and project_key and project_key not in self.project_custom_fields:
                try:
                    # Get custom fields for this project
                    self.project_custom_fields[project_key] = self.get_project_custom_fields(project_key)
                except Exception as e:
                    print(f"Warning: Could not fetch custom fields for project {project_key}: {e}")
            
            # Extract identifier with proper error handling
            try:
                extracted_identifier, found_field_name = self.extract_identifier(issue, self.project_custom_fields.get(project_key, {}))

                if not extracted_identifier:
                    print(f"Error: Could not extract identifier from issue {jirakey}")
                    return False

                print(f"Found identifier in {found_field_name}: {extracted_identifier}")
            except Exception as id_err:
                print(f"Error extracting identifier for issue {jirakey}: {id_err}")
                return False
            
            # Create a temporary directory for this issue
            temp_dir = f'./{jirakey}'
            os.makedirs(temp_dir, exist_ok=True)
            
            # Process the ticket with the TicketPDF module
            try:
                # Try to import and use TicketPDF
                try:
                    from modules import TicketPDF
                    # Use TicketPDF to generate PDF for the issue
                    pdf_generator = TicketPDF(issue, self.vault_url, self.jira, jirakey, {})
                    pdf_path = pdf_generator  # TicketPDF returns the path to the generated PDF
                except ImportError:
                    # If TicketPDF is not available, create a simple text file instead
                    print("TicketPDF module not available. Creating a simple text file instead.")
                    pdf_path = os.path.join(temp_dir, f"{jirakey}.txt")
                    
                    # Extract basic issue information
                    summary = issue.fields.summary if hasattr(issue, 'fields') and hasattr(issue.fields, 'summary') else "No summary"
                    description = issue.fields.description if hasattr(issue, 'fields') and hasattr(issue.fields, 'description') else "No description"
                    
                    # Write to text file
                    with open(pdf_path, 'w') as f:
                        f.write(f"Issue: {jirakey}\n")
                        f.write(f"Summary: {summary}\n")
                        f.write(f"Description: {description}\n")
                        f.write(f"Extracted Identifier: {extracted_identifier}\n")
                        f.write(f"URL: {self.vault_url}/browse/{jirakey}\n")
            except Exception as pdf_err:
                print(f"Error generating document for issue {jirakey}: {pdf_err}")
                # Last resort fallback - create a minimal text file
                pdf_path = os.path.join(temp_dir, f"{jirakey}.txt")
                with open(pdf_path, 'w') as f:
                    f.write(f"Issue: {jirakey}\n")
                    f.write(f"Error: {pdf_err}\n")
                    f.write(f"Extracted Identifier: {extracted_identifier}\n")
            
            # Send files to the API endpoint
            try:
                result = self.send_to_api(extracted_identifier, project_key, temp_dir)
                
                # Clean up temporary directory
                try:
                    shutil.rmtree(temp_dir)
                except Exception as clean_err:
                    print(f"Warning: Could not clean up temporary directory {temp_dir}: {clean_err}")
                
                return result
            except Exception as api_err:
                print(f"Error sending files to API for issue {jirakey}: {api_err}")
                return False
                
        except Exception as e:
            print(f"Error migrating issue {jirakey}: {e}")
            return False

    def process_csv_file(self, csv_file, project=None, status='Success'):
        """
        Process issues from a CSV file.
        
        Args:
            csv_file (str): Path to the CSV file
            project (str, optional): Project to filter by
            status (str, optional): Status to filter by
            
        Returns:
            list: List of issue keys to process
        """
        records = []
        with open(csv_file, newline='', encoding='utf-8') as csvfile:
            csvreader = csv.DictReader(csvfile)
            for row in csvreader:
                records.append(row)
        
        issue_keys = []
        if project:
            for row in records:
                if row.get('Status') == status and row.get('Project') == project:
                    issue_keys.append(row.get('Key'))
        else:
            issue_keys = [row.get('Key') for row in records if 'Key' in row]
        
        return issue_keys

    def run_migration(self, issue_keys, parallelism=10, patterns=None):
        """
        Run the migration process for a list of issue keys.
        
        Args:
            issue_keys (list): List of Jira issue keys to process
            parallelism (int, optional): Number of parallel tasks
            patterns (list, optional): List of regex patterns for player identifier extraction
            
        Returns:
            list: List of processed issues with their status
        """
        results = []
        
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            with open('api_migration_status.csv', 'a', newline='', encoding='utf-8') as csvfile:
                csv_writer = csv.writer(csvfile)
                # Write headers if the file is new
                if csvfile.tell() == 0:
                    csv_writer.writerow(['Timestamp', 'Status', 'Issue', 'Reason'])
                
                # Submit the migrate_jira_issue function for each issue
                futures = {executor.submit(self.migrate_jira_issue, issue, patterns): issue for issue in issue_keys}
                
                # Process the completed futures as they finish
                for future in as_completed(futures):
                    issue = futures[future]
                    try:
                        result = future.result()
                        if result:
                            status_msg = f"Successfully migrated {issue}"
                            csv_writer.writerow([self.get_timestamp(), 'Success', issue, status_msg])
                            results.append({"key": issue, "status": "Success", "message": status_msg})
                        else:
                            status_msg = f"Failed to migrate {issue}"
                            csv_writer.writerow([self.get_timestamp(), 'Fail', issue, status_msg])
                            results.append({"key": issue, "status": "Fail", "message": status_msg})
                    except Exception as err:
                        status_msg = str(err)
                        csv_writer.writerow([self.get_timestamp(), 'Fail', issue, status_msg])
                        results.append({"key": issue, "status": "Fail", "message": status_msg})
        
        return results


def main():
    """Main function when running the script directly"""
    parser = argparse.ArgumentParser(description='Export Jira issues to API')
    
    # Common arguments
    parser.add_argument('-usr', '--vault_name', type=str, help='Jira Vault Username', required=True)
    parser.add_argument('-pwd', '--vault_token', type=str, help='Jira Vault Password', required=True)
    parser.add_argument('-url', '--vault_url', type=str, help='Jira Vault URL', required=True)
    parser.add_argument('-jql', '--vault_jql', type=str, help='Jira Vault JQL to get tickets', required=False)
    parser.add_argument('-csv', '--csv_file', type=str, help='CSV file with tickets', required=False)
    parser.add_argument('-cproject', '--csv_project', type=str, help='Project to extract from CSV', required=False)
    parser.add_argument('-p', '--parallelism', type=int, help='Number of parallel tasks', default=10)
    parser.add_argument('-tf', '--identifier_field_terms', type=str, help='Comma-separated list of terms to identify identifier fields',
                        default="identifier,id,reference,account")
    parser.add_argument('--analyze_fields', action='store_true', default=True, help='Analyze fields for identifier extraction')
    parser.add_argument('--show_all_fields', action='store_true', default=False, help='Show all fields in PDF output')
    
    # Check if patterns.json exists
    patterns_file = os.path.join(os.path.dirname(__file__), "patterns.json")
    if not os.path.exists(patterns_file):
        print(f"Warning: patterns.json file not found at {patterns_file}")
        print("Identifier detection may be less accurate without pattern definitions.")
        print("Please create a patterns.json file or copy it from the main project.")
    
    # Create subparsers for different API types
    subparsers = parser.add_subparsers(dest='api_type', help='API type to use')

    # Custom API arguments
    custom_parser = subparsers.add_parser('custom', help='Use custom API endpoint')
    custom_parser.add_argument('-api', '--custom_api_url', type=str, help='Custom API endpoint URL', required=True)
    custom_parser.add_argument('-hdr', '--custom_api_headers', type=str, 
                             help='JSON string with custom headers (default: {"accept": "*/*"})',
                             default='{"accept": "*/*"}')
    custom_parser.add_argument('-prm', '--custom_api_params', type=str,
                             help='JSON string with custom parameters (will be added to extracted_identifier and project)',
                             default='{}')
    
    args = parser.parse_args()
    
    # Ensure API type is selected
    if not args.api_type:
        parser.error("API type must be specified (custom)")
    
    # Process identifier field terms
    if isinstance(args.identifier_field_terms, str):
        identifier_field_terms = [term.strip().lower() for term in args.identifier_field_terms.split(',')]
    else:
        identifier_field_terms = args.identifier_field_terms

    # Create exporter with parsed args
    exporter_kwargs = {
        'vault_url': args.vault_url,
        'vault_name': args.vault_name,
        'vault_token': args.vault_token,
        'api_type': args.api_type,
        'identifier_field_terms': identifier_field_terms,
        'analyze_fields': args.analyze_fields,
        'show_all_fields': args.show_all_fields
    }
    
    if args.api_type == 'custom':
        if not args.custom_api_url:
            parser.error("Custom API URL is required for custom API export")
        exporter_kwargs['custom_api_url'] = args.custom_api_url
        exporter_kwargs['custom_api_headers'] = args.custom_api_headers
        exporter_kwargs['custom_api_params'] = args.custom_api_params
    
    exporter = ApiExporter(**exporter_kwargs)
    
    # Get issues to process
    if hasattr(args, 'vault_jql') and args.vault_jql:
        issues = [issue['key'] for issue in exporter.get_jira_issues(args.vault_jql)]
    elif hasattr(args, 'csv_file') and args.csv_file:
        issues = exporter.process_csv_file(args.csv_file, args.csv_project)
    else:
        parser.error("Either --vault_jql or --csv_file must be provided")
        return
    
    if not issues:
        print("No issues found to process")
        return
    
    print(f"Found {len(issues)} issues to process")
    print(f"Using API type: {args.api_type}")
    
    # Run migration
    results = exporter.run_migration(issues, args.parallelism)
    
    # Print summary
    success_count = sum(1 for r in results if r["status"] == "Success")
    fail_count = sum(1 for r in results if r["status"] == "Fail")
    
    print(f"\nMigration completed:")
    print(f"  Total: {len(results)}")
    print(f"  Success: {success_count}")
    print(f"  Failed: {fail_count}")
    print("\nDetailed results saved to api_migration_status.csv")


if __name__ == "__main__":
    main() 