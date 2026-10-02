from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.core.config import get_settings
from app.core.security import create_session_token, hash_password, new_csrf_token, verify_password
from app.db.models import CandidateProfile, User
from app.db.session import get_db
from app.schemas import LoginRequest, RegisterRequest, UserResponse
from app.services.audit import record_audit

router = APIRouter(prefix="/auth", tags=["auth"])


def set_auth_cookies(response: Response, user_id: str) -> None:
    settings = get_settings()
    csrf_token = new_csrf_token()
    response.set_cookie(
        "session",
        create_session_token(user_id, settings),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_minutes * 60,
        path="/",
    )
    response.set_cookie(
        "csrf_token",
        csrf_token,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_minutes * 60,
        path="/",
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest, response: Response, request: Request, db: Session = Depends(get_db)
):
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    db.flush()
    db.add(CandidateProfile(user_id=user.id))
    record_audit(
        db,
        action="user.registered",
        actor="user",
        user_id=user.id,
        request_id=request.state.request_id,
    )
    db.commit()
    set_auth_cookies(response, user.id)
    return user


@router.post("/login", response_model=UserResponse)
def login(
    payload: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)
):
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    record_audit(
        db,
        action="user.logged_in",
        actor="user",
        user_id=user.id,
        request_id=request.state.request_id,
    )
    db.commit()
    set_auth_cookies(response, user.id)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    record_audit(
        db,
        action="user.logged_out",
        actor="user",
        user_id=user.id,
        request_id=request.state.request_id,
    )
    db.commit()
    response.delete_cookie("session", path="/")
    response.delete_cookie("csrf_token", path="/")


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)):
    return user
