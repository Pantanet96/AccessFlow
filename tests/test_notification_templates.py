import re

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


def test_telegram_defaults_escape_reserved_chars():
    # MarkdownV2 rejects the whole message (400) on any bare reserved char in
    # the static text; *bold* / _italic_ markup is the only intended use.
    bare = re.compile(r"(?<!\\)([\[\]()~`>#+=|{}.!-])")
    for (type_, part), by_locale in nt.DEFAULTS.items():
        if part != "telegram":
            continue
        for loc, src in by_locale.items():
            static = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", src, flags=re.S)
            assert not bare.findall(static), (type_, loc, static)


def test_validate_rejects_variables_the_type_does_not_get():
    # Valid against the union of every type's variables, it crashed at send
    # time (digest has no `days`) and aborted the run for every manager.
    src = "{% if days > 1 %}soon{% endif %}"
    assert nt.validate(src, "manager_digest") is not None
    assert nt.validate(src, "user_expiry") is None
    assert nt.validate(src, "user_overdue") is None  # same runtime context


def test_type_variables_all_have_samples():
    for type_, variables in nt.TYPES.items():
        assert set(variables) <= set(nt.SAMPLE_CTX), type_


def test_defaults_validate_against_their_own_type():
    for (type_, part), by_locale in nt.DEFAULTS.items():
        for loc, src in by_locale.items():
            assert nt.validate(src, type_) is None, (type_, part, loc)


# ---- preheader (the inbox preview line) ----

def _preheader_of(html):
    """The text inside the hidden block render_email inserts."""
    m = re.search(r'<div style="display:none[^"]*">(.*?)</div>', html, re.S)
    return m.group(1) if m else ""


def test_preheader_is_hidden_and_skips_the_greeting(db_session):
    _, html, _ = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert "mso-hide:all" in html  # the Outlook half of the hide
    # First thing inside <body>, so it is what the client previews.
    assert html.index("display:none") < html.index("Ciao Mario Rossi,")
    pre = _preheader_of(html)
    # The greeting is the one line carrying no information, and it is exactly
    # what a client previews when there is no preheader.
    assert "Ciao Mario Rossi," not in pre
    # The opening line carries the plan and the deadline on purpose: it is what
    # the message list shows next to the subject.
    assert "Bronze" in pre and "3 giorni" in pre


def test_preheader_stays_out_of_the_plain_part(db_session):
    # It repeats a line already in the body: a plain part opening by saying the
    # same thing twice reads as broken.
    _, html, text = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert text.startswith("Ciao Mario Rossi,")
    assert "display:none" not in text and "zwnj" not in text


def test_preheader_escapes_the_copy(db_session):
    # _html_to_text unescapes, so the line goes back to HTML and must be
    # re-escaped -- otherwise admin copy could reopen tags before the body.
    _, html, _ = nt.render_email(db_session, "user_expiry", "it",
                                 dict(CTX, plan_name='<b>bold'))
    pre = _preheader_of(html)
    assert "<b>" not in pre and "&lt;b&gt;" in pre


def test_preheader_clips_a_long_line(db_session):
    settings_store.set_value(db_session, "ntpl:welcome:email_html:it",
                             "<p>" + ("parola " * 60) + "</p>")
    _, html, _ = nt.render_email(db_session, "welcome", "it", CTX)
    pre = _preheader_of(html).replace("&#847;&zwnj;&nbsp;", "")  # drop the padding
    assert pre.endswith("…")
    assert len(pre) <= nt._PREHEADER_MAX + 1
    settings_store.delete_value(db_session, "ntpl:welcome:email_html:it")


def test_preheader_skips_the_greeting_whatever_the_name_length(db_session):
    # Keyed on the line's position, not its length: a length cutoff made the
    # greeting itself the preview for anyone with a long name, and the padding
    # after it then hid the line a client would otherwise have shown.
    long_name = "Giovanni Battista Della Rovere Spinoza Montecuccoli"
    _, html, _ = nt.render_email(db_session, "user_expiry", "it",
                                 dict(CTX, name=long_name))
    pre = _preheader_of(html)
    assert long_name not in pre
    assert "Bronze" in pre


def test_preheader_goes_inside_body_of_a_full_document(db_session):
    # An admin may paste a whole document: in front of <!DOCTYPE> the block
    # would force quirks mode and sit outside <body>, where webmail strips it.
    settings_store.set_value(
        db_session, "ntpl:welcome:email_html:it",
        "<!DOCTYPE html><html><body><p>Ciao {{ name|e }},</p>"
        "<p>Benvenuto in {{ plan_name|e }}.</p></body></html>",
    )
    _, html, _ = nt.render_email(db_session, "welcome", "it", CTX)
    assert html.startswith("<!DOCTYPE html>")
    assert html.index("display:none") > html.index("<body>")
    assert "Benvenuto in Bronze" in _preheader_of(html)
    settings_store.delete_value(db_session, "ntpl:welcome:email_html:it")


def test_a_pasted_document_does_not_leak_its_stylesheet(db_session):
    # A template exported from a mail builder carries a <head> with a <style>.
    # Stripping only the tags left the stylesheet as the first readable line:
    # the plain-text part, and with it the inbox preview, opened with CSS.
    settings_store.set_value(
        db_session, "ntpl:welcome:email_html:it",
        "<!DOCTYPE html><html><head><title>Promemoria</title>"
        "<style>body{margin:0;background:#f4f4f4}</style></head>"
        "<body><p>Ciao {{ name|e }},</p>"
        "<p>Benvenuto in {{ plan_name|e }}.</p></body></html>",
    )
    _, html, text = nt.render_email(db_session, "welcome", "it", CTX)
    assert "margin:0" not in text and "Promemoria" not in text
    assert text.startswith("Ciao Mario Rossi,")
    assert "Benvenuto in Bronze" in _preheader_of(html)
    assert "margin:0" in html, "the stylesheet still belongs in the HTML part"
    settings_store.delete_value(db_session, "ntpl:welcome:email_html:it")


def test_subject_is_folded_to_one_line(db_session):
    # The subject is edited in a textarea and EmailMessage raises on CR/LF in a
    # header: one stray Enter would fail every send of that type forever,
    # logged as an error with nothing in Settings to point at the cause.
    settings_store.set_value(db_session, "ntpl:welcome:email_subject:it",
                             "Benvenuto\nin {{ plan_name }}")
    subject, _, _ = nt.render_email(db_session, "welcome", "it", CTX)
    assert subject == "Benvenuto in Bronze"
    settings_store.delete_value(db_session, "ntpl:welcome:email_subject:it")


# ---- layout shell ----

def test_the_body_goes_inside_the_shell(db_session):
    _, html, _ = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert html.startswith("<!DOCTYPE html PUBLIC")
    assert "af-card" in html and "af-body" in html
    assert "<p>Ciao Mario Rossi,</p>" in html  # the fragment, markup intact
    assert 'lang="it"' in html
    assert html.count("<html") == 1


def test_the_width_survives_a_stripped_style_block(db_session):
    # Gmail drops <style> when it forwards a message, and in the app with a
    # non-Gmail account. The layout has to fit a phone without it, so the width
    # lives on the elements -- a max-width div plus an Outlook ghost table --
    # and not in a media query that may never arrive.
    _, html, _ = nt.render_email(db_session, "user_expiry", "it", CTX)
    stripped = re.sub(r"(?is)<style.*?</style>", "", html)
    assert "max-width:600px" in stripped
    # No FIXED width outside the MSO block (max-width:600px is the good one).
    assert not re.search(r"(?<!max-)width:600px", stripped)
    assert "[if mso]" in html  # Outlook still gets its fixed-width table


def test_a_full_document_is_left_alone(db_session):
    # Nesting the admin's own document inside ours would break both.
    settings_store.set_value(
        db_session, "ntpl:welcome:email_html:it",
        "<html><body><p>Ciao {{ name|e }},</p><p>Il mio layout.</p></body></html>")
    _, html, _ = nt.render_email(db_session, "welcome", "it", CTX)
    assert "af-card" not in html
    assert html.count("<html") == 1
    assert "display:none" in html  # the preheader still goes in
    settings_store.delete_value(db_session, "ntpl:welcome:email_html:it")


def test_the_footer_reaches_both_parts(db_session):
    # Filters compare the two alternatives of a multipart message: one that
    # says noticeably less than the other is a signal in itself.
    _, html, text = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert "Ricevi questo messaggio" in html
    assert "Ricevi questo messaggio" in text and "<" not in text


def test_the_shell_is_structurally_sound(db_session):
    # An unclosed tag in the layout is an empty message in Outlook, not a
    # slightly crooked one. The MSO ghost table sits inside a comment, so the
    # parser never sees it and it cannot unbalance the count.
    from html.parser import HTMLParser

    class _Balance(HTMLParser):
        VOID = {"meta", "br", "img", "hr", "input", "link", "area", "base"}

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack, self.bad = [], []

        def handle_starttag(self, tag, attrs):
            if tag not in self.VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in self.VOID:
                return
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            else:
                self.bad.append((tag, self.stack[-3:]))

    _, html, _ = nt.render_email(db_session, "invite", "it", CTX)
    parser = _Balance()
    parser.feed(html)
    assert not parser.bad, "closing tag with no match: %r" % parser.bad
    assert not parser.stack, "never closed: %r" % parser.stack


def test_the_shell_stays_small(db_session):
    # Paid on every single message, against Gmail's ~102KB clipping limit and
    # the html/text ratio filters look at. Every type, not just the lightest:
    # a ceiling tested on one message says nothing about the others.
    for type_ in nt.TYPES:
        for loc in nt.LOCALES:
            _, html, _ = nt.render_email(db_session, type_, loc, CTX)
            assert len(html.encode()) < 12000, (type_, loc, len(html.encode()))


def test_a_local_base_url_produces_no_footer_link(db_session):
    # public_base_url defaults to http://localhost:8000 and an instance behind a
    # proxy may never have changed it. That link is dead for every recipient.
    settings_store.set_value(db_session, "public_base_url", "http://localhost:8000")
    _, html, text = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert "localhost" not in html and "localhost" not in text
    settings_store.set_value(db_session, "public_base_url", "https://af.example.test")
    _, html, text = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert "https://af.example.test" in html and "https://af.example.test" in text
    settings_store.delete_value(db_session, "public_base_url")


def test_the_footer_reaches_the_plain_part_even_without_the_shell(db_session):
    # The footer says who sent the message, which holds just as well when the
    # admin brought their own document and the shell is skipped.
    settings_store.set_value(db_session, "ntpl:welcome:email_html:it",
                             "<html><body><p>Ciao,</p><p>Il mio layout.</p></body></html>")
    _, html, text = nt.render_email(db_session, "welcome", "it", CTX)
    assert "af-card" not in html  # shell skipped
    assert "Ricevi questo messaggio" in text
    settings_store.delete_value(db_session, "ntpl:welcome:email_html:it")


def test_the_english_footer_is_wired_too(db_session):
    _, html, text = nt.render_email(db_session, "user_expiry", "en", CTX)
    assert "You are receiving this" in html and "You are receiving this" in text
    assert 'lang="en"' in html


def test_the_shell_is_not_reshaped_by_a_value(db_session):
    # real_name reaches {{ user_name }} in the manager templates. Deciding
    # "is this a whole document" on the rendered output let a user's own name
    # drop the layout from the mail their manager receives.
    ctx = dict(CTX, user_name="<body onload=x>")
    _, html, _ = nt.render_email(db_session, "manager_collect", "it", ctx)
    assert "af-card" in html  # still wrapped
    assert "<body onload=x>" not in html  # and the default template escapes it


def test_the_wordmark_comes_from_the_from_name_and_is_escaped(db_session, monkeypatch):
    # smtp_from_name is admin input and the layout renders with autoescape off,
    # so the escaping has to happen before it gets there.
    from app import runtime_config

    settings_store.set_value(db_session, "public_base_url", "https://af.example.test")
    monkeypatch.setattr(runtime_config, "smtp_config",
                        lambda: {"from_name": "Famiglia Rossi"})
    _, html, _ = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert ">Famiglia Rossi<" in html  # it really becomes the wordmark
    assert "Apri Famiglia Rossi" in html  # and the footer link follows it
    settings_store.delete_value(db_session, "public_base_url")

    monkeypatch.setattr(runtime_config, "smtp_config",
                        lambda: {"from_name": "<script>x</script>"})
    _, html, _ = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_missing_shell_still_sends_the_body(db_session, monkeypatch, tmp_path):
    # The file ships with the code; losing it must cost the styling, not the
    # notification.
    monkeypatch.setattr(nt, "_LAYOUT_PATH", tmp_path / "nope.html")
    monkeypatch.setattr(nt, "_layout_cache", [])
    _, html, text = nt.render_email(db_session, "user_expiry", "it", CTX)
    assert "af-card" not in html
    assert "<p>Ciao Mario Rossi,</p>" in html
    assert "display:none" in html
    assert text.startswith("Ciao Mario Rossi,")


def test_empty_html_gets_no_preheader(db_session):
    # A template an admin blanked out must stay empty, not become a bare div.
    settings_store.set_value(db_session, "ntpl:welcome:email_html:it", " ")
    _, html, _ = nt.render_email(db_session, "welcome", "it", CTX)
    assert html == ""
    settings_store.delete_value(db_session, "ntpl:welcome:email_html:it")
