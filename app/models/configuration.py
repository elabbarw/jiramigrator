"""Configuration-related database models."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, JSON, LargeBinary
from app.core.database import Base


class Configuration(Base):
    """Saved migration configurations."""
    __tablename__ = "configurations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True)
    description = Column(Text, nullable=True)
    
    # Vault settings (credentials not stored, just URLs)
    vault_url = Column(String(500), nullable=True)
    
    # Export settings
    export_method = Column(String(50), default="sharepoint")
    sharepoint_site = Column(String(500), nullable=True)
    sharepoint_folder = Column(String(500), nullable=True)
    api_type = Column(String(50), nullable=True)
    environment = Column(String(50), nullable=True)
    custom_api_url = Column(String(500), nullable=True)
    custom_api_headers = Column(JSON, nullable=True)
    custom_api_params = Column(JSON, nullable=True)
    
    # Default query settings
    default_jql = Column(Text, nullable=True)
    default_project_key = Column(String(50), nullable=True)
    
    # Processing settings
    parallelism = Column(Integer, default=5)
    
    # Field analysis settings
    analyze_fields = Column(Boolean, default=True)
    show_all_fields = Column(Boolean, default=False)
    use_view_screen = Column(Boolean, default=True)
    identifier_field_terms = Column(JSON, nullable=True)
    important_field_terms = Column(JSON, nullable=True)
    include_manual_fields = Column(Boolean, default=False)
    manual_fields = Column(JSON, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Is this the default config?
    is_default = Column(Boolean, default=False)

    def to_dict(self):
        """Convert configuration to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "vault_url": self.vault_url,
            "export_method": self.export_method,
            "sharepoint_site": self.sharepoint_site,
            "sharepoint_folder": self.sharepoint_folder,
            "api_type": self.api_type,
            "environment": self.environment,
            "custom_api_url": self.custom_api_url,
            "custom_api_headers": self.custom_api_headers,
            "custom_api_params": self.custom_api_params,
            "default_jql": self.default_jql,
            "default_project_key": self.default_project_key,
            "parallelism": self.parallelism,
            "analyze_fields": self.analyze_fields,
            "show_all_fields": self.show_all_fields,
            "use_view_screen": self.use_view_screen,
            "identifier_field_terms": self.identifier_field_terms,
            "important_field_terms": self.important_field_terms,
            "include_manual_fields": self.include_manual_fields,
            "manual_fields": self.manual_fields,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "is_default": self.is_default,
        }


class Certificate(Base):
    """Certificate storage for SharePoint authentication."""
    __tablename__ = "certificates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True)
    description = Column(Text, nullable=True)
    
    # Certificate type
    cert_type = Column(String(50))  # 'crt', 'pem', 'key', 'pfx'
    
    # Certificate data (encrypted in production)
    cert_data = Column(LargeBinary, nullable=True)
    
    # File paths (alternative to storing in DB)
    file_path = Column(String(500), nullable=True)
    
    # Metadata
    thumbprint = Column(String(100), nullable=True)
    expiry_date = Column(DateTime, nullable=True)
    
    # Azure AD settings (if applicable)
    client_id = Column(String(255), nullable=True)
    tenant_id = Column(String(255), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Is this the active certificate?
    is_active = Column(Boolean, default=False)

    def to_dict(self):
        """Convert certificate to dictionary (without sensitive data)."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "cert_type": self.cert_type,
            "file_path": self.file_path,
            "thumbprint": self.thumbprint,
            "expiry_date": self.expiry_date.isoformat() if self.expiry_date else None,
            "client_id": self.client_id,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "is_active": self.is_active,
        }
