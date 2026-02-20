import logging
import os
import threading
import time
from urllib.parse import urlparse

import msal
import requests
from office365.runtime.auth.token_response import TokenResponse
from office365.runtime.client_request_exception import ClientRequestException
from office365.sharepoint.client_context import ClientContext
from office365.runtime.compat import get_absolute_url
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# SharePoint limits
SHAREPOINT_MAX_PATH_LENGTH = 400

# SharePoint authentication constants - loaded once at module level
SHAREPOINT_TENANT = os.getenv('SHAREPOINT_TENANT', 'your-tenant.onmicrosoft.com')
CERTIFICATE_THUMBPRINT = os.getenv('CERTIFICATE_THUMBPRINT', '')


def create_msal_app(tenant, client_id, thumbprint, private_key):
    """
    Create a reusable MSAL ConfidentialClientApplication.

    The app instance contains an in-memory token cache so that tokens are
    reused across requests and automatically refreshed when they expire.
    Create this ONCE and pass it to create_sharepoint_context() for each
    new ClientContext.
    """
    authority_url = f"https://login.microsoftonline.com/{tenant}"
    credentials = {
        "thumbprint": thumbprint,
        "private_key": private_key,
    }
    return msal.ConfidentialClientApplication(
        client_id,
        authority=authority_url,
        client_credential=credentials,
    )


def create_sharepoint_context(sharepoint_site, tenant, client_id, thumbprint, private_key,
                              msal_app=None):
    """
    Create a SharePoint ClientContext with certificate authentication.

    This function works around an issue where the Office365-REST-Python-Client
    library includes 'passphrase: None' in the MSAL credentials dictionary,
    which newer MSAL versions reject.

    Args:
        sharepoint_site: SharePoint site URL
        tenant: Azure AD tenant (e.g., 'contoso.onmicrosoft.com')
        client_id: Azure AD application client ID
        thumbprint: Certificate thumbprint (hex encoded)
        private_key: PEM-encoded private key content
        msal_app: Optional existing MSAL app to reuse (preserves token cache)

    Returns:
        ClientContext: Authenticated SharePoint client context
    """
    # Build scopes for SharePoint
    resource = get_absolute_url(sharepoint_site)
    scopes = [f"{resource}/.default"]

    # Reuse existing MSAL app or create a new one
    if msal_app is None:
        msal_app = create_msal_app(tenant, client_id, thumbprint, private_key)

    # Token acquisition function — uses the shared MSAL app so tokens are
    # cached and refreshed automatically instead of hitting Azure AD every time.
    def acquire_token():
        result = msal_app.acquire_token_for_client(scopes)
        if "error" in result:
            raise ValueError(f"Failed to acquire token: {result.get('error_description', result.get('error'))}")
        return TokenResponse.from_json(result)

    # Create context with custom token provider
    ctx = ClientContext(sharepoint_site)
    ctx.with_access_token(acquire_token)

    return ctx

# Configure a session with larger connection pool for SharePoint
sharepoint_session = requests.Session()
sharepoint_adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
sharepoint_session.mount('http://', sharepoint_adapter)
sharepoint_session.mount('https://', sharepoint_adapter)

# Global dictionary to store locks for each SharePoint folder
# This prevents concurrent uploads to the same folder
folder_locks = {}
folder_locks_mutex = threading.Lock()

def normalize_sharepoint_path(path):
    """
    Normalize SharePoint paths by:
    1. Ensuring consistent forward slashes
    2. Removing leading/trailing slashes
    3. Converting spaces to %20 only in path segments, not for the full path at once
    
    Args:
        path (str): The original SharePoint path
        
    Returns:
        str: Normalized SharePoint path
    """
    if not path:
        return ""
        
    # Replace backslashes with forward slashes
    path = path.replace('\\', '/')
    
    # Remove multiple forward slashes
    while '//' in path:
        path = path.replace('//', '/')
        
    # Remove leading and trailing slashes
    path = path.strip('/')
    
    # Handle URL encoding for individual path components only
    # This prevents double-encoding issues with SharePoint's APIs
    components = path.split('/')
    normalized_components = []
    
    for component in components:
        # Spaces in file/folder names should be URL encoded but we don't want to
        # URL encode the entire path as SharePoint sometimes does that internally
        if ' ' in component:
            component = component.replace(' ', '%20')
        normalized_components.append(component)
    
    return '/'.join(normalized_components)


class SharepointUpload():

    def __init__(self, sharepoint_site, client_id, key):
        # Save parameters as instance variables
        self.sharepoint_site = sharepoint_site
        self.client_id = client_id
        self.key = key

        # Configure session for SharePoint requests
        self.auth_session = sharepoint_session

        # Create a single MSAL app for the lifetime of this uploader.
        # The app holds an in-memory token cache so tokens are reused
        # across uploads and automatically refreshed on expiry, avoiding
        # redundant token requests that cause intermittent 401 errors.
        self._msal_app = create_msal_app(
            tenant=SHAREPOINT_TENANT,
            client_id=client_id,
            thumbprint=CERTIFICATE_THUMBPRINT,
            private_key=key,
        )

        # Set up and authenticate with SharePoint using the certificate
        self.ctx = create_sharepoint_context(
            sharepoint_site=sharepoint_site,
            tenant=SHAREPOINT_TENANT,
            client_id=client_id,
            thumbprint=CERTIFICATE_THUMBPRINT,
            private_key=key,
            msal_app=self._msal_app,
        )

        # Configure connection limits for SharePoint client
        # Increase timeout and connection limits
        if hasattr(self.ctx, 'request_timeout'):
            self.ctx.request_timeout = 120  # Increase timeout to 120 seconds

    def _shorten_filename(self, filename, max_length=120):
        """
        Shorten a filename to ensure it doesn't exceed SharePoint's limitations.
        
        Args:
            filename (str): The original filename
            max_length (int): Maximum length for the filename (default: 120 chars)
            
        Returns:
            str: Shortened filename that preserves extension
        """
        if len(filename) <= max_length:
            return filename
            
        # Preserve the file extension
        base, ext = os.path.splitext(filename)
        
        # Calculate how much we need to truncate
        # Leave room for ellipsis (...) plus the extension
        available_length = max_length - 3 - len(ext)
        
        # Truncate the base name and add ellipsis
        shortened_base = base[:available_length] + "..."
        
        # Return shortened filename with original extension
        return shortened_base + ext

    def _get_site_path(self):
        """Extract the site-relative path from the SharePoint site URL.

        E.g., 'https://gamesys.sharepoint.com/sites/ConfluenceSpacesArchive'
        returns '/sites/ConfluenceSpacesArchive'
        """
        parsed = urlparse(self.sharepoint_site)
        return parsed.path.rstrip('/')

    def _safe_filename_for_path(self, sharepoint_folder, filename):
        """Shorten filename if the total SharePoint path would exceed 400 chars.

        SharePoint enforces a 400-character limit on the full server-relative
        URL path for files and folders.

        Args:
            sharepoint_folder: The SharePoint folder path (site-relative)
            filename: The file name to potentially shorten

        Returns:
            str: The filename, shortened if necessary to fit the path limit
        """
        site_path = self._get_site_path()
        # Total server-relative path: /site_path/sharepoint_folder/filename
        total_length = len(site_path) + 1 + len(sharepoint_folder) + 1 + len(filename)

        if total_length <= SHAREPOINT_MAX_PATH_LENGTH:
            return filename

        # Calculate available space for filename
        overhead = len(site_path) + 1 + len(sharepoint_folder) + 1
        available = SHAREPOINT_MAX_PATH_LENGTH - overhead

        if available < 20:
            available = 20
            logger.warning(
                "SharePoint folder path is very long (%d chars), "
                "filename will be heavily truncated", overhead
            )

        shortened = self._shorten_filename(filename, max_length=available)
        logger.warning(
            "Truncated filename from '%s' to '%s' to fit SharePoint "
            "400-char path limit (folder path: %d chars)",
            filename, shortened, overhead
        )
        return shortened

    def _get_folder_lock(self, folder_path):
        """
        Get a lock for a specific SharePoint folder path.
        This prevents concurrent uploads to the same folder.

        Args:
            folder_path: The SharePoint folder path

        Returns:
            threading.Lock: A lock object for the folder
        """
        with folder_locks_mutex:
            if folder_path not in folder_locks:
                folder_locks[folder_path] = threading.Lock()
            return folder_locks[folder_path]

    @retry(
        retry=retry_if_exception_type((ClientRequestException, Exception)),
        stop=stop_after_attempt(5),  # Increased from 3 to 5
        wait=wait_exponential(multiplier=2, min=4, max=15)  # Increased max wait time
    )
    def upload_to_sharepoint(self, folder_path, sharepoint_folder, tags=None):
        """
        Upload files to SharePoint with improved error handling.
        
        Args:
            folder_path: Local folder containing files to upload
            sharepoint_folder: SharePoint destination folder path
            tags: Optional metadata tags for uploaded files
            
        Returns:
            bool: True if successful, False otherwise
        """
        # First, check if the local folder exists and has files
        if not os.path.exists(folder_path):
            # Keep this error print as it's essential
            print(f"Error: Local folder {folder_path} does not exist")
            return False
            
        # Check if there are any files to upload
        try:
            files_to_upload = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
            if not files_to_upload:
                return True  # Return success as there's nothing to upload
        except Exception as e:
            # Keep this error print as it's essential
            print(f"Error listing files in {folder_path}: {e}")
            raise
        
        # Normalize the SharePoint folder path
        sharepoint_folder = normalize_sharepoint_path(sharepoint_folder)
        
        # Get a lock for this specific SharePoint folder to prevent concurrent uploads
        folder_lock = self._get_folder_lock(sharepoint_folder)
        
        # Acquire the lock for this folder - this ensures only one thread can upload to this folder at a time
        with folder_lock:
            print(f"Acquired lock for folder {sharepoint_folder}")
            
            # Fresh context per upload to avoid stale connections, but reuse
            # the shared MSAL app so tokens come from cache instead of a new
            # Azure AD request each time.
            ctx = create_sharepoint_context(
                sharepoint_site=self.sharepoint_site,
                tenant=SHAREPOINT_TENANT,
                client_id=self.client_id,
                thumbprint=CERTIFICATE_THUMBPRINT,
                private_key=self.key,
                msal_app=self._msal_app,
            )
            
            # Add a delay to avoid rate limiting
            time.sleep(1)  # Reduced from 2 seconds to 1 second since we're now serializing access

            # Check if folder exists in SharePoint
            web = ctx.web
            target_folder = None
            
            try:
                # Create the folder structure with improved error handling
                self._ensure_folder_exists(ctx, web, sharepoint_folder)
                
                # Now get the folder we just created/verified
                target_folder = web.get_folder_by_server_relative_url(sharepoint_folder)
                ctx.load(target_folder)
                ctx.execute_query()
            except Exception as error:
                # Keep this error print as it's essential
                print(f"Error ensuring folder exists: {error}", flush=True)
                # Re-raise the exception for the retry decorator to handle
                raise
                
            # Add a delay after folder creation
            time.sleep(0.5)  # Reduced from 1 second to 0.5 seconds
            
            try:
                # Upload files to SharePoint
                for file_name in files_to_upload:
                    file_path = os.path.join(folder_path, file_name)
                    try:
                        with open(file_path, 'rb') as file:
                            file_content = file.read()
                            sp_filename = self._safe_filename_for_path(
                                sharepoint_folder, os.path.basename(file_name)
                            )
                            uploaded_file = target_folder.upload_file(sp_filename, file_content)
                            ctx.execute_query()

                            # Add tags to the file
                            if tags:
                                list_item = uploaded_file.listItemAllFields
                                for key, value in tags.items():
                                    list_item.set_property(key, value)
                                list_item.update()
                                ctx.execute_query()
                    except Exception as file_err:
                        # Keep this error print as it's essential
                        print(f"Error uploading file {file_name}: {file_err}")
                        # Continue with other files instead of failing the entire batch
                        continue
                
                print(f"Successfully uploaded files to {sharepoint_folder}")
                return True
            except Exception as err:
                # Keep this error print as it's essential but make it more concise
                print(f'SharePoint error: {err}', flush=True)
                # Reraise the exception for the retry decorator to handle
                raise
            
    def _ensure_folder_exists(self, ctx, web, folder_path):
        """
        Ensure a folder path exists in SharePoint, creating it if necessary.
        Uses a more robust approach to folder creation.
        
        Args:
            ctx: SharePoint client context
            web: SharePoint web object
            folder_path: The folder path to create
            
        Returns:
            The created/existing folder
        """
        # Break the path into parts
        path_parts = folder_path.strip('/').split('/')
        current_path = ""
        
        # Create each folder level as needed
        for i, part in enumerate(path_parts):
            if not part:  # Skip empty parts
                continue
                
            # Build up the path one component at a time
            if current_path:
                current_path += f"/{part}"
            else:
                current_path = part
            
            try:
                # Try to get the folder
                folder = web.get_folder_by_server_relative_url(current_path)
                ctx.load(folder)
                ctx.execute_query()
            except Exception:
                try:
                    # Get parent folder
                    parent_path = '/'.join(path_parts[:i]) if i > 0 else ""
                    
                    # If parent path is empty, use the root
                    if not parent_path:
                        parent_folder = web.root_folder
                    else:
                        parent_folder = web.get_folder_by_server_relative_url(parent_path)
                    
                    ctx.load(parent_folder)
                    ctx.execute_query()
                    
                    # Create this folder level
                    parent_folder.folders.add(part)
                    ctx.execute_query()
                    
                    # Verify the folder was created
                    folder = web.get_folder_by_server_relative_url(current_path)
                    ctx.load(folder)
                    ctx.execute_query()
                except Exception as create_error:
                    # Keep this error print as it's essential
                    print(f"Error creating folder {current_path}: {create_error}")
                    # Reraise to let the retry decorator handle it
                    raise
        
        return web.get_folder_by_server_relative_url(folder_path)

