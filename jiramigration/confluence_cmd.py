import os
import re
import sys
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Callable, Any

import requests
from requests.auth import HTTPBasicAuth
from atlassian import Confluence

try:
    from .modules import SharepointUpload
    from .modules.uploadsharepoint import normalize_sharepoint_path
except ImportError:
    from modules import SharepointUpload
    from modules.uploadsharepoint import normalize_sharepoint_path


# SharePoint auth constants (same as sharepoint_cmd.py)
client_id = 'dff7b1da-cacd-4d6b-9b72-ec226fe1fd87'

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = sys._MEIPASS
        return os.path.join(base_path, relative_path)
    except Exception:
        possible_paths = [
            os.path.join(os.path.dirname(__file__), relative_path),
            os.path.join(os.path.abspath("."), relative_path),
            os.path.join(os.path.abspath("."), "jiramigration", relative_path),
            os.path.join("/app/jiramigration", relative_path),
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
        return possible_paths[0]


cert_file = get_resource_path('certificate.crt')
key_file = get_resource_path('certificate.pem')


def normalize_filename(filename: str) -> str:
    """Normalize a filename for safe use in file systems and SharePoint."""
    filename = filename.replace(' ', '_')
    filename = re.sub(r'[^\w\-.\(\)_]', '', filename)
    filename = re.sub(r'_+', '_', filename)
    filename = filename.strip('_')
    if len(filename) > 120:
        filename = filename[:120]
    return filename


def normalize_folder_name(name: str) -> str:
    """Normalize a folder name for SharePoint compatibility.

    SharePoint folder restrictions:
    - No leading/trailing dots or spaces
    - No consecutive dots
    - No special characters: ~ " # % & * : < > ? / \\ { | }
    - Max 50 characters per segment to prevent the total path from
      exceeding SharePoint's 400-char URL limit when multiple ancestor
      levels are combined.
    """
    name = name.replace(' ', '_')
    # Remove characters not allowed in SharePoint folder names
    name = re.sub(r'[^\w\-_]', '', name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name)
    # Strip leading/trailing underscores and dots
    name = name.strip('_.')
    # Truncate to 50 chars per segment
    if len(name) > 50:
        name = name[:50].rstrip('_.')
    return name or 'Untitled'


class ConfluenceMigration:
    """Handles migration of Confluence pages to SharePoint as PDFs."""

    def __init__(self, args):
        self.confluence_url = args.confluence_url
        self.confluence_username = args.confluence_username
        self.confluence_token = args.confluence_token
        self.space_key = getattr(args, 'space_key', None)
        self.cql = getattr(args, 'cql', None)
        self.sharepoint_site = args.sharepoint_site
        self.sharepoint_folder = args.sharepoint_folder
        self.parallelism = getattr(args, 'parallelism', 5)
        self.skip_issues = getattr(args, 'skip_issues', set())
        self.progress_callback = None

        # Connect to Confluence
        self.confluence = Confluence(
            url=self.confluence_url,
            username=self.confluence_username,
            password=self.confluence_token,
        )

        # Set up SharePoint uploader
        with open(key_file, 'r') as f:
            private_key = f.read()
        self.sp_uploader = SharepointUpload(self.sharepoint_site, client_id, private_key)

    def set_progress_callback(self, callback: Callable):
        self.progress_callback = callback

    def _report_progress(self, page_title: str, processed: int, total: int,
                         status: str, error: str = None):
        if self.progress_callback:
            self.progress_callback(
                issue_key=page_title,
                processed=processed,
                total=total,
                status=status,
                error=error,
                pdf_generated=(status == 'success'),
                attachments_count=0,
                files_uploaded=1 if status == 'success' else 0,
            )

    def fetch_pages(self) -> List[Dict]:
        """Fetch all matching pages from Confluence with ancestors expanded."""
        pages = []
        limit = 25

        if self.cql:
            start = 0
            while True:
                results = self.confluence.cql(
                    self.cql,
                    start=start,
                    limit=limit,
                    expand='ancestors',
                )
                batch = results.get('results', [])
                if not batch:
                    break
                for item in batch:
                    content = item.get('content', item)
                    pages.append(content)
                start += len(batch)
                if start >= results.get('totalSize', 0):
                    break
        elif self.space_key:
            start = 0
            while True:
                results = self.confluence.get_all_pages_from_space(
                    self.space_key,
                    start=start,
                    limit=limit,
                    expand='ancestors',
                )
                if not results:
                    break
                pages.extend(results)
                if len(results) < limit:
                    break
                start += len(results)

        return pages

    def build_page_tree(self, pages: List[Dict]) -> Dict[str, str]:
        """Build a mapping of page_id -> SharePoint folder path.

        Returns a dict where each key is a page id (str) and the value
        is the relative SharePoint folder path for that page's PDF.
        """
        # Get space name for the top-level folder
        space_name = self.space_key or 'Confluence'
        if self.space_key:
            try:
                space_info = self.confluence.get_space(self.space_key)
                space_name = space_info.get('name', self.space_key)
            except Exception:
                space_name = self.space_key

        page_paths: Dict[str, str] = {}
        for page in pages:
            page_id = str(page['id'])
            ancestors = page.get('ancestors', [])
            # Build path from ancestors using folder-safe names
            path_parts = [normalize_folder_name(space_name)]
            for ancestor in ancestors:
                path_parts.append(normalize_folder_name(ancestor.get('title', 'Untitled')))
            # The page itself becomes a folder only if it has children;
            # but for simplicity, we always place the PDF in the ancestor path.
            # Pages with children will naturally have their folder created
            # when child pages reference them as ancestors.
            page_paths[page_id] = '/'.join(path_parts)

        return page_paths

    def export_page_pdf(self, page_id: str, page_title: str, dest_dir: str) -> str:
        """Export a single page as PDF using Confluence's native export.

        Returns the path to the downloaded PDF file.
        """
        filename = normalize_filename(page_title) + '.pdf'
        filepath = os.path.join(dest_dir, filename)

        # Use requests directly for PDF export (atlassian-python-api
        # doesn't have a direct PDF export method for Server)
        url = f"{self.confluence_url}/spaces/flyingpdf/pdfpageexport.action?pageId={page_id}"
        response = requests.get(
            url,
            auth=HTTPBasicAuth(self.confluence_username, self.confluence_token),
            stream=True,
            timeout=120,
        )
        response.raise_for_status()

        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return filepath

    def migrate_page(self, page: Dict, page_paths: Dict[str, str],
                     temp_dir: str, processed_counter: List[int],
                     total: int, lock: threading.Lock) -> Dict[str, Any]:
        """Migrate a single page: export PDF and upload to SharePoint."""
        page_id = str(page['id'])
        page_title = page.get('title', 'Untitled')
        result = {'page_id': page_id, 'title': page_title, 'status': 'failed'}

        try:
            # Create temp directory for this page
            page_dir = os.path.join(temp_dir, f"page_{page_id}")
            os.makedirs(page_dir, exist_ok=True)

            # Export PDF
            pdf_path = self.export_page_pdf(page_id, page_title, page_dir)

            # Build SharePoint destination path
            sp_folder = page_paths.get(page_id, 'Confluence')
            full_sp_path = normalize_sharepoint_path(
                f"{self.sharepoint_folder}/{sp_folder}"
            )

            # Upload to SharePoint
            self.sp_uploader.upload_to_sharepoint(page_dir, full_sp_path)

            result['status'] = 'success'

        except Exception as e:
            result['status'] = 'failed'
            result['error'] = str(e)
            print(f"Error migrating page '{page_title}': {e}")

        finally:
            # Update progress
            with lock:
                processed_counter[0] += 1
                current = processed_counter[0]

            self._report_progress(
                page_title, current, total,
                result['status'],
                error=result.get('error'),
            )

            # Clean up temp page directory
            page_dir = os.path.join(temp_dir, f"page_{page_id}")
            if os.path.exists(page_dir):
                shutil.rmtree(page_dir, ignore_errors=True)

        return result

    def start_migration(self) -> Dict[str, Any]:
        """Run the full Confluence migration."""
        print(f"Starting Confluence migration from {self.confluence_url}")

        # 1. Fetch pages
        print("Fetching pages from Confluence...")
        pages = self.fetch_pages()

        # Filter out already-successful pages when resuming
        if self.skip_issues:
            original_count = len(pages)
            pages = [p for p in pages if p.get('title', '') not in self.skip_issues]
            print(f"Skipping {original_count - len(pages)} already-processed pages, {len(pages)} remaining")

        total = len(pages)
        print(f"Found {total} pages to migrate")

        if total == 0:
            return {
                'status': 'completed',
                'total_pages': 0,
                'successful': 0,
                'failed': 0,
                'results': [],
            }

        # 2. Build page tree
        page_paths = self.build_page_tree(pages)

        # 3. Process pages in parallel
        temp_dir = tempfile.mkdtemp(prefix='confluence_migration_')
        processed_counter = [0]  # Mutable counter for threads
        lock = threading.Lock()
        results = []

        try:
            with ThreadPoolExecutor(max_workers=self.parallelism) as executor:
                futures = {
                    executor.submit(
                        self.migrate_page, page, page_paths,
                        temp_dir, processed_counter, total, lock
                    ): page
                    for page in pages
                }
                for future in as_completed(futures):
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as e:
                        page = futures[future]
                        results.append({
                            'page_id': str(page.get('id', '?')),
                            'title': page.get('title', 'Unknown'),
                            'status': 'failed',
                            'error': str(e),
                        })
        finally:
            # Clean up temp directory
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

        successful = sum(1 for r in results if r['status'] == 'success')
        failed = sum(1 for r in results if r['status'] == 'failed')

        print(f"Migration complete: {successful} succeeded, {failed} failed out of {total}")

        return {
            'status': 'completed',
            'total_pages': total,
            'successful': successful,
            'failed': failed,
            'results': results,
        }
