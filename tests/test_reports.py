from datetime import datetime

from sqlmodel import select

from app.models import (
    AppUser,
    Plan,
    Renewal,
    RenewalStatus,
    Role,
    Subscription,
    SubscriptionStatus,
)
from app.services import reports as rep

REF = datetime(2026, 6, 15, 12, 0, 0)  # June 2026


def _plan(session, slug):
    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def _user(session, name, **kw):
    u = AppUser(role=Role.user, real_name=name, **kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _manager(session, name, role=Role.moderator):
    m = AppUser(role=role, real_name=name)
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def _sub(session, user, plan, expiry):
    s = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        expiry_at=expiry,
        status=SubscriptionStatus.active,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _paid_renewal(session, sub, plan, amount, paid_at):
    r = Renewal(
        subscription_id=sub.id,
        plan_id=plan.id,
        amount_cents=amount,
        status=RenewalStatus.paid,
        paid_at=paid_at,
    )
    session.add(r)
    session.commit()
    return r


def test_earnings_buckets(db_session):
    bronze = _plan(db_session, "bronze")
    gold = _plan(db_session, "gold")

    # previous month (May) paid renewal -> prev
    u1 = _user(db_session, "Prev")
    s1 = _sub(db_session, u1, bronze, datetime(2026, 5, 20))
    _paid_renewal(db_session, s1, bronze, 500, datetime(2026, 5, 10))

    # this month (June) paid renewal -> current_collected
    u2 = _user(db_session, "Collected")
    s2 = _sub(db_session, u2, gold, datetime(2027, 6, 5))
    _paid_renewal(db_session, s2, gold, 2500, datetime(2026, 6, 5))

    # expiring this month, no paid renewal this month -> collectable
    u3 = _user(db_session, "Due")
    _sub(db_session, u3, bronze, datetime(2026, 6, 25))

    # expiring next month (July), paid plan -> next_projected
    u4 = _user(db_session, "NextMonth")
    _sub(db_session, u4, gold, datetime(2026, 7, 10))

    e = rep.earnings(db_session, ref=REF)
    assert e["prev"] == 500
    assert e["current_collected"] == 2500
    assert e["current_collectable"] == 500
    assert e["next_projected"] == 5000


def test_collectable_excludes_already_paid_this_month(db_session):
    bronze = _plan(db_session, "bronze")
    u = _user(db_session, "PaidUp")
    s = _sub(db_session, u, bronze, datetime(2026, 6, 25))
    # already paid this month -> moves to collected, not collectable
    _paid_renewal(db_session, s, bronze, 500, datetime(2026, 6, 3))
    e = rep.earnings(db_session, ref=REF)
    assert e["current_collectable"] == 0
    assert e["current_collected"] == 500


def test_plan_counters(db_session):
    bronze = _plan(db_session, "bronze")
    for n in range(3):
        _sub(db_session, _user(db_session, f"B{n}"), bronze, datetime(2026, 9, 1))
    counters = {c["name"]: c["count"] for c in rep.plan_counters(db_session)}
    assert counters["Bronze"] == 3
    assert counters["Gold"] == 0


def test_upcoming_expiries(db_session):
    bronze = _plan(db_session, "bronze")
    u = _user(db_session, "Soon")
    _sub(db_session, u, bronze, datetime(2026, 6, 20))
    far = _user(db_session, "Far")
    _sub(db_session, far, bronze, datetime(2026, 9, 1))
    rows = rep.upcoming_expiries(db_session, days=30, ref=REF)
    names = [r["user"] for r in rows]
    assert "Soon" in names and "Far" not in names


def test_reports_route_permission(client, db_session, login_as):
    user = _user(db_session, "Plain")
    login_as(client, user.id)
    assert client.get("/reports", follow_redirects=False).status_code == 403


# ---- Manager scoping / history / trend -------------------------------------


def test_earnings_scoped_to_manager(db_session):
    bronze = _plan(db_session, "bronze")
    mgr = _manager(db_session, "Boss")
    mine = _user(db_session, "Mine", manager_id=mgr.id)
    other = _user(db_session, "Other")
    _paid_renewal(
        db_session, _sub(db_session, mine, bronze, datetime(2026, 6, 20)),
        bronze, 500, datetime(2026, 6, 5),
    )
    _paid_renewal(
        db_session, _sub(db_session, other, bronze, datetime(2026, 6, 20)),
        bronze, 900, datetime(2026, 6, 5),
    )

    assert rep.earnings(db_session, REF)["current_collected"] == 1400
    assert rep.earnings(db_session, REF, mgr.id)["current_collected"] == 500
    # UNASSIGNED is a real filter, not "no filter": `other` has no manager.
    unassigned = rep.earnings(db_session, REF, rep.UNASSIGNED)
    assert unassigned["current_collected"] == 900

    # A manager with no users must match nothing, not everything.
    empty = _manager(db_session, "Newbie")
    assert rep.earnings(db_session, REF, empty.id)["current_collected"] == 0


def test_earnings_renewal_rate_and_arpu(db_session):
    bronze = _plan(db_session, "bronze")
    paid_up = _user(db_session, "PaidUp")
    s1 = _sub(db_session, paid_up, bronze, datetime(2026, 6, 10))
    _paid_renewal(db_session, s1, bronze, 500, datetime(2026, 6, 2))
    _sub(db_session, _user(db_session, "Due"), bronze, datetime(2026, 6, 28))

    e = rep.earnings(db_session, REF)
    assert e["renewal_rate"] == 50.0  # 1 of 2 subs due this month renewed
    assert e["paying_users"] == 2
    assert e["arpu_cents"] == 250

    # Nothing due -> None, not 0%: "0% of nothing" would read as a red flag.
    assert rep.earnings(db_session, datetime(2026, 1, 15))["renewal_rate"] is None


def test_monthly_series_buckets_by_month(db_session):
    bronze = _plan(db_session, "bronze")
    sub = _sub(db_session, _user(db_session, "U"), bronze, datetime(2026, 6, 20))
    _paid_renewal(db_session, sub, bronze, 500, datetime(2026, 4, 3))
    _paid_renewal(db_session, sub, bronze, 700, datetime(2026, 6, 3))
    _paid_renewal(db_session, sub, bronze, 300, datetime(2026, 6, 20))
    # Outside the 12-month window ending on June 2026.
    _paid_renewal(db_session, sub, bronze, 999, datetime(2025, 1, 5))

    series = rep.monthly_series(db_session, 12, REF)
    assert len(series) == 12
    assert series[0]["month"] == "2025-07" and series[-1]["month"] == "2026-06"
    by_month = {s["month"]: s["collected_cents"] for s in series}
    assert by_month["2026-04"] == 500
    assert by_month["2026-06"] == 1000
    assert by_month["2026-05"] == 0  # gaps stay in, the chart needs a flat line
    assert sum(by_month.values()) == 1500  # the 2025-01 row is out of range


def test_manager_totals_cover_unassigned(db_session):
    bronze = _plan(db_session, "bronze")
    mgr = _manager(db_session, "Boss")
    mine = _user(db_session, "Mine", manager_id=mgr.id)
    orphan = _user(db_session, "Orphan")
    _paid_renewal(
        db_session, _sub(db_session, mine, bronze, datetime(2026, 6, 20)),
        bronze, 500, datetime(2026, 6, 5),
    )
    _paid_renewal(
        db_session, _sub(db_session, orphan, bronze, datetime(2026, 6, 20)),
        bronze, 900, datetime(2026, 6, 5),
    )

    rows = {r["name"]: r for r in rep.manager_totals(db_session, REF)}
    assert rows["Boss"]["current_collected"] == 500
    assert rows[None]["current_collected"] == 900  # None -> "Unassigned" row
    # The per-manager rows must reconcile with the headline card.
    total = rep.earnings(db_session, REF)["current_collected"]
    assert sum(r["current_collected"] for r in rows.values()) == total


def test_paid_renewals_window_and_manager_filter(db_session):
    bronze = _plan(db_session, "bronze")
    mgr = _manager(db_session, "Boss")
    mine = _user(db_session, "Mine", manager_id=mgr.id)
    other = _user(db_session, "Other")
    s_mine = _sub(db_session, mine, bronze, datetime(2026, 6, 20))
    _paid_renewal(db_session, s_mine, bronze, 500, datetime(2026, 6, 5))
    _paid_renewal(db_session, s_mine, bronze, 100, datetime(2026, 5, 5))
    _paid_renewal(
        db_session, _sub(db_session, other, bronze, datetime(2026, 6, 20)),
        bronze, 900, datetime(2026, 6, 7),
    )

    assert len(rep.paid_renewals(db_session)) == 3  # no window = everything

    june = rep.paid_renewals(db_session, datetime(2026, 6, 1), datetime(2026, 7, 1))
    assert {r["amount_cents"] for r in june} == {500, 900}

    scoped = rep.paid_renewals(
        db_session, datetime(2026, 6, 1), datetime(2026, 7, 1), mgr.id
    )
    assert [r["amount_cents"] for r in scoped] == [500]
    assert scoped[0]["manager"] == "Boss"


def test_parse_manager_keeps_unassigned_sentinel():
    from app.routers.reports import _parse_manager, _parse_month

    assert _parse_manager("0") == 0  # falsy but a real filter
    assert _parse_manager("7") == 7
    assert _parse_manager("") is None and _parse_manager("-1") is None
    assert _parse_month("2026-06") == datetime(2026, 6, 1)
    assert _parse_month("nope") is None and _parse_month("") is None


def test_reports_page_filters_by_month_and_manager(client, db_session, login_as):
    bronze = _plan(db_session, "bronze")
    admin = _manager(db_session, "Chief", role=Role.admin)
    mgr = _manager(db_session, "Boss")
    mine = _user(db_session, "Mine", manager_id=mgr.id)
    other = _user(db_session, "Other")
    _paid_renewal(
        db_session, _sub(db_session, mine, bronze, datetime(2026, 6, 20)),
        bronze, 500, datetime(2026, 6, 5),
    )
    _paid_renewal(
        db_session, _sub(db_session, other, bronze, datetime(2026, 6, 20)),
        bronze, 900, datetime(2026, 6, 7),
    )
    login_as(client, admin.id)

    both = client.get("/reports?month=2026-06")
    assert both.status_code == 200
    assert "Mine" in both.text and "Other" in both.text
    # "Other" has no manager -> that row is the superadmin's own book.
    assert "Superadmin" in both.text

    scoped = client.get(f"/reports?month=2026-06&manager={mgr.id}")
    assert scoped.status_code == 200
    # The movements table is month+manager scoped; "Other" only appears there.
    assert "Other" not in scoped.text

    # An empty month falls back to today rather than 500-ing.
    assert client.get("/reports?month=garbage").status_code == 200


def test_csv_export_honours_filters(client, db_session, login_as):
    bronze = _plan(db_session, "bronze")
    admin = _manager(db_session, "Chief", role=Role.admin)
    mgr = _manager(db_session, "Boss")
    mine = _user(db_session, "Mine", manager_id=mgr.id)
    other = _user(db_session, "Other")
    s_mine = _sub(db_session, mine, bronze, datetime(2026, 6, 20))
    _paid_renewal(db_session, s_mine, bronze, 500, datetime(2026, 6, 5))
    _paid_renewal(db_session, s_mine, bronze, 100, datetime(2026, 5, 5))
    _paid_renewal(
        db_session, _sub(db_session, other, bronze, datetime(2026, 6, 20)),
        bronze, 900, datetime(2026, 6, 7),
    )
    login_as(client, admin.id)

    # No filter keeps the old behaviour: the whole ledger.
    all_rows = client.get("/reports/export.csv").text.strip().splitlines()
    assert len(all_rows) == 4  # header + 3

    filtered = client.get(f"/reports/export.csv?month=2026-06&manager={mgr.id}")
    rows = filtered.text.strip().splitlines()
    assert len(rows) == 2 and "5.00" in rows[1] and "Boss" in rows[1]
    assert "incassi-2026-06.csv" in filtered.headers["content-disposition"]

    admin = AppUser(role=Role.admin, real_name="RepAdmin")
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    login_as(client, admin.id)
    assert client.get("/reports").status_code == 200


def test_paid_renewals_list(db_session):
    bronze = _plan(db_session, "bronze")
    u = _user(db_session, "Payer")
    s = _sub(db_session, u, bronze, datetime(2026, 6, 25))
    _paid_renewal(db_session, s, bronze, 500, datetime(2026, 6, 5))
    rows = rep.paid_renewals(db_session)
    assert len(rows) == 1
    assert rows[0]["user"] == "Payer"
    assert rows[0]["plan"] == "Bronze"
    assert rows[0]["amount_cents"] == 500


def test_reports_export_csv(client, db_session, login_as):
    admin = AppUser(role=Role.admin, real_name="RepAdmin2")
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    bronze = _plan(db_session, "bronze")
    u = _user(db_session, "CsvUser")
    s = _sub(db_session, u, bronze, datetime(2026, 6, 25))
    _paid_renewal(db_session, s, bronze, 500, datetime(2026, 6, 5))
    login_as(client, admin.id)
    resp = client.get("/reports/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "CsvUser" in resp.text
    assert "5.00" in resp.text


def test_reports_export_csv_requires_permission(client, db_session, login_as):
    user = _user(db_session, "PlainCsv")
    login_as(client, user.id)
    assert client.get("/reports/export.csv", follow_redirects=False).status_code == 403
