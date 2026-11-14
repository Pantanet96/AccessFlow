"""Public unsubscribe endpoint (RFC 8058 one-click + a page for humans).

No login and no CSRF check on purpose -- see the exemption in main.py. The
token in the path is the only authority these routes use: they never read the
session cookie, so there is no ambient credential for a forged request to ride.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.db import get_session
from app.services import audit, unsubscribe
from app.templating import templates

router = APIRouter()


def _page(request: Request, state: str, token: str = "", status_code: int = 200):
    response = templates.TemplateResponse(
        request, "unsubscribe.html", {"state": state, "token": token},
        status_code=status_code,
    )
    # The URL is a capability and the page reflects live state: neither
    # belongs in a shared or a browser cache.
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_confirm(
    token: str, request: Request, session: Session = Depends(get_session)
):
    """Shows the confirmation button and changes nothing.

    A GET must stay inert: mail scanners and link previewers fetch every URL
    in a message, and an unsubscribe that fired on GET would switch reminders
    off for people who never clicked anything."""
    user = unsubscribe.read_token(session, token)
    if user is None:
        return _page(request, "invalid", status_code=404)
    return _page(request, "confirm" if user.notify_via_email else "done", token)


@router.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_apply(
    token: str, request: Request, session: Session = Depends(get_session)
):
    """The one-click POST a mailbox provider sends, and the page's own button."""
    user = unsubscribe.read_token(session, token)
    if user is None:
        return _page(request, "invalid", status_code=404)
    if unsubscribe.apply(session, user):
        # The user is the actor: the link was in their own mailbox.
        audit.record(session, user.id, "unsubscribe_email", "user", user.id)
    return _page(request, "done", token)
