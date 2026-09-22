"""Authentication API: account creation, login/logout, password change.

Every endpoint here except ``/status``, ``/setup`` and ``/login`` requires an
authenticated session (enforced by the middleware in main.py; ``PUT
/password`` additionally reads the cookie through its own dependency so it
can re-issue it). The auth-exempt endpoints are self-guarding:

* ``POST /setup`` refuses with 409 as soon as a password exists - which is
  what makes first-run account creation impossible to skip or replay.
* ``GET /status`` reveals exactly two booleans and nothing else, so an
  unauthenticated visitor learns only whether to show "create" vs "login".

Passwords are never logged; failures return generic messages (the one
exception is the lockout notice, which intentionally tells the caller to
wait - standard practice and harmless for a single-account app).
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool

from homestew.models.schemas import (
    AuthStatusResponse,
    PasswordChangeRequest,
    PasswordLoginRequest,
    PasswordSetupRequest,
)
from homestew.services import auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    """Direct TCP peer address used as the lockout key.

    Deliberately NOT X-Forwarded-For: HomeStew is reached directly (no
    reverse proxy in this deployment), and trusting a client-supplied header
    would let an attacker rotate it to evade the per-IP lockout entirely.
    """
    return request.client.host if request.client else "unknown"


def _issue_session(response: Response, remember_me: bool) -> None:
    """Set the signed session cookie on ``response``.

    Remember-me: persistent 30-day cookie. Otherwise the cookie carries no
    max-age (the browser drops it on close) while the token itself still
    expires after 12 h, so neither a closed browser nor a stolen cookie
    file grants indefinite access.
    """
    if remember_me:
        token = auth.create_session_token(auth.SESSION_MAX_AGE_REMEMBER)
        max_age: Optional[int] = auth.SESSION_MAX_AGE_REMEMBER
    else:
        token = auth.create_session_token(auth.SESSION_MAX_AGE_SESSION)
        max_age = None  # browser-session cookie
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        # Plain http:// on the LAN is the expected deployment; Secure would
        # break it. SameSite=Lax still blocks cross-site POSTs from sending
        # the cookie.
        secure=False,
        path="/",
    )


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(request: Request):
    """Two booleans - the only pre-login information the app reveals."""
    return AuthStatusResponse(
        password_configured=auth.password_configured(),
        authenticated=auth.verify_session_token(request.cookies.get(auth.SESSION_COOKIE)),
    )


@router.post("/setup", response_model=AuthStatusResponse, status_code=status.HTTP_201_CREATED)
async def create_account(body: PasswordSetupRequest, response: Response):
    """Create the single account (first run only) and sign the caller in.

    Auth-exempt by necessity - there is no password to check yet - but it
    permanently locks itself out with a 409 once one exists, so the forced
    create-account screen cannot be bypassed or replayed.
    """
    if auth.password_configured():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account already exists. Use the CLI reset if the "
            "password was forgotten (docker exec -it homestew python -m "
            "homestew.auth_cli reset-password).",
        )
    if body.password != body.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The passwords do not match.",
        )
    await run_in_threadpool(auth.set_password, body.password)
    _issue_session(response, remember_me=True)  # first login: stay signed in
    logger.info("Account created via web setup.")
    return AuthStatusResponse(password_configured=True, authenticated=True)


@router.post("/login", response_model=AuthStatusResponse)
async def login(body: PasswordLoginRequest, request: Request, response: Response):
    """Verify the password and issue a session cookie."""
    ip = _client_ip(request)
    remaining = auth.lockout_remaining(ip)
    if remaining:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Too many failed attempts. Try again in {remaining} s.",
            headers={"Retry-After": str(remaining)},
        )
    ok = await run_in_threadpool(
        auth.verify_password, body.password, auth.stored_password_hash()
    )
    if not ok:
        # Progressive delay below the lockout threshold (see services/auth).
        await asyncio.sleep(auth.failure_delay(ip))
        auth.record_failure(ip)
        logger.warning("Failed login attempt from %s.", ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password.",
        )
    auth.reset_failures(ip)
    _issue_session(response, remember_me=body.remember_me)
    return AuthStatusResponse(password_configured=True, authenticated=True)


@router.post("/logout", response_model=AuthStatusResponse)
async def logout(request: Request, response: Response):
    """Clear the session cookie (no server state to revoke - it is signed)."""
    response.delete_cookie(key=auth.SESSION_COOKIE, path="/")
    return AuthStatusResponse(
        password_configured=auth.password_configured(), authenticated=False
    )


@router.put("/password", response_model=AuthStatusResponse)
async def change_password(body: PasswordChangeRequest, request: Request, response: Response):
    """Change the password from Settings > Advanced (requires current one).

    Re-hashing rotates the session signing key, so every browser - this one
    included - is logged out; the caller's fresh cookie is re-issued here so
    saving a new password does not kick the user to the login screen.
    """
    ok = await run_in_threadpool(
        auth.verify_password, body.current_password, auth.stored_password_hash()
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The current password is incorrect.",
        )
    if body.new_password != body.confirm_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The new passwords do not match.",
        )
    await run_in_threadpool(auth.set_password, body.new_password)
    _issue_session(response, remember_me=True)  # keep this browser signed in
    logger.info("Account password changed via Settings.")
    return AuthStatusResponse(password_configured=True, authenticated=True)
