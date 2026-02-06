"""Common helper functions for the application."""
from fastapi import HTTPException
from sqlalchemy.orm import Session
from typing import Type, TypeVar, Any

T = TypeVar('T')


def get_or_404(db: Session, model: Type[T], id: Any, id_field: str = "id") -> T:
    """Fetch a model instance by ID or raise 404."""
    instance = db.query(model).filter(getattr(model, id_field) == id).first()
    if not instance:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return instance
