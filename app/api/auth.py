"""Authentication API endpoints."""
import secrets
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from typing import Optional

from app.core.auth import entra_auth, create_session_token, get_current_user

router = APIRouter(prefix="/auth", tags=["authentication"])


def render_auth_message(title: str, message: str, button_text: str = "Go to Login",
                        button_url: str = "/auth/login", is_error: bool = True) -> HTMLResponse:
    """Render a simple auth message page."""
    border_class = "border-danger" if is_error else "border-success"
    text_class = "text-danger" if is_error else "text-success"
    return HTMLResponse(content=f'''<!DOCTYPE html>
<html>
<head>
    <title>{title} - Jira Migration</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
    <div class="container mt-5">
        <div class="row justify-content-center">
            <div class="col-md-6">
                <div class="card shadow {border_class}">
                    <div class="card-body text-center p-5">
                        <h2 class="{text_class} mb-4">{title}</h2>
                        <p class="text-muted">{message}</p>
                        <a href="{button_url}" class="btn btn-primary">{button_text}</a>
                    </div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>''')


@router.get("/login")
async def login(request: Request):
    """Initiate login flow."""
    if not entra_auth.is_configured:
        # If Entra ID not configured, show setup message
        return HTMLResponse(content="""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Login - Jira Migration</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        </head>
        <body class="bg-light">
            <div class="container mt-5">
                <div class="row justify-content-center">
                    <div class="col-md-6">
                        <div class="card shadow">
                            <div class="card-body text-center p-5">
                                <h2 class="mb-4">Entra ID Not Configured</h2>
                                <p class="text-muted">Microsoft Entra ID authentication is not configured yet.</p>
                                <p>Please configure the following environment variables:</p>
                                <ul class="list-unstyled text-start bg-light p-3 rounded">
                                    <li><code>AZURE_CLIENT_ID</code></li>
                                    <li><code>AZURE_CLIENT_SECRET</code></li>
                                    <li><code>AZURE_TENANT_ID</code></li>
                                    <li><code>AZURE_REDIRECT_URI</code></li>
                                </ul>
                                <a href="/" class="btn btn-primary">Continue Without Auth</a>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """)
    
    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    
    # Get authorization URL
    auth_url = entra_auth.get_auth_url(state=state)
    
    # Create response with state cookie
    response = RedirectResponse(url=auth_url)
    response.set_cookie("auth_state", state, httponly=True, max_age=600)
    
    return response


@router.get("/callback")
async def callback(request: Request, code: str = None, state: str = None, error: str = None):
    """Handle OAuth callback from Entra ID."""
    if error:
        response = render_auth_message("Login Failed", error, "Try Again")
        response.status_code = 400
        return response
    
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code received")
    
    # Verify state (CSRF protection)
    stored_state = request.cookies.get("auth_state")
    if state != stored_state:
        raise HTTPException(status_code=400, detail="Invalid state parameter")
    
    try:
        # Exchange code for tokens
        token_result = entra_auth.acquire_token_by_code(code)
        
        # Get user info
        access_token = token_result.get("access_token")
        user_info = entra_auth.get_user_info(access_token)
        
        # Create session token
        session_token = create_session_token(user_info, access_token)
        
        # Redirect to dashboard with session cookie
        response = RedirectResponse(url="/")
        response.set_cookie(
            "session_token",
            session_token,
            httponly=True,
            max_age=28800,  # 8 hours
            samesite="lax"
        )
        response.delete_cookie("auth_state")
        
        return response
        
    except Exception as e:
        response = render_auth_message("Authentication Error", str(e), "Try Again")
        response.status_code = 500
        return response


@router.get("/logout")
async def logout(request: Request):
    """Log out user."""
    response = RedirectResponse(url="/auth/login")
    response.delete_cookie("session_token")
    
    # If Entra ID is configured, redirect to Microsoft logout
    if entra_auth.is_configured:
        # Microsoft logout URL
        logout_url = f"https://login.microsoftonline.com/{entra_auth.tenant_id}/oauth2/v2.0/logout"
        logout_url += f"?post_logout_redirect_uri={entra_auth.redirect_uri.rsplit('/callback', 1)[0]}/auth/login"
        response = RedirectResponse(url=logout_url)
        response.delete_cookie("session_token")
    
    return response


@router.get("/me")
async def get_me(request: Request):
    """Get current user info."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@router.get("/status")
async def auth_status():
    """Get authentication configuration status."""
    return {
        "configured": entra_auth.is_configured,
        "auth_method": entra_auth.auth_method,
        "client_id": entra_auth.client_id[:8] + "..." if entra_auth.client_id else None,
        "tenant_id": entra_auth.tenant_id[:8] + "..." if entra_auth.tenant_id else None,
        "cert_path": entra_auth.cert_path if entra_auth.auth_method == "certificate" else None,
    }
