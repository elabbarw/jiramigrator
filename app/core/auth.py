"""Microsoft Entra ID (Azure AD) authentication module."""
import os
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from functools import wraps

import msal
from fastapi import Request, HTTPException, Depends
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from jose import jwt, JWTError

from app.core.config import settings


def load_certificate(cert_path: str, key_path: str) -> Optional[Dict[str, str]]:
    """Load certificate and private key for MSAL authentication.
    
    Args:
        cert_path: Path to the certificate file (.crt or .pem with cert)
        key_path: Path to the private key file (.pem or .key)
    
    Returns:
        Dictionary with private_key and thumbprint for MSAL, or None if loading fails
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.backends import default_backend
        import hashlib
        
        # Read certificate file
        if not os.path.exists(cert_path):
            return None
        
        with open(cert_path, 'rb') as f:
            cert_data = f.read()
        
        # Parse certificate to get thumbprint
        cert = x509.load_pem_x509_certificate(cert_data, default_backend())
        
        # Calculate thumbprint (SHA-1 hash of DER-encoded certificate)
        thumbprint = cert.fingerprint(hashes.SHA1()).hex().upper()
        
        # Read private key file
        if not os.path.exists(key_path):
            return None
        
        with open(key_path, 'rb') as f:
            key_data = f.read()
        
        # Decode private key to verify it's valid, then return as string
        private_key = serialization.load_pem_private_key(
            key_data,
            password=None,
            backend=default_backend()
        )
        
        # Convert back to PEM string format for MSAL
        private_key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')
        
        return {
            "private_key": private_key_pem,
            "thumbprint": thumbprint
        }
        
    except Exception as e:
        print(f"Error loading certificate: {e}")
        return None


class EntraIDAuth:
    """Microsoft Entra ID authentication handler.
    
    Supports both certificate-based and client secret authentication.
    Certificate-based auth is preferred and more secure.
    """
    
    def __init__(self):
        self.client_id = os.getenv("AZURE_CLIENT_ID", settings.AZURE_CLIENT_ID)
        self.client_secret = os.getenv("AZURE_CLIENT_SECRET", settings.AZURE_CLIENT_SECRET)
        self.tenant_id = os.getenv("AZURE_TENANT_ID", settings.AZURE_TENANT_ID)
        self.redirect_uri = os.getenv("AZURE_REDIRECT_URI", settings.AZURE_REDIRECT_URI)
        
        # Certificate paths
        self.cert_path = os.getenv("AZURE_CERT_PATH", settings.AZURE_CERT_PATH)
        self.key_path = os.getenv("AZURE_KEY_PATH", settings.AZURE_KEY_PATH)
        self.cert_thumbprint = os.getenv("AZURE_CERT_THUMBPRINT", settings.AZURE_CERT_THUMBPRINT)
        
        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self.scope = ["User.Read"]
        
        self._msal_app = None
        self._credential = None
    
    def _get_credential(self) -> Optional[Any]:
        """Get the credential for MSAL (certificate or secret)."""
        if self._credential is not None:
            return self._credential
        
        # Try certificate first (more secure)
        if self.cert_path and self.key_path:
            cert_credential = load_certificate(self.cert_path, self.key_path)
            if cert_credential:
                self._credential = cert_credential
                print("Using certificate-based authentication for Entra ID")
                return self._credential
        
        # Fall back to thumbprint + key if provided separately
        if self.cert_thumbprint and self.key_path and os.path.exists(self.key_path):
            try:
                with open(self.key_path, 'r') as f:
                    private_key = f.read()
                self._credential = {
                    "private_key": private_key,
                    "thumbprint": self.cert_thumbprint
                }
                print("Using certificate thumbprint authentication for Entra ID")
                return self._credential
            except Exception as e:
                print(f"Error loading key file: {e}")
        
        # Fall back to client secret
        if self.client_secret:
            self._credential = self.client_secret
            print("Using client secret authentication for Entra ID")
            return self._credential
        
        return None
    
    @property
    def is_configured(self) -> bool:
        """Check if Entra ID is properly configured."""
        has_credential = bool(self._get_credential())
        return all([
            self.client_id,
            has_credential,
            self.tenant_id,
            self.redirect_uri
        ])
    
    @property
    def auth_method(self) -> str:
        """Return the authentication method being used."""
        cred = self._get_credential()
        if isinstance(cred, dict):
            return "certificate"
        elif cred:
            return "client_secret"
        return "none"
    
    @property
    def msal_app(self):
        """Get or create MSAL confidential client application."""
        if self._msal_app is None and self.is_configured:
            credential = self._get_credential()
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=credential,
                authority=self.authority
            )
        return self._msal_app
    
    def get_auth_url(self, state: str = None) -> str:
        """Generate authorization URL for login."""
        if not self.is_configured:
            raise ValueError("Entra ID is not configured")
        
        auth_url = self.msal_app.get_authorization_request_url(
            scopes=self.scope,
            redirect_uri=self.redirect_uri,
            state=state
        )
        return auth_url
    
    def acquire_token_by_code(self, code: str) -> Dict[str, Any]:
        """Exchange authorization code for tokens."""
        if not self.is_configured:
            raise ValueError("Entra ID is not configured")
        
        result = self.msal_app.acquire_token_by_authorization_code(
            code=code,
            scopes=self.scope,
            redirect_uri=self.redirect_uri
        )
        
        if "error" in result:
            raise ValueError(f"Token acquisition failed: {result.get('error_description', result.get('error'))}")
        
        return result
    
    def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """Get user info from Microsoft Graph API."""
        import requests
        
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers=headers
        )
        
        if response.status_code != 200:
            raise ValueError("Failed to fetch user info")
        
        return response.json()


# Global auth instance
entra_auth = EntraIDAuth()


def create_session_token(user_info: Dict[str, Any], access_token: str) -> str:
    """Create a session token for the user."""
    payload = {
        "sub": user_info.get("id"),
        "email": user_info.get("mail") or user_info.get("userPrincipalName"),
        "name": user_info.get("displayName"),
        "exp": datetime.utcnow() + timedelta(hours=8),
        "iat": datetime.utcnow()
    }
    
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
    return token


def decode_session_token(token: str) -> Optional[Dict[str, Any]]:
    """Decode and validate session token."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        return payload
    except JWTError:
        return None


async def get_current_user(request: Request) -> Optional[Dict[str, Any]]:
    """Get current user from session."""
    token = request.cookies.get("session_token")
    if not token:
        return None
    
    user = decode_session_token(token)
    return user


async def require_auth(request: Request) -> Dict[str, Any]:
    """Dependency that requires authentication."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to check authentication on protected routes."""
    
    # Routes that don't require authentication
    PUBLIC_ROUTES = [
        "/auth/login",
        "/auth/callback",
        "/auth/logout",
        "/health",
        "/static",
        "/favicon.ico"
    ]
    
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        
        # Skip auth for public routes
        for public_route in self.PUBLIC_ROUTES:
            if path.startswith(public_route):
                return await call_next(request)
        
        # Check if Entra ID is configured
        if not entra_auth.is_configured:
            # If not configured, allow access (for initial setup)
            request.state.user = {"name": "Admin", "email": "admin@local"}
            return await call_next(request)
        
        # Check for session token
        token = request.cookies.get("session_token")
        if not token:
            # Redirect to login for page requests
            if not path.startswith("/api/"):
                return RedirectResponse(url="/auth/login")
            raise HTTPException(status_code=401, detail="Not authenticated")
        
        # Validate token
        user = decode_session_token(token)
        if not user:
            if not path.startswith("/api/"):
                return RedirectResponse(url="/auth/login")
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        
        # Attach user to request
        request.state.user = user
        
        return await call_next(request)
