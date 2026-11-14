"""Channel-split, localized notification templates.

Each message is a small Jinja string keyed by (type, part, locale):
  parts: email_subject, email_html, telegram
Built-in DEFAULTS below are the baseline; an admin may override any single
(type, part, locale) from Settings -> "Notification templates". Overrides live
in settings_store (encrypted key/value), key = f"ntpl:{type}:{part}:{locale}".
Blank/missing override -> the built-in default. Email plain-text is derived from
the HTML. Telegram is MarkdownV2: dynamic values are escaped with the |tg filter,
static markup (*bold*, _italics_) stays literal in the template.
"""
import html as _htmlmod
import logging
import re
from pathlib import Path

from jinja2 import ChainableUndefined, Environment, pass_context
from jinja2.sandbox import SandboxedEnvironment
from sqlmodel import Session

from app.config import get_settings
from app.services import email_blocks, settings_store

PARTS = ("email_subject", "email_html", "telegram")
LOCALES = ("it", "en")

# type -> variables available to that template (shown in the editor)
TYPES: dict[str, list[str]] = {
    # notify_expiry passes one context to both user_* types.
    "user_expiry": ["name", "plan_name", "expiry_date", "days", "grace_left", "suspended"],
    "manager_collect": ["name", "user_name", "plan_name", "expiry_date", "days", "amount_eur"],
    "user_overdue": ["name", "plan_name", "expiry_date", "days", "grace_left", "suspended"],
    "manager_overdue": ["name", "user_name", "plan_name", "expiry_date", "amount_eur"],
    "manager_digest": ["name", "items", "count", "window_days", "total_eur"],
    "welcome": ["name", "plan_name", "expiry_date", "public_url", "telegram_link"],
    "invite": ["name", "email", "login_url", "libraries", "plan_name", "inviter_name",
               "expires_date"],
}

# Types that don't use every channel. An invitee has no AppUser and no Telegram
# link yet -- the bot is connected from the profile page after first sign-in --
# so the invite mail is email-only and the editor shouldn't offer a dead part.
TYPE_PARTS: dict[str, tuple[str, ...]] = {
    "invite": ("email_subject", "email_html"),
}


def parts_for(type_: str) -> tuple[str, ...]:
    return TYPE_PARTS.get(type_, PARTS)


# Available in every template, injected at render time rather than carried
# by each caller's context. Listed separately in the editor for the same
# reason: they are not specific to any one message.
COMMON_VARS = ("site_url", "brand")


def public_site_url() -> str:
    """The instance's public address, or "" while it is still the local
    default. One place for that rule: the footer, the blocks and the
    unsubscribe link must all agree on whether there is a URL to give."""
    from app import runtime_config  # lazy: avoid an import cycle

    url = runtime_config.public_base_url()
    return "" if _LOCAL_URL_RE.match(url) else url


def _common_ctx() -> dict:
    """site_url is empty when public_base_url is still the local default, so
    a template calling button(site_url, ...) renders nothing rather than a
    link to localhost."""
    return {"site_url": public_site_url(), "brand": _brand()}

# Sample context to validate/preview a template without real data.
SAMPLE_CTX = {
    "name": "Mario Rossi",
    "user_name": "Mario Rossi",
    "plan_name": "Bronze",
    "expiry_date": "2026-07-22",
    "days": 3,
    "amount_eur": "5.00",
    "grace_left": 2,
    "suspended": False,
    "public_url": "https://seerr.example.com",
    "telegram_link": "https://t.me/mybot?start=abc123",
    "items": [
        {"user_name": "Mario Rossi", "plan_name": "Bronze",
         "expiry_date": "2026-07-22", "days_left": 3, "amount_eur": "5.00"},
        {"user_name": "Lucia Bianchi", "plan_name": "Gold",
         "expiry_date": "2026-07-25", "days_left": 6, "amount_eur": "50.00"},
    ],
    "count": 2,
    "window_days": 14,
    "total_eur": "55.00",
    "email": "mario.rossi@example.com",
    "login_url": "https://accessflow.example.com/login",
    "libraries": ["Film", "Serie TV"],
    "inviter_name": "Admin",
    "expires_date": "2026-08-21",
    "site_url": "https://accessflow.example.com",
    "brand": "AccessFlow",
}


def _tg(value) -> str:
    """Escape a dynamic value for Telegram MarkdownV2."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", str(value))


_env = SandboxedEnvironment(  # Fix #5: admin-editable templates run in a sandbox
    undefined=ChainableUndefined,  # missing var -> empty, never crash a send
    autoescape=False,              # plain text + markdown; html parts escape vars with |e
    trim_blocks=True,
    lstrip_blocks=True,
)
def _money(amount) -> str:
    """Format an amount (major units) with the configured display currency.
    EUR -> '5.00 €', USD -> '$5.00', 'none' -> '5.00'. Never converts."""
    from app import runtime_config  # lazy: avoid import cycle at module load
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    c = runtime_config.currency()
    n = f"{value:.2f}"
    sym, pos = c["symbol"], c["position"]
    if not sym:
        return n
    return f"{sym}{n}" if pos == "prefix" else f"{n} {sym}"


def _tgmoney(amount) -> str:
    """Like _money but MarkdownV2-escaped for Telegram (escapes the '.' etc.)."""
    return _tg(_money(amount))


log = logging.getLogger("pum.templates")

_MONTHS = {
    "it": ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
           "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"),
    "en": ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"),
}


def _fmt_date(value, locale) -> str:
    """2026-07-22 -> "22 luglio 2026". Anything else is returned unchanged.

    A reminder is read in a hurry; an ISO date is for a database. Shared by
    the |longdate filter and the rows() block, which would otherwise print
    the raw date in the one message that lists several of them."""
    raw = str(value or "")
    parts = raw.split("-")
    if len(parts) != 3:
        return raw  # 'illimitato', an empty value, an already-formatted date
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return raw
    # Guarded rather than trusted: expiry_date is a variable, and a template
    # may hand this filter anything. A month of 0 would index [-1] and quietly
    # report December.
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return raw
    return f"{day} {_MONTHS.get(locale, _MONTHS['en'])[month - 1]} {year}"


@pass_context
def _longdate(ctx, value) -> str:
    """The locale rides in the render context, so a template author writes
    {{ expiry_date|longdate }} and nothing more."""
    return _fmt_date(value, ctx.get("_locale"))


@pass_context
def _rows(ctx, items) -> str:
    """rows() with this render's locale applied to the dates and the
    "not shown" line. The block itself stays free of template concerns."""
    locale = ctx.get("_locale")
    strings = _LAYOUT_STRINGS.get(locale) or _LAYOUT_STRINGS["en"]
    return email_blocks.rows(
        items,
        fmt_date=lambda d: _fmt_date(d, locale),
        more_label=strings["more_rows"],
    )


_env.filters["tg"] = _tg
_env.filters["money"] = _money
_env.filters["tgmoney"] = _tgmoney
_env.filters["longdate"] = _longdate

# Layout blocks an admin can call from a template. Globals and not filters:
# they take several arguments and read as what they are, `{{ button(url,
# "Rinnova") }}`. Each escapes its own arguments -- autoescape is off here.
_env.globals.update(
    button=email_blocks.button,
    notice=email_blocks.notice,
    facts=email_blocks.facts,
    rows=_rows,
)

# Shown in the Settings editor next to the variables, so the blocks are
# discoverable without reading this file.
_BLOCK_NAMES = ("button", "notice", "facts", "rows")

BLOCKS = (
    'button(url, "Etichetta")',
    'notice("Testo", "warn|stop|info")',
    'facts("Etichetta", valore, "Etichetta", valore)',
    "rows(items)",
)


def _link_to_text(m: re.Match) -> str:
    """"Rinnova" + an href become "Rinnova (https://...)".

    A button is a link with no visible URL: readable in the HTML part, a dead
    end in the plain one, which is what a text-only client and most filters
    actually read."""
    href, label = m.group(1).strip(), re.sub(r"<[^>]+>", "", m.group(2)).strip()
    if not label:
        return href
    if label == href or href.startswith("mailto:"):
        return label
    return f"{label} ({href})"


def _html_to_text(html: str) -> str:
    """Cheap plain-text fallback for the multipart email."""
    # Drop what these carry, not just their tags: a template exported from
    # a mail builder brings a <head> with a <style>, and stripping only the
    # tags left the stylesheet as the first readable line -- the plain-text
    # part, and with it the inbox preview, opened with CSS.
    text = re.sub(r"(?is)<(style|script|title|head)\b[^>]*>.*?</\1\s*>", " ", html)
    # Keep the destination: the layout blocks render their call to action as a
    # styled link, so without this the plain part says "Rinnova" and stops.
    text = re.sub(r'(?is)<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>', _link_to_text, text)
    # Table cells carry real content now (facts, rows, notice), and stripping
    # their tags with no separator ran every value into the next one.
    text = re.sub(r"(?i)</t[dh]\s*>", " ", text)
    text = re.sub(r"(?i)</tr\s*>", "\n", text)
    text = re.sub(r"(?i)</table\s*>", "\n\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    # <li> carries the invite's step-by-step instructions and the digest's
    # per-user rows; without these the items run together into one long line.
    text = re.sub(r"(?i)<li\s*>", "- ", text)
    text = re.sub(r"(?i)</li\s*>", "\n", text)
    text = re.sub(r"(?i)</(p|ul|ol)\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = _htmlmod.unescape(text)
    # The blocks space their labels with &nbsp;, which unescapes to a
    # non-breaking space -- invisible here, but a stray byte in a part
    # that is meant to be plain.
    text = text.replace(" ", " ")
    # Nested tables leave runs of blank lines and trailing spaces behind.
    text = re.sub(r"[ \t]+(\n)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- defaults
def _d(it: str, en: str) -> dict:
    return {"it": it, "en": en}


DEFAULTS: dict[tuple[str, str], dict[str, str]] = {
    # ---- user_expiry ----
    ("user_expiry", "email_subject"): _d(
        "Il tuo abbonamento {{ plan_name }} scade tra {{ days }} giorni",
        "Your {{ plan_name }} subscription expires in {{ days }} day(s)",
    ),
    ("user_expiry", "email_html"): _d(
        "<p>Ciao {{ name|e }},</p>"
        "<p>il tuo abbonamento <strong>{{ plan_name|e }}</strong> scade "
        "fra {{ days }} giorni.</p>"
        '{{ facts("Piano", plan_name, "Scade il", expiry_date|longdate,'
        ' "Giorni rimasti", days) }}'
        "<p>Per non restare senza accesso, contatta il tuo referente e rinnova.</p>"
        '{{ button(site_url, "Vedi il tuo abbonamento") }}',
        "<p>Hello {{ name|e }},</p>"
        "<p>your <strong>{{ plan_name|e }}</strong> subscription expires "
        "in {{ days }} day(s).</p>"
        '{{ facts("Plan", plan_name, "Expires on", expiry_date|longdate,'
        ' "Days left", days) }}'
        "<p>Contact your manager to renew and keep your access.</p>"
        '{{ button(site_url, "View your subscription") }}',
    ),
    ("user_expiry", "telegram"): _d(
        "⏳ Ciao {{ name|tg }}\\! Il tuo *{{ plan_name|tg }}* scade il "
        "{{ expiry_date|tg }} \\(tra {{ days }}g\\)\\. Rinnova col tuo referente\\.",
        "⏳ Hi {{ name|tg }}\\! Your *{{ plan_name|tg }}* expires on "
        "{{ expiry_date|tg }} \\(in {{ days }}d\\)\\. Renew with your manager\\.",
    ),
    # ---- manager_collect ----
    ("manager_collect", "email_subject"): _d(
        "Incassa: {{ user_name }} — {{ plan_name }} scade tra {{ days }} giorni",
        "Collect: {{ user_name }} — {{ plan_name }} expires in {{ days }} day(s)",
    ),
    ("manager_collect", "email_html"): _d(
        "<p>Ciao {{ name|e }},</p>"
        "<p>c'è un pagamento da incassare da <strong>{{ user_name|e }}</strong>.</p>"
        '{{ facts("Utente", user_name, "Piano", plan_name,'
        ' "Scade il", expiry_date|longdate, "Importo", amount_eur|money) }}'
        '{{ button(site_url, "Apri " ~ brand) }}',
        "<p>Hello {{ name|e }},</p>"
        "<p>there is a payment to collect from <strong>{{ user_name|e }}</strong>.</p>"
        '{{ facts("User", user_name, "Plan", plan_name,'
        ' "Expires on", expiry_date|longdate, "Amount", amount_eur|money) }}'
        '{{ button(site_url, "Open " ~ brand) }}',
    ),
    ("manager_collect", "telegram"): _d(
        "💶 *{{ user_name|tg }}* — {{ plan_name|tg }} scade il {{ expiry_date|tg }} "
        "\\(tra {{ days }}g\\)\\. Dovuto: *{{ amount_eur|tgmoney }}*\\. Incassa e rinnova\\.",
        "💶 *{{ user_name|tg }}* — {{ plan_name|tg }} expires {{ expiry_date|tg }} "
        "\\(in {{ days }}d\\)\\. Due: *{{ amount_eur|tgmoney }}*\\. Collect and renew\\.",
    ),
    # ---- user_overdue (grace vs suspended via {% if %}) ----
    ("user_overdue", "email_subject"): _d(
        "{% if suspended %}Accesso {{ plan_name }} sospeso"
        "{% else %}Il tuo {{ plan_name }} è scaduto{% endif %}",
        "{% if suspended %}Your {{ plan_name }} access is suspended"
        "{% else %}Your {{ plan_name }} subscription is overdue{% endif %}",
    ),
    ("user_overdue", "email_html"): _d(
        "<p>Ciao {{ name|e }},</p>"
        "{% if suspended %}"
        '{{ notice("Il tuo accesso è sospeso: l\'abbonamento è scaduto.", "stop") }}'
        '{{ facts("Piano", plan_name, "Scaduto il", expiry_date|longdate) }}'
        "<p>Rinnova con il tuo referente per riattivarlo.</p>"
        "{% else %}"
        '{{ notice("Abbonamento scaduto. Hai ancora " ~ grace_left ~'
        ' " giorni prima della sospensione.", "warn") }}'
        '{{ facts("Piano", plan_name, "Scaduto il", expiry_date|longdate) }}'
        "<p>Rinnova ora con il tuo referente per non perdere l'accesso.</p>"
        "{% endif %}"
        '{{ button(site_url, "Vedi il tuo abbonamento") }}',
        "<p>Hello {{ name|e }},</p>"
        "{% if suspended %}"
        '{{ notice("Your access is suspended: the subscription has expired.", "stop") }}'
        '{{ facts("Plan", plan_name, "Expired on", expiry_date|longdate) }}'
        "<p>Renew with your manager to restore it.</p>"
        "{% else %}"
        '{{ notice("Subscription expired. You have " ~ grace_left ~'
        ' " days left before access is suspended.", "warn") }}'
        '{{ facts("Plan", plan_name, "Expired on", expiry_date|longdate) }}'
        "<p>Renew now with your manager to keep your access.</p>"
        "{% endif %}"
        '{{ button(site_url, "View your subscription") }}',
    ),
    ("user_overdue", "telegram"): _d(
        "{% if suspended %}🚫 Ciao {{ name|tg }}, il tuo *{{ plan_name|tg }}* è "
        "scaduto e l'accesso è sospeso\\. Rinnova per riattivare\\."
        "{% else %}⚠️ Ciao {{ name|tg }}, *{{ plan_name|tg }}* scaduto il "
        "{{ expiry_date|tg }}\\. Tolleranza: {{ grace_left }}g\\. Rinnova ora\\.{% endif %}",
        "{% if suspended %}🚫 Hi {{ name|tg }}, your *{{ plan_name|tg }}* expired and "
        "access is suspended\\. Renew to restore\\."
        "{% else %}⚠️ Hi {{ name|tg }}, *{{ plan_name|tg }}* expired {{ expiry_date|tg }}\\. "
        "Grace: {{ grace_left }}d\\. Renew now\\.{% endif %}",
    ),
    # ---- manager_overdue ----
    ("manager_overdue", "email_subject"): _d(
        "Incassa: {{ user_name }} — {{ plan_name }} scaduto",
        "Collect: {{ user_name }} — {{ plan_name }} overdue",
    ),
    ("manager_overdue", "email_html"): _d(
        "<p>Ciao {{ name|e }},</p>"
        '{{ notice("Abbonamento scaduto: l\'accesso di " ~ user_name ~ '
        '" sta per essere sospeso.", "warn") }}'
        '{{ facts("Utente", user_name, "Piano", plan_name,'
        ' "Scaduto il", expiry_date|longdate, "Importo", amount_eur|money) }}'
        '{{ button(site_url, "Apri " ~ brand) }}',
        "<p>Hello {{ name|e }},</p>"
        '{{ notice("Subscription expired: " ~ user_name ~ '
        '" is about to lose access.", "warn") }}'
        '{{ facts("User", user_name, "Plan", plan_name,'
        ' "Expired on", expiry_date|longdate, "Amount", amount_eur|money) }}'
        '{{ button(site_url, "Open " ~ brand) }}',
    ),
    ("manager_overdue", "telegram"): _d(
        "💶 *{{ user_name|tg }}* — {{ plan_name|tg }} scaduto il {{ expiry_date|tg }}\\. "
        "Dovuto: *{{ amount_eur|tgmoney }}*\\. Incassa e rinnova\\.",
        "💶 *{{ user_name|tg }}* — {{ plan_name|tg }} overdue since {{ expiry_date|tg }}\\. "
        "Due: *{{ amount_eur|tgmoney }}*\\. Collect and renew\\.",
    ),
    # ---- manager_digest (weekly cumulative; iterates items) ----
    ("manager_digest", "email_subject"): _d(
        "Da incassare: {{ count }} abbonamenti entro {{ window_days }} giorni",
        "To collect: {{ count }} subscription(s) within {{ window_days }} days",
    ),
    ("manager_digest", "email_html"): _d(
        "<p>Ciao {{ name|e }},</p>"
        "<p>hai <strong>{{ count }}</strong> abbonamenti da incassare nei prossimi "
        "{{ window_days }} giorni.</p>"
        "{{ rows(items[:40]) }}"
        "{% if count > 40 %}<p>\u2026e altri {{ count - 40 }}. Aprili in {{ brand }} per vederli tutti.</p>{% endif %}"
        '{{ facts("Totale da incassare", total_eur|money) }}'
        '{{ button(site_url, "Apri " ~ brand) }}',
        "<p>Hello {{ name|e }},</p>"
        "<p>you have <strong>{{ count }}</strong> subscription(s) to collect in the "
        "next {{ window_days }} days.</p>"
        "{{ rows(items[:40]) }}"
        "{% if count > 40 %}<p>\u2026and {{ count - 40 }} more. Open {{ brand }} to see them all.</p>{% endif %}"
        '{{ facts("Total to collect", total_eur|money) }}'
        '{{ button(site_url, "Open " ~ brand) }}',
    ),
    ("manager_digest", "telegram"): _d(
        "💶 Ciao {{ name|tg }}, {{ count }} da incassare entro {{ window_days }} giorni:\n"
        "{% for it in items[:25] %}• {{ it.user_name|tg }} — {{ it.plan_name|tg }} · "
        "{{ it.expiry_date|tg }} · {{ it.amount_eur|tgmoney }}\n{% endfor %}"
        "{% if count > 25 %}\\+{{ count - 25 }} non mostrati\n{% endif %}"
        "Totale: *{{ total_eur|tgmoney }}*",
        "💶 Hi {{ name|tg }}, {{ count }} to collect within {{ window_days }} days:\n"
        "{% for it in items[:25] %}• {{ it.user_name|tg }} — {{ it.plan_name|tg }} · "
        "{{ it.expiry_date|tg }} · {{ it.amount_eur|tgmoney }}\n{% endfor %}"
        "{% if count > 25 %}\\+{{ count - 25 }} not shown\n{% endif %}"
        "Total: *{{ total_eur|tgmoney }}*",
    ),
    # ---- welcome ----
    ("welcome", "email_subject"): _d(
        "Benvenuto in {{ plan_name }}",
        "Welcome to {{ plan_name }}",
    ),
    ("welcome", "email_html"): _d(
        "<p>Ciao {{ name|e }},</p>"
        "<p>il tuo accesso è attivo. Buona visione.</p>"
        '{{ facts("Piano", plan_name, "Scadenza", expiry_date|longdate) }}'
        '{{ button(public_url, "Richiedi film e serie") }}'
        "{% if telegram_link %}<p>Vuoi i promemoria anche su Telegram?</p>"
        '{{ button(telegram_link, "Collega il bot") }}{% endif %}',
        "<p>Hello {{ name|e }},</p>"
        "<p>your access is active. Enjoy.</p>"
        '{{ facts("Plan", plan_name, "Expires", expiry_date|longdate) }}'
        '{{ button(public_url, "Request movies and shows") }}'
        "{% if telegram_link %}<p>Want reminders on Telegram too?</p>"
        '{{ button(telegram_link, "Connect the bot") }}{% endif %}',
    ),
    ("welcome", "telegram"): _d(
        "🎉 Ciao {{ name|tg }}\\! Il tuo *{{ plan_name|tg }}* è attivo "
        "\\(scadenza: {{ expiry_date|tg }}\\)\\."
        "{% if public_url %}\nRichieste: {{ public_url|tg }}{% endif %}",
        "🎉 Hi {{ name|tg }}\\! Your *{{ plan_name|tg }}* is active "
        "\\(expires: {{ expiry_date|tg }}\\)\\."
        "{% if public_url %}\nRequests: {{ public_url|tg }}{% endif %}",
    ),
    # ---- invite (email only: the invitee has no account on either side yet) ----
    ("invite", "email_subject"): _d(
        "Invito ad accedere al server Plex di {{ inviter_name }}",
        "You have been invited to {{ inviter_name }}'s Plex server",
    ),
    ("invite", "email_html"): _d(
        "<p>Ciao {{ name|e }},</p>"
        "<p><strong>{{ inviter_name|e }}</strong> ti ha invitato a condividere "
        "il suo server Plex.</p>"
        '{{ facts("Librerie", libraries|join(", ") if libraries else "tutte",'
        ' "Piano", plan_name or "-",'
        ' "Invito valido fino al", expires_date|longdate if expires_date else "-") }}'
        '{{ notice("Usa sempre l\'indirizzo " ~ email ~ ": con un altro indirizzo"'
        ' " l\'accesso non viene riconosciuto.", "warn") }}'
        "<p><strong>Se hai già un account Plex</strong></p>"
        "<ol>"
        "<li>Apri la mail di Plex e accetta la condivisione "
        "(oppure vai su <a href=\"https://app.plex.tv\">app.plex.tv</a>).</li>"
        "<li>Torna qui e accedi con il tuo account Plex.</li>"
        "</ol>"
        "<p><strong>Se non hai ancora un account Plex</strong></p>"
        "<ol>"
        "<li>Crea un account gratuito su <a href=\"https://www.plex.tv\">plex.tv</a> "
        "con l'indirizzo {{ email|e }}.</li>"
        "<li>Apri la mail di Plex e accetta la condivisione.</li>"
        "<li>Torna qui e accedi con il tuo account Plex.</li>"
        "</ol>"
        '{{ button(login_url, "Accedi con Plex") }}',
        "<p>Hello {{ name|e }},</p>"
        "<p><strong>{{ inviter_name|e }}</strong> has invited you to their "
        "Plex server.</p>"
        '{{ facts("Libraries", libraries|join(", ") if libraries else "all",'
        ' "Plan", plan_name or "-",'
        ' "Invite valid until", expires_date|longdate if expires_date else "-") }}'
        '{{ notice("Always use the address " ~ email ~ ": with any other address"'
        ' " your access will not be recognised.", "warn") }}'
        "<p><strong>If you already have a Plex account</strong></p>"
        "<ol>"
        "<li>Open the email from Plex and accept the share "
        "(or go to <a href=\"https://app.plex.tv\">app.plex.tv</a>).</li>"
        "<li>Come back here and sign in with your Plex account.</li>"
        "</ol>"
        "<p><strong>If you do not have a Plex account yet</strong></p>"
        "<ol>"
        "<li>Create a free account at <a href=\"https://www.plex.tv\">plex.tv</a> "
        "using the address {{ email|e }}.</li>"
        "<li>Open the email from Plex and accept the share.</li>"
        "<li>Come back here and sign in with your Plex account.</li>"
        "</ol>"
        '{{ button(login_url, "Sign in with Plex") }}',
    ),
}


# ---------------------------------------------------------------- rendering
def _override_key(type_: str, part: str, locale: str) -> str:
    return f"ntpl:{type_}:{part}:{locale}"


def _default_source(type_: str, part: str, locale: str) -> str:
    entry = DEFAULTS.get((type_, part))
    if entry is None:
        return ""
    return entry.get(locale) or entry.get("en", "")


def _source(session: Session, type_: str, part: str, locale: str) -> str:
    """Override -> default, with locale fallback locale -> default_locale -> en."""
    override = settings_store.get_value(session, _override_key(type_, part, locale))
    if override:
        return override
    for loc in (locale, get_settings().default_locale, "en"):
        src = _default_source(type_, part, loc)
        if src:
            return src
    return ""


def render_part(session: Session, type_: str, part: str, locale: str, ctx: dict) -> str:
    src = _source(session, type_, part, locale)
    if not src.strip():
        return ""
    # _locale rides along for |longdate, which has no other way to know it.
    # Underscored so it does not look like a variable to use in a template.
    # The caller's ctx wins over the common values, never the other way.
    merged = dict(_common_ctx(), **ctx)
    merged.pop("_locale", None)  # the render's locale is not the caller's to set
    return _env.from_string(src).render(_locale=locale, **merged).strip()


_PREHEADER_MAX = 120
# Padding after the hidden preheader. Without it a client fills the rest of the
# preview line with whatever follows in the body -- the greeting we just skipped.
_PREHEADER_PAD = "&#847;&zwnj;&nbsp;" * 30


def _preheader(text: str) -> str:
    """The inbox-preview line: the first line of the body that says something.

    With no preheader a client previews the start of the body, which for nearly
    every template here is "Ciao <name>," -- the one line carrying no
    information, shown next to the subject in the message list."""
    lines = [ln for ln in (ln.strip() for ln in text.splitlines()) if ln]
    if not lines:
        return ""
    # Drop the greeting: the FIRST line, and only when it ends in a comma.
    # Keyed on position, not on length -- a length cutoff turned a long
    # real_name ("Ciao Giovanni Battista Della Rovere,") into the preview
    # itself, and the padding after it then hid the informative line that a
    # client would otherwise have shown.
    start = 1 if len(lines) > 1 and lines[0].endswith(",") else 0
    line = lines[start]
    clipped = line[:_PREHEADER_MAX].rstrip()
    return clipped + "…" if len(line) > _PREHEADER_MAX else clipped


_BODY_RE = re.compile(r"<body\b[^>]*>", re.I)


def _with_preheader(html: str, text: str) -> str:
    """Insert the hidden preview line. Admin copy itself is untouched."""
    pre = _preheader(text)
    if not pre:
        return html
    # mso-hide:all is the Outlook half of the hide; the rest covers every other
    # client, including the ones that ignore display:none on its own.
    block = (
        '<div style="display:none;max-height:0;max-width:0;overflow:hidden;'
        'opacity:0;mso-hide:all;font-size:1px;line-height:1px;color:transparent">'
        + _htmlmod.escape(pre, quote=False) + _PREHEADER_PAD + "</div>"
    )
    # The built-in templates are fragments, but an admin may well paste a whole
    # document. Going in front of <!DOCTYPE> would drop the page into quirks
    # mode and leave the block outside <body>, where most webmail strips it --
    # losing the preheader silently.
    opening = _BODY_RE.search(html)
    if opening:
        return html[:opening.end()] + block + html[opening.end():]
    return block + html


# ---------------------------------------------------------------- layout
# The shell the rendered fragment is dropped into: hand-written table HTML,
# versioned like any other template, with no build step behind it. See the
# comment at the top of that file for why it is not generated.
_LAYOUT_PATH = Path(__file__).resolve().parent.parent / "templates" / "email" / "_layout.html"

# A plain Environment, NOT the sandboxed _env above: that one deliberately has
# no loader, and this one never renders anything an admin wrote. Keeping them
# apart is what stops an override from reaching the filesystem.
_layout_env = Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True)
_layout_cache: list = []  # one slot: the compiled Template

# Already a whole document of the admin's own: wrapping it in ours would nest a
# second <html> inside the first and break both.
_FULL_DOC_RE = re.compile(r"<(?:html|body|head)\b", re.I)

# public_base_url defaults to http://localhost:8000, and an instance behind a
# proxy can easily never have changed it -- the setting is introduced as the
# one that fixes the cookie Secure flag. A footer pointing there is a dead
# link for every recipient, or worse whatever answers on their own port 8000,
# so drop the link rather than ship it.
_LOCAL_URL_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?(/|$)", re.I
)

_LAYOUT_STRINGS = {
    "it": {
        "footer_note": "Ricevi questo messaggio perché il tuo accesso è gestito con %(brand)s.",
        "site_label": "Apri %(brand)s",
        "more_rows": "+ %d non mostrati",
        "unsubscribe": "Non ricevere più queste email",
    },
    "en": {
        "footer_note": "You are receiving this because your access is managed with %(brand)s.",
        "site_label": "Open %(brand)s",
        "more_rows": "+ %d not shown",
        "unsubscribe": "Stop these emails",
    },
}


def _layout_template():
    """The shell, parsed once. None when the file cannot be read.

    Caches the compiled Template and not the source: from_string() does not
    go through Jinja's own cache, so holding the source still re-parsed the
    whole shell for every single message (~3ms each, which a 500-reminder
    scan pays with the scheduler's job lock held)."""
    if _layout_cache:
        return _layout_cache[0]
    try:
        source = _LAYOUT_PATH.read_text(encoding="utf-8")
    except OSError:
        # Deliberately NOT cached. A transient failure (a rolling update, a
        # hiccup on a mounted volume) would otherwise send every later
        # message unstyled for the life of the process, with one log line
        # at the start and nothing visible afterwards.
        log.warning("email layout unreadable at %s; sending unwrapped bodies",
                    _LAYOUT_PATH)
        return None
    _layout_cache.append(_layout_env.from_string(source))
    return _layout_cache[0]


def _brand() -> str:
    from app import runtime_config  # lazy: avoid an import cycle at module load

    return (runtime_config.smtp_config().get("from_name") or "").strip() or "AccessFlow"


def _compose(fragment: str, text: str, locale: str, *,
             full_doc: bool, unsubscribe_url: str = "") -> tuple[str, str]:
    """Wrap the rendered fragment in the layout. Returns (html, plain_text)."""
    strings = _LAYOUT_STRINGS.get(locale) or _LAYOUT_STRINGS["en"]
    brand = _brand()
    site_url = public_site_url()
    footer_note = strings["footer_note"] % {"brand": brand}
    # Appended whichever branch we take below: the footer says who sent the
    # message, which is as true of an admin's own document as of ours.
    tail = footer_note + (" " + site_url if site_url else "")
    if unsubscribe_url:
        # In the plain part as well: a text-only client has no header
        # button, and this line is the only way out it can offer.
        tail += "\n" + strings["unsubscribe"] + ": " + unsubscribe_url
    text_out = (text + "\n\n--\n" + tail) if text else text

    template = _layout_template()
    if template is None or full_doc:
        return _with_preheader(fragment, text), text_out

    pre = _preheader(text)
    accent = email_blocks.palette()
    html = template.render(
        lang=locale if locale in LOCALES else "en",
        # Straight from the theme table, never from admin input, so they
        # go into the stylesheet as-is.
        accent=accent["accent"],
        accent_link=accent["link"],
        accent_dark=accent["dark"],
        brand=_htmlmod.escape(brand),
        # Escaped here, not by the environment: autoescape is off so that
        # `content` can carry the markup the admin's template produced.
        # quote=False on the preheader: it is a text node, and some clients
        # build the list snippet by stripping tags WITHOUT decoding entities,
        # which turns an apostrophe into a visible &#x27;.
        preheader=(_htmlmod.escape(pre, quote=False) + _PREHEADER_PAD) if pre else "",
        content=fragment,
        footer_note=_htmlmod.escape(footer_note),
        site_url=_htmlmod.escape(site_url),
        site_label=_htmlmod.escape(strings["site_label"] % {"brand": brand}),
        unsubscribe_url=_htmlmod.escape(unsubscribe_url),
        unsubscribe_label=_htmlmod.escape(strings["unsubscribe"]),
    )
    return html, text_out


def render_email(session: Session, type_: str, locale: str, ctx: dict, *,
                 unsubscribe_url: str = "") -> tuple[str, str, str]:
    """Returns (subject, html, plain_text).

    `unsubscribe_url` puts a visible way out in the footer of both parts.
    It belongs to the recipient, not to the template, which is why it is
    an argument here and not one more variable in `ctx`."""
    # Folded to one line: the subject is edited in a textarea, and EmailMessage
    # raises on a header holding CR/LF. One stray Enter there would otherwise
    # fail every send of that type forever -- logged, never delivered, with
    # nothing in Settings to say why.
    subject = " ".join(render_part(session, type_, "email_subject", locale, ctx).split())
    html = render_part(session, type_, "email_html", locale, ctx)
    # Derive the text part BEFORE the preheader goes in: the preheader repeats a
    # line that is already in the body, and a plain-text part opening by saying
    # the same thing twice reads as broken.
    text = _html_to_text(html) if html else ""
    if html:
        # Decided on the TEMPLATE, not on the rendered output: whether this
        # is a whole document is a property of what the admin wrote. Read
        # from the render, a value could decide it instead -- a user's
        # real_name reaches {{ user_name }} in the manager templates, so a
        # name holding a tag would reshape the mail their manager gets.
        full_doc = bool(_FULL_DOC_RE.search(
            _source(session, type_, "email_html", locale)))
        html, text = _compose(html, text, locale, full_doc=full_doc,
                              unsubscribe_url=unsubscribe_url)
    return subject, html, text


def render_telegram(session: Session, type_: str, locale: str, ctx: dict) -> str:
    return render_part(session, type_, "telegram", locale, ctx)


def validate(src: str, type_: str | None = None,
             part: str | None = None) -> str | None:
    """None if `src` renders, else the error message.

    With `type_`, only that type's variables are defined, as at send time.
    Against the whole SAMPLE_CTX a digest template using `days` passed here,
    then raised on `days > 1` when sent and aborted the digest run.

    With `part`, the layout blocks are refused outside the HTML body: they
    are globals on the whole environment, so pasting one into the subject
    renders a table into a header, and into the Telegram part it produces
    markup Telegram rejects outright."""
    if part and part != "email_html":
        used = [b for b in _BLOCK_NAMES
                if re.search(r"\b%s\s*\(" % b, src)]
        if used:
            return ("%s() renders HTML and belongs in the email body only."
                    % used[0])
    ctx = (SAMPLE_CTX if type_ is None else
           {k: SAMPLE_CTX[k] for k in (*TYPES[type_], *COMMON_VARS)})
    try:
        # _locale so |longdate behaves here as it will at send time.
        _env.from_string(src).render(_locale=get_settings().default_locale, **ctx)
        return None
    except Exception as exc:  # noqa: BLE001 — surface any Jinja error to the editor
        return str(exc)


# ---------------------------------------------------------------- editor data
def editor_entries(session: Session) -> list[dict]:
    """One block per type, each with its parts x locales (override or default)."""
    out = []
    for type_, variables in TYPES.items():
        rows = []
        for part in parts_for(type_):
            for loc in LOCALES:
                override = settings_store.get_value(session, _override_key(type_, part, loc))
                rows.append({
                    "part": part,
                    "locale": loc,
                    "text": override if override is not None else _default_source(type_, part, loc),
                    "overridden": override is not None,
                })
        out.append({"type": type_, "variables": variables, "rows": rows})
    return out
