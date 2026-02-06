import os
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Try to import pydantic v2, fallback to v1
try:
    from pydantic_settings import BaseSettings
except ImportError:
    from pydantic import BaseSettings


class Settings(BaseSettings):
    """Application settings."""
    # API settings
    API_V1_STR: str = "/api/v1"
    APP_NAME: str = "Jira Migration App"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    
    # Database settings
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./jira_migration.db")
    
    # JIRA settings
    JIRA_URL: str = os.getenv("JIRA_URL", "")
    JIRA_USERNAME: str = os.getenv("username", "")
    JIRA_TOKEN: str = os.getenv("password", "")
    
    # Celery settings
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
    
    # Email settings
    SMTP_SERVER: str = os.getenv("SMTP_SERVER", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "")
    
    # SharePoint settings
    SHAREPOINT_SITE: str = os.getenv("SHAREPOINT_SITE", "")
    CLIENT_ID: str = os.getenv("client_id", "")

    # File paths
    CERT_FILE: str = os.getenv("CERT_FILE", "certs/certificate.crt")
    KEY_FILE: str = os.getenv("KEY_FILE", "certs/certificate.pem")
    
    # Security settings
    SECRET_KEY: str = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
    
    # Azure/Entra ID settings
    AZURE_CLIENT_ID: str = os.getenv("AZURE_CLIENT_ID", "")
    AZURE_CLIENT_SECRET: str = os.getenv("AZURE_CLIENT_SECRET", "")  # Optional if using certificate
    AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", "")
    AZURE_REDIRECT_URI: str = os.getenv("AZURE_REDIRECT_URI", "http://localhost:8000/auth/callback")
    
    # Certificate-based authentication (preferred over client secret)
    AZURE_CERT_PATH: str = os.getenv("AZURE_CERT_PATH", "certs/certificate.crt")  # Path to certificate file
    AZURE_KEY_PATH: str = os.getenv("AZURE_KEY_PATH", "certs/certificate.pem")    # Path to private key file
    AZURE_CERT_THUMBPRINT: str = os.getenv("AZURE_CERT_THUMBPRINT", "")     # Optional: certificate thumbprint
    
    class Config:
        case_sensitive = True
        env_file = ".env"


settings = Settings() 