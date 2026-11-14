# -*- coding: utf-8 -*-
import pytest

from app import runtime_config
from app.services import email_blocks as eb
from app.services import notification_templates as nt
from app.services import settings_store


# ---- button ----

def test_button_renders_the_label_and_the_link(db_session):
    out = eb.button("https://af.example.test/x", "Rinnova ora")
    assert 'href="https://af.example.test/x"' in out
    assert ">Rinnova ora</a>" in out
    assert "<table" in out and "bgcolor=" in out  # Outlook needs both


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "data:text/html,<script>x</script>",
    "ftp://host/file",
    "/relative/path",
    "",
    None,
])
def test_button_only_accepts_http_links(db_session, url):
    # A template variable reaches the href, and nobody inspects an admin's
    # template the way they would inspect a page.
    assert eb.button(url, "Vai") == ""


def test_button_escapes_its_label(db_session):
    out = eb.button("https://x.test", '"><script>alert(1)</script>')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_button_follows_the_instance_theme(db_session):
    settings_store.set_value(db_session, "color_theme", "muschio")
    assert eb._THEME_COLOURS["muschio"]["link"] in eb.button("https://x.test", "Vai")
    settings_store.set_value(db_session, "color_theme", "rame")
    assert eb._THEME_COLOURS["rame"]["link"] in eb.button("https://x.test", "Vai")
    settings_store.delete_value(db_session, "color_theme")


def test_an_unknown_theme_falls_back(db_session, monkeypatch):
    monkeypatch.setattr(runtime_config, "color_theme", lambda: "nuovo-tema")
    assert eb.palette() == eb._THEME_COLOURS["rame"]


# ---- notice ----

@pytest.mark.parametrize("tone", ["warn", "stop", "info"])
def test_notice_uses_its_tone(tone):
    out = eb.notice("Attenzione", tone)
    assert eb._TONES[tone]["line"] in out and "Attenzione" in out


def test_an_unknown_tone_does_not_break_the_send():
    assert eb._TONES["warn"]["line"] in eb.notice("x", "inventato")


def test_notice_escapes_its_text():
    assert "<b>" not in eb.notice("<b>grassetto", "warn")


# ---- facts ----

def test_facts_pairs_labels_with_values():
    out = eb.facts("Piano", "Bronze", "Scade il", "22 luglio 2026")
    assert "Piano" in out and "Bronze" in out
    assert "Scade il" in out and "22 luglio 2026" in out
    assert out.count("<tr>") == 2


def test_facts_with_no_pairs_renders_nothing():
    assert eb.facts() == ""


def test_facts_ignores_a_dangling_label():
    # facts("A", 1, "B") -- the odd one out has no value to show.
    assert eb.facts("A", "1", "B").count("<tr>") == 1


# ---- rows ----

def _items():
    return [
        {"user_name": "Mario Rossi", "plan_name": "Bronze",
         "expiry_date": "2026-07-22", "days_left": 3, "amount_eur": "5.00"},
        {"user_name": "Lucia Bianchi", "plan_name": "Gold",
         "expiry_date": "2026-07-10", "days_left": -4, "amount_eur": "50.00"},
    ]


def test_rows_stacks_name_and_detail_on_separate_lines(db_session):
    # One line per entry wraps wherever it runs out of room at phone width,
    # which puts the amount under the plan name.
    out = eb.rows(_items())
    assert "Mario Rossi" in out and "Lucia Bianchi" in out
    assert out.count('colspan="2"') == 2  # the detail line of each entry


def test_rows_marks_the_overdue_one(db_session):
    out = eb.rows(_items())
    assert "af-late" in out  # Lucia is 4 days past
    assert "af-dim" in out   # Mario is not


def test_rows_survives_an_empty_or_odd_list(db_session):
    assert eb.rows([]) == ""
    assert eb.rows(None) == ""
    assert "af-" in eb.rows([{"user_name": "Solo nome"}])  # missing keys are fine


def test_rows_caps_a_long_list(db_session):
    # Each entry costs a few hundred bytes of inline style; Gmail clips the
    # message past ~102KB, and the totals at the bottom go first.
    many = [dict(_items()[0], user_name="Utente %d" % i) for i in range(200)]
    out = eb.rows(many)
    assert out.count("af-strong") == eb.MAX_ROWS * 2  # name + amount per entry
    assert "+ %d" % (200 - eb.MAX_ROWS) in out


def test_rows_takes_the_wording_of_its_overflow_line(db_session):
    # The block has no locale; whoever renders it does.
    many = [dict(_items()[0]) for _ in range(70)]
    assert "10 non mostrati" in eb.rows(many, more_label="%d non mostrati")


def test_a_broken_overflow_label_still_sends(db_session):
    # A placeholder an admin got wrong must not raise mid-run.
    many = [dict(_items()[0]) for _ in range(70)]
    assert "+ 10" in eb.rows(many, more_label="nessun segnaposto")


def test_rows_formats_its_dates_through_the_caller(db_session):
    out = eb.rows(_items(), fmt_date=lambda d: "il " + d)
    assert "il 2026-07-22" in out


def test_the_digest_stays_under_the_clipping_limit(db_session):
    # Not a style check: past the limit Gmail hides the end of the message
    # behind a "message truncated" link.
    ctx = dict(nt.SAMPLE_CTX)
    ctx["items"] = [dict(_items()[0], user_name="Utente %d" % i) for i in range(2000)]
    ctx["count"] = 2000
    _, html, _ = nt.render_email(db_session, "manager_digest", "it", ctx)
    assert len(html.encode()) < 102 * 1024
    assert "e altri 1960" in html  # and it says what it left out


def test_rows_escapes_its_values(db_session):
    out = eb.rows([{"user_name": "<script>x</script>", "plan_name": "P",
                    "expiry_date": "2026-01-01", "days_left": 1, "amount_eur": "1"}])
    assert "<script>" not in out and "&lt;script&gt;" in out


# ---- longdate ----

@pytest.mark.parametrize("locale, expected", [
    ("it", "22 luglio 2026"),
    ("en", "22 July 2026"),
])
def test_longdate(locale, expected):
    assert nt._env.from_string("{{ d|longdate }}").render(
        _locale=locale, d="2026-07-22") == expected


@pytest.mark.parametrize("value", ["illimitato", "", "non-una-data", "2026-13-99"])
def test_longdate_leaves_anything_else_alone(value):
    out = nt._env.from_string("{{ d|longdate }}").render(_locale="it", d=value)
    assert out == value


# ---- il contesto comune ----

def test_site_url_is_empty_while_the_base_url_is_local(db_session):
    settings_store.set_value(db_session, "public_base_url", "http://localhost:8000")
    assert nt._common_ctx()["site_url"] == ""
    settings_store.set_value(db_session, "public_base_url", "https://af.example.test")
    assert nt._common_ctx()["site_url"] == "https://af.example.test"
    settings_store.delete_value(db_session, "public_base_url")


def test_a_template_can_use_the_common_variables(db_session):
    settings_store.set_value(db_session, "public_base_url", "https://af.example.test")
    settings_store.set_value(db_session, "smtp_from_name", "Casa Rossi")
    # user_expiry calls button(site_url, ...) without being handed either value.
    # SAMPLE_CTX carries its own site_url for the editor preview, and a caller's
    # context wins, so drop it here to exercise the injected one.
    ctx = {k: v for k, v in nt.SAMPLE_CTX.items() if k not in nt.COMMON_VARS}
    out = nt.render_part(db_session, "user_expiry", "email_html", "it", ctx)
    assert 'href="https://af.example.test"' in out
    assert nt._common_ctx()["brand"] == "Casa Rossi"
    settings_store.delete_value(db_session, "public_base_url")
    settings_store.delete_value(db_session, "smtp_from_name")


def test_the_caller_context_wins_over_the_common_one(db_session):
    # Through render_part, not by building the dict by hand: done that way the
    # test proves how dict(**) works in Python and would pass even if the merge
    # in render_part were the other way round.
    settings_store.set_value(db_session, "smtp_from_name", "Istanza")
    settings_store.set_value(db_session, "ntpl:user_expiry:email_subject:it",
                             "{{ brand }}")
    assert nt.render_part(db_session, "user_expiry", "email_subject", "it",
                          {}) == "Istanza"
    assert nt.render_part(db_session, "user_expiry", "email_subject", "it",
                          {"brand": "Dal chiamante"}) == "Dal chiamante"
    settings_store.delete_value(db_session, "ntpl:user_expiry:email_subject:it")
    settings_store.delete_value(db_session, "smtp_from_name")


# ---- i template di serie usano i blocchi ----

def test_the_digest_uses_the_stacked_rows(db_session):
    _, html, _ = nt.render_email(db_session, "manager_digest", "it", nt.SAMPLE_CTX)
    assert "<li>" not in html  # was a bullet list, which wrapped badly on phones
    assert 'colspan="2"' in html


def test_no_default_leaves_braces_in_the_output(db_session):
    """Braces inside a quoted string are not interpolated: Jinja consumes the
    whole string as one token. `notice("... {{ user_name }} ...")` shipped the
    literal text to the manager, in the body and in the inbox preview, and the
    whole suite went green."""
    for (type_, part), by_locale in nt.DEFAULTS.items():
        if part != "email_html":
            continue
        for loc in by_locale:
            _, html, text = nt.render_email(db_session, type_, loc, nt.SAMPLE_CTX)
            assert "{{" not in html and "}}" not in html, (type_, loc)
            assert "{%" not in html, (type_, loc)
            assert "{{" not in text, (type_, loc)


def test_every_class_the_blocks_emit_has_a_dark_rule(db_session):
    """A class the layout styles but nobody emits, or one emitted with no dark
    rule, both read as working until someone opens the message at night."""
    import re

    layout = nt._LAYOUT_PATH.read_text(encoding="utf-8")
    dark = re.search(r"(?s)prefers-color-scheme: dark\s*\)\s*\{(.*?)\n  \}", layout)
    styled = set(re.findall(r"\.(af-[a-z-]+)", dark.group(1)))

    emitted = set()
    for block in (eb.facts("A", "1"),
                  eb.notice("x", "warn"),
                  eb.rows(_items())):
        emitted |= {c for group in re.findall(r'class="([^"]+)"', block)
                    for c in group.split()}
    assert emitted, "the blocks should carry classes for the dark overrides"
    assert emitted <= styled, "no dark rule for: %s" % sorted(emitted - styled)


def test_the_overrides_win_over_the_generic_body_colour(db_session):
    """`.af-body td` is more specific than `.af-dim`, and the blocks put those
    classes on <td>: without a matching selector the overdue rows stopped
    being distinguishable from the rest in dark mode."""
    layout = nt._LAYOUT_PATH.read_text(encoding="utf-8")
    for cls in ("af-dim", "af-strong", "af-late"):
        assert ".af-body td.%s" % cls in layout, cls


def test_the_blocks_survive_the_trip_to_plain_text(db_session):
    # _html_to_text was extended for exactly these; nothing was holding it.
    html = ("<p>Ciao,</p>" + eb.facts("Piano", "Bronze")
            + eb.rows(_items()) + eb.button("https://af.test/x", "Rinnova"))
    text = nt._html_to_text(html)
    # Label and value keep a separator: run together they read as one word.
    assert "PianoBronze" not in text
    assert "Piano" in text and "Bronze" in text
    # One entry per line, not every name welded to the next.
    assert "Mario Rossi" in text and "Lucia Bianchi" in text
    assert "RossiLucia" not in text.replace(" ", "")
    assert "Rinnova (https://af.test/x)" in text  # the destination survives
    assert "<" not in text and "&nbsp;" not in text


def test_the_colour_table_covers_every_theme_the_ui_offers():
    # Adding a fourth theme in runtime_config without one here would send every
    # email with the default accent, quietly.
    assert eb.missing_themes() == set()


@pytest.mark.parametrize("part", ["email_subject", "telegram"])
def test_a_block_is_refused_outside_the_html_body(part):
    # The blocks are globals on the whole environment, so nothing stops an
    # admin from pasting one into the subject -- where it renders a table.
    assert nt.validate('{{ facts("a", "b") }}', "user_expiry", part) is not None
    assert nt.validate("Testo normale {{ plan_name }}", "user_expiry", part) is None


def test_a_block_is_allowed_in_the_html_body():
    assert nt.validate('{{ facts("a", "b") }}', "user_expiry", "email_html") is None


def test_the_telegram_digest_stays_within_the_message_limit(db_session):
    # sendMessage caps at 4096 characters; past it Telegram answers 400 and the
    # manager gets nothing at all on that channel.
    ctx = dict(nt.SAMPLE_CTX)
    ctx["items"] = [dict(_items()[0], user_name="Utente %d" % i) for i in range(400)]
    ctx["count"] = 400
    body = nt.render_telegram(db_session, "manager_digest", "it", ctx)
    assert len(body) < 4096
    assert "non mostrati" in body


def test_every_default_html_still_validates(db_session):
    # The blocks are globals on the sandbox: a typo in a call would only show
    # up at send time otherwise.
    for (type_, part), by_locale in nt.DEFAULTS.items():
        if part != "email_html":
            continue
        for loc, src in by_locale.items():
            assert nt.validate(src, type_) is None, (type_, loc)
