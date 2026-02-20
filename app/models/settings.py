"""Settings/Environment variables database model."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.orm import validates
from cryptography.fernet import Fernet
import os
import base64

from app.core.database import Base


def get_encryption_key():
    """Get or generate encryption key for sensitive values."""
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        # Use SECRET_KEY to derive encryption key
        secret = os.getenv("SECRET_KEY", "default-secret-key-change-me")
        # Pad/truncate to 32 bytes and base64 encode for Fernet
        key_bytes = secret.encode()[:32].ljust(32, b'\0')
        key = base64.urlsafe_b64encode(key_bytes).decode()
    return key


class EnvironmentVariable(Base):
    """Environment variable stored in database."""
    __tablename__ = "environment_variables"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(255), unique=True, index=True, nullable=False)
    value = Column(Text, nullable=True)
    
    # Metadata
    description = Column(Text, nullable=True)
    category = Column(String(100), default="general")  # jira, sharepoint, email, azure, general
    is_secret = Column(Boolean, default=False)  # If true, value is encrypted
    is_required = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(255), nullable=True)

    def set_value(self, value: str):
        """Set value, encrypting if this is a secret."""
        if self.is_secret and value:
            try:
                key = get_encryption_key()
                f = Fernet(key.encode() if isinstance(key, str) else key)
                self.value = f.encrypt(value.encode()).decode()
            except Exception:
                # If encryption fails, store as-is (not ideal but prevents data loss)
                self.value = value
        else:
            self.value = value
    
    def get_value(self) -> str:
        """Get value, decrypting if this is a secret."""
        if self.is_secret and self.value:
            try:
                key = get_encryption_key()
                f = Fernet(key.encode() if isinstance(key, str) else key)
                return f.decrypt(self.value.encode()).decode()
            except Exception:
                # If decryption fails, return masked value
                return "********"
        return self.value or ""
    
    def to_dict(self, include_value: bool = True):
        """Convert to dictionary."""
        result = {
            "id": self.id,
            "key": self.key,
            "description": self.description,
            "category": self.category,
            "is_secret": self.is_secret,
            "is_required": self.is_required,
            "has_value": bool(self.value),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }
        
        if include_value:
            if self.is_secret:
                # Mask secret values
                result["value"] = "********" if self.value else ""
            else:
                result["value"] = self.value or ""
        
        return result


# Default environment variables to initialize
DEFAULT_ENV_VARS = [
    # Jira settings
    {"key": "JIRA_URL", "description": "Jira instance URL (e.g., https://company.atlassian.net)", "category": "jira", "is_required": True},
    {"key": "JIRA_USERNAME", "description": "Jira username or email", "category": "jira", "is_required": True},
    {"key": "JIRA_TOKEN", "description": "Jira API token", "category": "jira", "is_secret": True, "is_required": True},
    
    # SharePoint settings
    {"key": "SHAREPOINT_SITE", "description": "SharePoint site URL", "category": "sharepoint"},
    {"key": "SHAREPOINT_CLIENT_ID", "description": "Azure AD App Client ID for SharePoint", "category": "sharepoint"},
    {"key": "SHAREPOINT_TENANT", "description": "SharePoint tenant (e.g., company.onmicrosoft.com)", "category": "sharepoint"},
    {"key": "SHAREPOINT_THUMBPRINT", "description": "Certificate thumbprint for SharePoint auth", "category": "sharepoint"},
    
    # Azure/Entra ID settings
    {"key": "AZURE_CLIENT_ID", "description": "Azure AD App Client ID for login", "category": "azure", "is_required": True},
    {"key": "AZURE_TENANT_ID", "description": "Azure AD Tenant ID", "category": "azure", "is_required": True},
    {"key": "AZURE_REDIRECT_URI", "description": "OAuth redirect URI (e.g., http://localhost:8000/auth/callback)", "category": "azure", "is_required": True},
    {"key": "AZURE_CERT_PATH", "description": "Path to certificate file (.crt) for Entra ID auth", "category": "azure"},
    {"key": "AZURE_KEY_PATH", "description": "Path to private key file (.pem) for Entra ID auth", "category": "azure"},
    {"key": "AZURE_CERT_THUMBPRINT", "description": "Certificate thumbprint (auto-calculated if not provided)", "category": "azure"},
    {"key": "AZURE_CLIENT_SECRET", "description": "Azure AD App Client Secret (only if not using certificate)", "category": "azure", "is_secret": True},
    
    # Email settings
    {"key": "SMTP_SERVER", "description": "SMTP server hostname", "category": "email"},
    {"key": "SMTP_PORT", "description": "SMTP server port", "category": "email"},
    {"key": "SMTP_USERNAME", "description": "SMTP username", "category": "email"},
    {"key": "SMTP_PASSWORD", "description": "SMTP password", "category": "email", "is_secret": True},
    {"key": "EMAIL_FROM", "description": "From email address for notifications", "category": "email"},

    # Confluence settings
    {"key": "CONFLUENCE_URL", "description": "Confluence Server/DC base URL (e.g., https://confluence.company.com)", "category": "confluence", "is_required": True},
    {"key": "CONFLUENCE_USERNAME", "description": "Confluence username", "category": "confluence", "is_required": True},
    {"key": "CONFLUENCE_TOKEN", "description": "Confluence password or personal access token", "category": "confluence", "is_secret": True, "is_required": True},
]
