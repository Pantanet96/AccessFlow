from app.services import notification_templates as nt
from app.services import settings_store

CTX = dict(nt.SAMPLE_CTX)


def test_render_email_it_and_en(db_session):
    subj, html, text = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert "Bronze" in subj and "scade" in subj.lower()
    assert "<p>" in html and "Bronze" in html
    assert "<" not in text and "Bronze" in text  # tags stripped for the plain part

    en_subj, _, _ = nt.render_email(db_session, "user_expiry", "en", CTX)
    assert "expires" in en_subj.lower()


def test_telegram_markdownv2_escapes_dynamic_values(db_session):
    body = nt.render_telegram(db_session, "user_expiry", "it", dict(CTX, plan_name="Plan_1"))
    assert "Plan\\_1" in body           # underscore escaped (would break MarkdownV2)
    assert "*Plan\\_1*" in body          # static bold markup preserved around it


def test_override_then_reset(db_session):
    key = "ntpl:user_expiry:telegram:it"
    settings_store.set_value(db_session, key, "CUSTOM {{ name|tg }}")
    assert nt.render_telegram(db_session, "user_expiry", "it", CTX).startswith("CUSTOM")
    settings_store.delete_value(db_session, key)
    assert "CUSTOM" not in nt.render_telegram(db_session, "user_expiry", "it", CTX)


def test_validate_catches_syntax_error():
    assert nt.validate("Hello {{ name }}") is None
    assert nt.validate("Broken {% if %}") is not None


def test_render_manager_digest_lists_items(db_session):
    subj, html, text = nt.render_email(db_session, "manager_digest", "it", CTX)
    assert "Mario Rossi" in html and "Lucia Bianchi" in html  # iterates items
    assert "55.00" in html                                    # total
    tg = nt.render_telegram(db_session, "manager_digest", "en", CTX)
    assert "Mario Rossi" in tg and "Lucia Bianchi" in tg


def test_locale_fallback_never_empty(db_session):
    # Unknown locale -> falls back to default_locale / en, not empty.
    assert nt.render_telegram(db_session, "welcome", "fr", CTX)


def test_plain_text_keeps_list_items_apart(db_session):
    """The invite mail carries its instructions as <ol><li> steps. _html_to_text
    only handled <br> and </p>, so every step ran into the next one -- the plain
    part of the multipart mail read as a single unbroken line."""
    _, html, text = nt.render_email(db_session, "invite", "it", CTX)
    assert html.count("<li>") >= 2, "template should still use list items"
    for line in text.splitlines():
        assert line.count("- ") <= 1, f"steps merged onto one line: {line!r}"
    # Each step is its own line, bulleted.
    assert sum(line.startswith("- ") for line in text.splitlines()) == html.count("<li>")
