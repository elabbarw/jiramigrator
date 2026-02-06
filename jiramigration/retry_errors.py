#!/usr/bin/env python3
import argparse
import csv
import sys
from sharepoint_cmd import JiraMigration, main as jira_main
from modules.uploadsharepoint import normalize_sharepoint_path

def retry_from_csv(csv_file, vault_name, vault_token, vault_url, **kwargs):
    """
    Retry Jira issues with 'error' status from a CSV file.
    
    Args:
        csv_file: Path to the CSV file containing keys and status
        vault_name: Jira username/vault name
        vault_token: Jira token/password
        vault_url: Jira URL
        **kwargs: Additional arguments to pass to the JiraMigration constructor
    """
    # Read the CSV file and filter for keys with error status
    error_keys = []
    try:
        with open(csv_file, 'r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                if 'key' in row and 'status' in row and row['status'].lower() == 'error':
                    error_keys.append(row['key'])
    except Exception as e:
        print(f"Error reading CSV file: {e}")
        return

    if not error_keys:
        print("No keys with error status found in the CSV file.")
        return

    print(f"Found {len(error_keys)} keys with error status to retry.")
    
    # Create args object to pass to JiraMigration
    class Args:
        pass
    
    args = Args()
    args.vault_name = vault_name
    args.vault_token = vault_token
    args.vault_url = vault_url
    args.parallelism = kwargs.get('parallelism', 5)
    args.max_retries = kwargs.get('max_retries', 3)
    
    # Set other required args based on the kwargs
    args.export_method = kwargs.get('export_method', 'sharepoint')
    args.sharepoint_site = kwargs.get('sharepoint_site')
    
    # Normalize SharePoint folder to preserve double spaces while trimming leading/trailing spaces
    if 'sharepoint_folder' in kwargs and kwargs['sharepoint_folder']:
        args.sharepoint_folder = normalize_sharepoint_path(kwargs['sharepoint_folder'])
        print(f"Using SharePoint folder path: '{args.sharepoint_folder}'")
    else:
        args.sharepoint_folder = None
        
    args.api_type = kwargs.get('api_type', 'custom')
    args.custom_api_url = kwargs.get('custom_api_url')
    args.show_all_fields = kwargs.get('show_all_fields', False)
    args.use_view_screen = kwargs.get('use_view_screen', True)
    args.include_manual_fields = kwargs.get('include_manual_fields', False)
    args.manual_fields = kwargs.get('manual_fields', [])
    args.identifier_field_terms = kwargs.get('identifier_field_terms', ["identifier", "id", "reference", "account"])
    args.project_key = kwargs.get('project_key')
    args.csv_file = None  # Not using a CSV file for input
    args.jql = None  # Not using a JQL query
    
    # Initialize and run the migration with our filtered issue keys
    migration = JiraMigration(args)
    result = migration.start_migration(error_keys)
    
    print(f"Retry completed. Total: {result['total']}, Processed: {result['processed']}, Failed: {result['failed']}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retry Jira issues with error status from a CSV file")
    parser.add_argument("--csv-file", required=True, help="Path to the CSV file with key and status columns")
    parser.add_argument("--vault-name", required=True, help="Vault name (Jira username)")
    parser.add_argument("--vault-token", required=True, help="Vault token (Jira password/token)")
    parser.add_argument("--vault-url", required=True, help="Vault URL (Jira URL)")
    parser.add_argument("--export-method", choices=['sharepoint', 'api'], default='sharepoint', help="Export method")
    parser.add_argument("--sharepoint-site", help="SharePoint site (required if export-method is sharepoint)")
    parser.add_argument("--sharepoint-folder", help="SharePoint folder (required if export-method is sharepoint)")
    parser.add_argument("--api-type", choices=['custom'], default='custom', help="API type (if export-method is api)")
    parser.add_argument("--custom-api-url", help="Custom API URL (if export-method is api)")
    parser.add_argument("--parallelism", type=int, default=3, help="Number of parallel tasks (default: 3)")
    parser.add_argument("--max-retries", type=int, default=5, help="Maximum number of retry attempts (default: 5)")
    parser.add_argument("--project-key", help="Project key")
    parser.add_argument("--show-all-fields", action="store_true", help="Show all fields in the PDF output, not just visible ones")
    parser.add_argument("--use-view-screen", action="store_true", default=True, help="Use view/read screens for field visibility")
    parser.add_argument("--include-manual-fields", action="store_true", help="Include manually specified fields")
    parser.add_argument("--manual-fields", help="Comma-separated list of field display names to include")
    
    args = parser.parse_args()
    
    # Convert args namespace to dictionary
    kwargs = vars(args)
    csv_file = kwargs.pop('csv_file')
    vault_name = kwargs.pop('vault_name')
    vault_token = kwargs.pop('vault_token')
    vault_url = kwargs.pop('vault_url')
    
    retry_from_csv(csv_file, vault_name, vault_token, vault_url, **kwargs) 