"""Support access (candidate grants; support reads redacted summaries) and pilot metrics."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, require_csrf
from app.db.models import SupportAccessGrant, User
from app.db.session import get_db
from app.schemas import StrictModel
from app.services.metrics import pilot_metrics
from app.services.support import (
    MAX_GRANT_HOURS,
    SupportAccessError,
    case_summary,
    grant_access,
    grant_state,
    grants_for_candidate,
    mask_email,
    open_cases,
    revoke_access,
)

router = APIRouter(tags=["support"])


class GrantCreate(StrictModel):
    support_email: str = Field(min_length=3, max_length=320)
    hours: int = Field(ge=1, le=MAX_GRANT_HOURS)
    reason: str = Field(min_length=1, max_length=500)


class GrantResponse(StrictModel):
    id: str
    support_email: str
    scope: str
    reason: str
    state: str
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_viewed_at: datetime | None
    view_count: int


class CaseResponse(StrictModel):
    grant_id: str
    candidate: str
    reason: str
    expires_at: datetime


def _grant_response(db: Session, grant: SupportAccessGrant) -> GrantResponse:
    support = db.get(User, grant.support_user_id)
    return GrantResponse(
        id=grant.id,
        support_email=support.email if support else "deleted account",
        scope=grant.scope,
        reason=grant.reason,
        state=grant_state(grant),
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        last_viewed_at=grant.last_viewed_at,
        view_count=grant.view_count,
    )


# --- candidate ---------------------------------------------------------------------------


@router.get("/support/grants", response_model=list[GrantResponse])
def list_grants(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [_grant_response(db, grant) for grant in grants_for_candidate(db, user.id)]


@router.post("/support/grants", response_model=GrantResponse, status_code=201)
def create_grant(
    payload: GrantCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        grant = grant_access(
            db,
            user=user,
            support_email=payload.support_email,
            hours=payload.hours,
            reason=payload.reason,
            request_id=request.state.request_id,
        )
    except SupportAccessError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _grant_response(db, grant)


@router.post("/support/grants/{grant_id}/revoke", response_model=GrantResponse)
def revoke_grant(
    grant_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_csrf(request)
    try:
        grant = revoke_access(db, user=user, grant_id=grant_id, request_id=request.state.request_id)
    except SupportAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return _grant_response(db, grant)


# --- support -----------------------------------------------------------------------------


def _require_role(user: User, role: str) -> None:
    if user.role != role:
        raise HTTPException(status_code=403, detail="Not permitted")


@router.get("/support/cases", response_model=list[CaseResponse])
def list_cases(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_role(user, "support")
    cases = []
    for grant in open_cases(db, user):
        candidate = db.get(User, grant.user_id)
        cases.append(
            CaseResponse(
                grant_id=grant.id,
                candidate=mask_email(candidate.email) if candidate else "deleted account",
                reason=grant.reason,
                expires_at=grant.expires_at,
            )
        )
    return cases


@router.get("/support/cases/{grant_id}")
def view_case(
    grant_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_role(user, "support")
    try:
        summary = case_summary(
            db, support_user=user, grant_id=grant_id, request_id=request.state.request_id
        )
    except SupportAccessError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return summary


# --- operators ---------------------------------------------------------------------------


@router.get("/ops/metrics")
def get_metrics(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    _require_role(user, "operator")
    return pilot_metrics(db)
