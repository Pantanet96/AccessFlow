"""Reports dashboard (Admin + SuperAdmin)."""
import csv
import io
from datetime import datetime

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlmodel import Session

from app import runtime_config
from app.auth.deps import require_capability
from app.db import get_session
from app.i18n import gettext as _
from app.models import AppUser, utcnow
from app.permissions import Capability
from app.services import reports as reports_svc
from app.services import users as users_svc
from app.templating import templates

router = APIRouter()

TREND_MONTHS = 12


def _parse_month(raw: str) -> datetime | None:
    """"YYYY-MM" from <input type="month">. Anything else -> None = current."""
    try:
        return datetime.strptime(raw, "%Y-%m")
    except (ValueError, TypeError):
        return None


def _parse_manager(raw: str) -> int | None:
    # isdigit(), not int(): "0" is the real UNASSIGNED sentinel and must not be
    # swallowed by a falsy check, while "-1"/"abc" mean "no filter".
    return int(raw) if raw.isdigit() else None


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    month: str = "",
    manager: str = "",
    viewer: AppUser = Depends(require_capability(Capability.view_reports)),
    session: Session = Depends(get_session),
):
    ref = _parse_month(month) or utcnow()
    mid = _parse_manager(manager)
    m_start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    m_end = m_start + relativedelta(months=1)

    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "current_user": viewer,
            "month": m_start.strftime("%Y-%m"),
            "manager": manager if mid is not None else "",
            "managers": users_svc.manager_candidates(session),
            "unassigned_id": reports_svc.UNASSIGNED,
            "query": request.url.query,
            "earnings": reports_svc.earnings(session, ref, mid),
            "counters": reports_svc.plan_counters(session, mid),
            "series": reports_svc.monthly_series(session, TREND_MONTHS, ref, mid),
            "manager_totals": reports_svc.manager_totals(session, ref),
            "movements": reports_svc.paid_renewals(session, m_start, m_end, mid),
            # Expiries are always "from now", whatever month is being read.
            "upcoming": reports_svc.upcoming_expiries(session, manager_id=mid),
        },
    )


@router.get("/reports/export.csv")
def reports_export_csv(
    month: str = "",
    manager: str = "",
    viewer: AppUser = Depends(require_capability(Capability.view_reports)),
    session: Session = Depends(get_session),
):
    ref = _parse_month(month)
    mid = _parse_manager(manager)
    start = end = None
    if ref is not None:
        start = ref
        end = ref + relativedelta(months=1)

    buf = io.StringIO()
    writer = csv.writer(buf)
    code = runtime_config.currency()["code"]
    amount_hdr = _("Amount") + (f" ({code})" if code else "")
    writer.writerow([
        _("Payment date"), _("User"), _("Plan"), amount_hdr,
        _("Reference"), _("Collected by"), _("Manager"),
    ])
    for r in reports_svc.paid_renewals(session, start, end, mid):
        writer.writerow([
            r["paid_at"].strftime("%Y-%m-%d") if r["paid_at"] else "",
            r["user"], r["plan"], f"{r['amount_cents'] / 100:.2f}",
            # No manager == the superadmin's own book; same label as the page.
            r["causale"], r["collected_by"], r["manager"] or _("Superadmin"),
        ])
    name = f"incassi-{ref:%Y-%m}.csv" if ref else "incassi.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )
