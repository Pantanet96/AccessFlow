def test_italian_translation_renders(client):
    resp = client.get("/?locale=it")
    assert resp.status_code == 200
    # "Sign in" -> "Accedi" when the Italian catalog is compiled.
    assert "Accedi" in resp.text


def test_english_default(client):
    resp = client.get("/?locale=en")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


def test_dynamic_labels_are_in_the_italian_catalog():
    """Labels stored first and translated later -- audit actions, notification
    types, the enum values templates render as `_(value|capitalize)` -- are
    invisible to `pybabel extract` unless the literal is marked with N_ at its
    definition. Without that they silently fall back to English forever, which
    is exactly what this asserts against."""
    from app.i18n import STATUS_LABELS, _load
    from app.services.audit import _ACTION_LABELS, _NTYPE_LABELS

    it = _load("it")
    missing = [
        source
        for source in (
            *_ACTION_LABELS.values(),
            *_NTYPE_LABELS.values(),
            *STATUS_LABELS,
        )
        if it.gettext(source) == source
    ]
    assert not missing, f"untranslated in it: {missing}"


def test_audit_action_renders_in_italian(client, db_session, login_as):
    from sqlmodel import select

    from app.models import AppUser, Role
    from app.services import audit

    admin = db_session.exec(
        select(AppUser).where(AppUser.role == Role.superadmin)
    ).one()
    audit.record(db_session, admin.id, "create_invite", "invite", 1, {"email": "a@b.it"})
    login_as(client, admin.id)
    resp = client.get("/audit?locale=it")
    assert resp.status_code == 200
    assert "ha creato un invito" in resp.text
    assert "created an invite" not in resp.text
