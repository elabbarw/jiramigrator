"""Configuration management API endpoints."""
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime
import os
import shutil

from app.core.database import get_db
from app.models.configuration import Configuration, Certificate

router = APIRouter(prefix="/configurations", tags=["configurations"])


class ConfigurationCreate(BaseModel):
    """Schema for creating a configuration."""
    name: str
    description: Optional[str] = None
    vault_url: Optional[str] = None
    export_method: str = "sharepoint"
    sharepoint_site: Optional[str] = None
    sharepoint_folder: Optional[str] = None
    api_type: Optional[str] = None
    environment: Optional[str] = None
    custom_api_url: Optional[str] = None
    custom_api_headers: Optional[dict] = None
    custom_api_params: Optional[dict] = None
    default_jql: Optional[str] = None
    default_project_key: Optional[str] = None
    parallelism: int = 5
    analyze_fields: bool = True
    show_all_fields: bool = False
    use_view_screen: bool = True
    identifier_field_terms: Optional[List[str]] = None
    important_field_terms: Optional[List[str]] = None
    include_manual_fields: bool = False
    manual_fields: Optional[List[str]] = None
    is_default: bool = False


class ConfigurationUpdate(BaseModel):
    """Schema for updating a configuration."""
    name: Optional[str] = None
    description: Optional[str] = None
    vault_url: Optional[str] = None
    export_method: Optional[str] = None
    sharepoint_site: Optional[str] = None
    sharepoint_folder: Optional[str] = None
    api_type: Optional[str] = None
    environment: Optional[str] = None
    custom_api_url: Optional[str] = None
    custom_api_headers: Optional[dict] = None
    custom_api_params: Optional[dict] = None
    default_jql: Optional[str] = None
    default_project_key: Optional[str] = None
    parallelism: Optional[int] = None
    analyze_fields: Optional[bool] = None
    show_all_fields: Optional[bool] = None
    use_view_screen: Optional[bool] = None
    identifier_field_terms: Optional[List[str]] = None
    important_field_terms: Optional[List[str]] = None
    include_manual_fields: Optional[bool] = None
    manual_fields: Optional[List[str]] = None
    is_default: Optional[bool] = None


@router.get("/")
async def list_configurations(db: Session = Depends(get_db)):
    """List all saved configurations."""
    configs = db.query(Configuration).order_by(Configuration.name).all()
    return {"configurations": [config.to_dict() for config in configs]}


@router.get("/default")
async def get_default_configuration(db: Session = Depends(get_db)):
    """Get the default configuration."""
    config = db.query(Configuration).filter(Configuration.is_default == True).first()
    if not config:
        return {"configuration": None}
    return {"configuration": config.to_dict()}


@router.get("/{config_id}")
async def get_configuration(config_id: int, db: Session = Depends(get_db)):
    """Get a specific configuration."""
    config = db.query(Configuration).filter(Configuration.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")
    return config.to_dict()


@router.post("/")
async def create_configuration(config: ConfigurationCreate, db: Session = Depends(get_db)):
    """Create a new configuration."""
    # Check for duplicate name
    existing = db.query(Configuration).filter(Configuration.name == config.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Configuration with this name already exists")
    
    # If this is set as default, unset other defaults
    if config.is_default:
        db.query(Configuration).filter(Configuration.is_default == True).update({"is_default": False})
    
    db_config = Configuration(**config.dict())
    db.add(db_config)
    db.commit()
    db.refresh(db_config)
    
    return db_config.to_dict()


@router.put("/{config_id}")
async def update_configuration(
    config_id: int, 
    config: ConfigurationUpdate, 
    db: Session = Depends(get_db)
):
    """Update an existing configuration."""
    db_config = db.query(Configuration).filter(Configuration.id == config_id).first()
    if not db_config:
        raise HTTPException(status_code=404, detail="Configuration not found")
    
    # Check for duplicate name if name is being changed
    if config.name and config.name != db_config.name:
        existing = db.query(Configuration).filter(Configuration.name == config.name).first()
        if existing:
            raise HTTPException(status_code=400, detail="Configuration with this name already exists")
    
    # If this is set as default, unset other defaults
    if config.is_default:
        db.query(Configuration).filter(
            Configuration.is_default == True,
            Configuration.id != config_id
        ).update({"is_default": False})
    
    # Update only provided fields
    update_data = config.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_config, key, value)
    
    db.commit()
    db.refresh(db_config)
    
    return db_config.to_dict()


@router.delete("/{config_id}")
async def delete_configuration(config_id: int, db: Session = Depends(get_db)):
    """Delete a configuration."""
    config = db.query(Configuration).filter(Configuration.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")
    
    db.delete(config)
    db.commit()
    
    return {"message": "Configuration deleted successfully"}


# Certificate endpoints

@router.get("/certificates/")
async def list_certificates(db: Session = Depends(get_db)):
    """List all certificates."""
    certs = db.query(Certificate).order_by(Certificate.name).all()
    return {"certificates": [cert.to_dict() for cert in certs]}


@router.get("/certificates/active")
async def get_active_certificate(db: Session = Depends(get_db)):
    """Get the active certificate."""
    cert = db.query(Certificate).filter(Certificate.is_active == True).first()
    if not cert:
        return {"certificate": None}
    return {"certificate": cert.to_dict()}


@router.post("/certificates/upload")
async def upload_certificate(
    name: str,
    cert_type: str,
    file: UploadFile = File(...),
    description: Optional[str] = None,
    client_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Upload a certificate file."""
    # Validate cert_type
    if cert_type not in ["crt", "pem", "key", "pfx"]:
        raise HTTPException(status_code=400, detail="Invalid certificate type")
    
    # Check for duplicate name
    existing = db.query(Certificate).filter(Certificate.name == name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Certificate with this name already exists")
    
    # Create certificates directory if it doesn't exist
    certs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "certificates")
    os.makedirs(certs_dir, exist_ok=True)
    
    # Save file
    file_path = os.path.join(certs_dir, f"{name}.{cert_type}")
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    # Create database record
    cert = Certificate(
        name=name,
        description=description,
        cert_type=cert_type,
        file_path=file_path,
        client_id=client_id,
        tenant_id=tenant_id
    )
    db.add(cert)
    db.commit()
    db.refresh(cert)
    
    return cert.to_dict()


@router.put("/certificates/{cert_id}/activate")
async def activate_certificate(cert_id: int, db: Session = Depends(get_db)):
    """Set a certificate as active."""
    cert = db.query(Certificate).filter(Certificate.id == cert_id).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    
    # Deactivate all other certificates
    db.query(Certificate).filter(Certificate.is_active == True).update({"is_active": False})
    
    # Activate this certificate
    cert.is_active = True
    db.commit()
    db.refresh(cert)
    
    return cert.to_dict()


@router.delete("/certificates/{cert_id}")
async def delete_certificate(cert_id: int, db: Session = Depends(get_db)):
    """Delete a certificate."""
    cert = db.query(Certificate).filter(Certificate.id == cert_id).first()
    if not cert:
        raise HTTPException(status_code=404, detail="Certificate not found")
    
    # Delete file if exists
    if cert.file_path and os.path.exists(cert.file_path):
        os.remove(cert.file_path)
    
    db.delete(cert)
    db.commit()
    
    return {"message": "Certificate deleted successfully"}
