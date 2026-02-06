"""Settings/Environment variables API endpoints."""
from fastapi import APIRouter, HTTPException, Depends, Request
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
from pydantic import BaseModel

from app.core.database import get_db
from app.core.auth import require_auth
from app.models.settings import EnvironmentVariable, DEFAULT_ENV_VARS

router = APIRouter(prefix="/settings", tags=["settings"])


class EnvVarUpdate(BaseModel):
    """Schema for updating an environment variable."""
    value: Optional[str] = None
    description: Optional[str] = None


class EnvVarCreate(BaseModel):
    """Schema for creating an environment variable."""
    key: str
    value: Optional[str] = None
    description: Optional[str] = None
    category: str = "general"
    is_secret: bool = False
    is_required: bool = False


@router.get("/env")
async def list_env_vars(
    request: Request,
    category: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all environment variables."""
    query = db.query(EnvironmentVariable)
    
    if category:
        query = query.filter(EnvironmentVariable.category == category)
    
    env_vars = query.order_by(EnvironmentVariable.category, EnvironmentVariable.key).all()
    
    # Group by category
    grouped = {}
    for var in env_vars:
        cat = var.category or "general"
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(var.to_dict())
    
    return {
        "environment_variables": [var.to_dict() for var in env_vars],
        "grouped": grouped,
        "categories": list(grouped.keys())
    }


@router.get("/env/{key}")
async def get_env_var(
    key: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """Get a specific environment variable."""
    env_var = db.query(EnvironmentVariable).filter(EnvironmentVariable.key == key).first()
    if not env_var:
        raise HTTPException(status_code=404, detail="Environment variable not found")
    
    return env_var.to_dict()


@router.put("/env/{key}")
async def update_env_var(
    key: str,
    data: EnvVarUpdate,
    request: Request,
    db: Session = Depends(get_db)
):
    """Update an environment variable."""
    env_var = db.query(EnvironmentVariable).filter(EnvironmentVariable.key == key).first()
    if not env_var:
        raise HTTPException(status_code=404, detail="Environment variable not found")
    
    # Get current user for audit
    user = getattr(request.state, 'user', None)
    user_email = user.get('email', 'unknown') if user else 'unknown'
    
    if data.value is not None:
        env_var.set_value(data.value)
    
    if data.description is not None:
        env_var.description = data.description
    
    env_var.updated_by = user_email
    
    db.commit()
    db.refresh(env_var)
    
    return env_var.to_dict()


@router.post("/env")
async def create_env_var(
    data: EnvVarCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    """Create a new environment variable."""
    # Check if key already exists
    existing = db.query(EnvironmentVariable).filter(EnvironmentVariable.key == data.key).first()
    if existing:
        raise HTTPException(status_code=400, detail="Environment variable already exists")
    
    # Get current user for audit
    user = getattr(request.state, 'user', None)
    user_email = user.get('email', 'unknown') if user else 'unknown'
    
    env_var = EnvironmentVariable(
        key=data.key,
        description=data.description,
        category=data.category,
        is_secret=data.is_secret,
        is_required=data.is_required,
        updated_by=user_email
    )
    env_var.set_value(data.value)
    
    db.add(env_var)
    db.commit()
    db.refresh(env_var)
    
    return env_var.to_dict()


@router.delete("/env/{key}")
async def delete_env_var(
    key: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """Delete an environment variable."""
    env_var = db.query(EnvironmentVariable).filter(EnvironmentVariable.key == key).first()
    if not env_var:
        raise HTTPException(status_code=404, detail="Environment variable not found")
    
    db.delete(env_var)
    db.commit()
    
    return {"message": f"Environment variable {key} deleted"}


@router.post("/env/initialize")
async def initialize_env_vars(
    request: Request,
    db: Session = Depends(get_db)
):
    """Initialize default environment variables."""
    created = []
    skipped = []
    
    for var_def in DEFAULT_ENV_VARS:
        existing = db.query(EnvironmentVariable).filter(
            EnvironmentVariable.key == var_def["key"]
        ).first()
        
        if existing:
            skipped.append(var_def["key"])
            continue
        
        env_var = EnvironmentVariable(
            key=var_def["key"],
            description=var_def.get("description"),
            category=var_def.get("category", "general"),
            is_secret=var_def.get("is_secret", False),
            is_required=var_def.get("is_required", False)
        )
        db.add(env_var)
        created.append(var_def["key"])
    
    db.commit()
    
    return {
        "created": created,
        "skipped": skipped,
        "message": f"Created {len(created)} variables, skipped {len(skipped)} existing"
    }


@router.post("/env/bulk-update")
async def bulk_update_env_vars(
    updates: Dict[str, str],
    request: Request,
    db: Session = Depends(get_db)
):
    """Bulk update multiple environment variables."""
    user = getattr(request.state, 'user', None)
    user_email = user.get('email', 'unknown') if user else 'unknown'
    
    updated = []
    not_found = []
    
    for key, value in updates.items():
        env_var = db.query(EnvironmentVariable).filter(EnvironmentVariable.key == key).first()
        if not env_var:
            not_found.append(key)
            continue
        
        env_var.set_value(value)
        env_var.updated_by = user_email
        updated.append(key)
    
    db.commit()
    
    return {
        "updated": updated,
        "not_found": not_found,
        "message": f"Updated {len(updated)} variables"
    }


@router.get("/env/export")
async def export_env_vars(
    request: Request,
    include_secrets: bool = False,
    db: Session = Depends(get_db)
):
    """Export environment variables as .env format."""
    env_vars = db.query(EnvironmentVariable).order_by(EnvironmentVariable.category).all()
    
    lines = ["# Jira Migration App - Environment Variables", "# Generated export", ""]
    current_category = None
    
    for var in env_vars:
        if var.category != current_category:
            lines.append(f"\n# {var.category.upper()}")
            current_category = var.category
        
        if var.is_secret and not include_secrets:
            value = "# [SECRET - not exported]"
            lines.append(f"# {var.key}={value}")
        else:
            value = var.get_value() if include_secrets else (var.value or "")
            lines.append(f"{var.key}={value}")
    
    return {
        "content": "\n".join(lines),
        "count": len(env_vars)
    }


def get_env_value(key: str, db: Session = None, default: str = None) -> Optional[str]:
    """Helper function to get environment variable value.
    
    First checks database, then falls back to os.environ, then default.
    """
    import os
    
    # Try database first
    if db:
        env_var = db.query(EnvironmentVariable).filter(EnvironmentVariable.key == key).first()
        if env_var and env_var.value:
            return env_var.get_value()
    
    # Fall back to os.environ
    os_value = os.getenv(key)
    if os_value:
        return os_value
    
    return default
