"""Onboarding tutorial dismissal (shown once per account, replayable from /profile)."""
from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from app.auth.deps import require_user
from app.db import get_session
from app.security import safe_next_url
from app.models import AppUser

router = APIRouter()


@router.post("/tutorial/dismiss")
def tutorial_dismiss(
    next: str = Form("/"),
    viewer: AppUser = Depends(require_user),
    session: Session = Depends(get_session),
):
    viewer.tutorial_seen = True
    session.add(viewer)
    session.commit()
    safe_next = safe_next_url(next, "/")
    return RedirectResponse(safe_next, status_code=303)
