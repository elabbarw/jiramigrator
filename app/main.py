import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from typing import Optional
import uvicorn

from app.api.endpoints import router as api_router
from app.api.jobs import router as jobs_router
from app.api.configurations import router as config_router
from app.api.auth import router as auth_router
from app.api.settings import router as settings_api_router
from app.api.websocket import manager
from app.core.config import settings
from app.core.database import init_db, SessionLocal
from app.core.auth import AuthMiddleware, entra_auth
from app.models.settings import EnvironmentVariable, DEFAULT_ENV_VARS


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    init_db()
    
    # Initialize default environment variables
    db = SessionLocal()
    try:
        for var_def in DEFAULT_ENV_VARS:
            existing = db.query(EnvironmentVariable).filter(
                EnvironmentVariable.key == var_def["key"]
            ).first()
            if not existing:
                env_var = EnvironmentVariable(
                    key=var_def["key"],
                    description=var_def.get("description"),
                    category=var_def.get("category", "general"),
                    is_secret=var_def.get("is_secret", False),
                    is_required=var_def.get("is_required", False)
                )
                db.add(env_var)
        db.commit()
    finally:
        db.close()
    
    yield


app = FastAPI(
    title="Jira Migration App",
    description="Full-stack application for migrating Jira issues to SharePoint or API endpoints",
    version="2.0.0",
    lifespan=lifespan
)

# Add authentication middleware
app.add_middleware(AuthMiddleware)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Set up Jinja2 templates
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["auth_enabled"] = entra_auth.is_configured

# Include API routes
app.include_router(auth_router)  # Auth routes at /auth
app.include_router(api_router, prefix="/api")
app.include_router(jobs_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(settings_api_router, prefix="/api")


# Custom exception handlers
@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle HTTP exceptions with custom error pages or JSON responses."""
    # For API routes, return JSON
    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "status_code": exc.status_code}
        )
    
    # For page routes, return HTML error templates
    if exc.status_code == 404:
        return templates.TemplateResponse(
            "404.html",
            {"request": request},
            status_code=404
        )
    elif exc.status_code == 500:
        return templates.TemplateResponse(
            "500.html",
            {"request": request},
            status_code=500
        )
    
    # For other HTTP errors, return JSON as fallback
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code}
    )


@app.exception_handler(Exception)
async def custom_exception_handler(request: Request, exc: Exception):
    """Handle unhandled exceptions with 500 error page or JSON response."""
    # For API routes, return JSON
    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "status_code": 500}
        )
    
    # For page routes, return HTML error template
    return templates.TemplateResponse(
        "500.html",
        {"request": request},
        status_code=500
    )


# WebSocket endpoint for real-time updates
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for dashboard updates."""
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, handle incoming messages
            data = await websocket.receive_text()
            # Echo back for ping/pong
            await websocket.send_text(f"received: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.websocket("/ws/job/{job_id}")
async def websocket_job_endpoint(websocket: WebSocket, job_id: str):
    """WebSocket endpoint for specific job updates."""
    await manager.connect(websocket, job_id)
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"received: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket, job_id)


# Page routes
@app.get("/")
async def index(request: Request):
    """Serve the dashboard page."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/migrate")
async def migrate_page(request: Request):
    """Serve the migration form page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/jobs")
async def jobs_page(request: Request):
    """Serve the jobs list page."""
    return templates.TemplateResponse("jobs.html", {"request": request})


@app.get("/jobs/{job_id}")
async def job_detail_page(request: Request, job_id: int):
    """Serve the job detail page."""
    return templates.TemplateResponse("job_detail.html", {"request": request, "job_id": job_id})


@app.get("/status/{job_id}")
async def status_page(request: Request, job_id: str):
    """Serve the status page for a specific job (legacy route)."""
    return templates.TemplateResponse("status.html", {"request": request, "job_id": job_id})


@app.get("/settings")
async def settings_page(request: Request):
    """Serve the settings page."""
    return templates.TemplateResponse("settings.html", {"request": request})


@app.get("/logs/{job_id}")
async def logs_page(request: Request, job_id: int):
    """Serve the migration logs page."""
    return templates.TemplateResponse("logs.html", {"request": request, "job_id": job_id})


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "version": "2.0.0"}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True) 