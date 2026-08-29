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
import re

from jinja2 import ChainableUndefined
from jinja2.sandbox import SandboxedEnvironment
from sqlmodel import Session

from app.config import get_settings
from app.services import settings_store

PARTS = ("email_subject", "email_html", "telegram")
LOCALES = ("it", "en")

# type -> variables available to that template (shown in the editor)
TYPES: dict[str, list[str]] = {
    "user_expiry": ["name", "plan_name", "expiry_date", "days"],
    "manager_collect": ["name", "user_name", "plan_name", "expiry_date", "days", "amount_eur"],
    "user_overdue": ["name", "plan_name", "expiry_date", "grace_left", "suspended"],
    "manager_overdue": ["name", "user_name", "plan_name", "expiry_date", "amount_eur"],
    "manager_digest": ["name", "items", "count", "window_days", "total_eur"],
    "welcome": ["name", "plan_name", "expiry_date", "public_url", "telegram_link"],
    "invite": ["name", "email", "login_url", "libraries", "plan_name", "inviter_name"],
}

# Types that don't use every channel. An invitee has no AppUser and no Telegram
# link yet -- the bot is connected from the profile page after first sign-in --
# so the invite mail is email-only and the editor shouldn't offer a dead part.
TYPE_PARTS: dict[str, tuple[str, ...]] = {
    "invite": ("email_subject", "email_html"),
}


def parts_for(type_: str) -> tuple[str, ...]:
    return TYPE_PARTS.get(type_, PARTS)

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


_env.filters["tg"] = _tg
_env.filters["money"] = _money
_env.filters["tgmoney"] = _tgmoney


def _html_to_text(html: str) -> str:
    """Cheap plain-text fallback for the multipart email."""
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return _htmlmod.unescape(text).strip()


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
        "<p>il tuo abbonamento <strong>{{ plan_name|e }}</strong> scade il "
        "<strong>{{ expiry_date }}</strong> (tra {{ days }} giorni).</p>"
        "<p>Contatta il tuo referente per rinnovare.</p>",
        "<p>Hello {{ name|e }},</p>"
        "<p>your <strong>{{ plan_name|e }}</strong> subscription expires on "
        "<strong>{{ expiry_date }}</strong> (in {{ days }} day(s)).</p>"
        "<p>Please contact your manager to renew.</p>",
    ),
    ("user_expiry", "telegram"): _d(
        "⏳ Ciao {{ name|tg }}! Il tuo *{{ plan_name|tg }}* scade il "
        "{{ expiry_date|tg }} \\(tra {{ days }}g\\)\\. Rinnova col tuo referente\\.",
        "⏳ Hi {{ name|tg }}! Your *{{ plan_name|tg }}* expires on "
        "{{ expiry_date|tg }} \\(in {{ days }}d\\)\\. Renew with your manager\\.",
    ),
    # ---- manager_collect ----
    ("manager_collect", "email_subject"): _d(
        "Incassa: {{ user_name }} — {{ plan_name }} scade tra {{ days }} giorni",
        "Collect: {{ user_name }} — {{ plan_name }} expires in {{ days }} day(s)",
    ),
    ("manager_collect", "email_html"): _d(
        "<p>L'abbonamento <strong>{{ plan_name|e }}</strong> di {{ user_name|e }} "
        "scade il <strong>{{ expiry_date }}</strong> (tra {{ days }} giorni).</p>"
        "<p>Importo dovuto: <strong>{{ amount_eur|money }}</strong>. "
        "Incassa il pagamento e rinnova.</p>",
        "<p>{{ user_name|e }}'s <strong>{{ plan_name|e }}</strong> subscription "
        "expires on <strong>{{ expiry_date }}</strong> (in {{ days }} day(s)).</p>"
        "<p>Amount due: <strong>{{ amount_eur|money }}</strong>. "
        "Please collect payment and renew.</p>",
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
        "<p>il tuo abbonamento <strong>{{ plan_name|e }}</strong> è scaduto il "
        "{{ expiry_date }} e l'accesso è ora <strong>sospeso</strong>. "
        "Rinnova per riattivarlo.</p>"
        "{% else %}"
        "<p>il tuo abbonamento <strong>{{ plan_name|e }}</strong> è scaduto il "
        "{{ expiry_date }}. La tolleranza finisce tra {{ grace_left }} giorni — "
        "rinnova ora per non perdere l'accesso.</p>"
        "{% endif %}",
        "<p>Hello {{ name|e }},</p>"
        "{% if suspended %}"
        "<p>your <strong>{{ plan_name|e }}</strong> subscription expired on "
        "{{ expiry_date }} and access is now <strong>suspended</strong>. "
        "Renew to restore access.</p>"
        "{% else %}"
        "<p>your <strong>{{ plan_name|e }}</strong> subscription expired on "
        "{{ expiry_date }}. Grace ends in {{ grace_left }} day(s) — renew now to "
        "keep access.</p>"
        "{% endif %}",
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
        "<p>L'abbonamento <strong>{{ plan_name|e }}</strong> di {{ user_name|e }} "
        "è scaduto il <strong>{{ expiry_date }}</strong>.</p>"
        "<p>Importo dovuto: <strong>{{ amount_eur|money }}</strong>. Incassa e rinnova.</p>",
        "<p>{{ user_name|e }}'s <strong>{{ plan_name|e }}</strong> subscription "
        "expired on <strong>{{ expiry_date }}</strong>.</p>"
        "<p>Amount due: <strong>{{ amount_eur|money }}</strong>. Please collect and renew.</p>",
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
        "<p>Hai <strong>{{ count }}</strong> abbonamenti da incassare nei prossimi "
        "{{ window_days }} giorni:</p>"
        "<ul>"
        "{% for it in items %}<li>{{ it.user_name|e }} — <strong>{{ it.plan_name|e }}</strong> "
        "· scade il {{ it.expiry_date }} ({{ it.days_left }}g) · "
        "<strong>{{ it.amount_eur|money }}</strong></li>{% endfor %}"
        "</ul>"
        "<p>Totale da incassare: <strong>{{ total_eur|money }}</strong>.</p>",
        "<p>Hello {{ name|e }},</p>"
        "<p>You have <strong>{{ count }}</strong> subscription(s) to collect in the next "
        "{{ window_days }} days:</p>"
        "<ul>"
        "{% for it in items %}<li>{{ it.user_name|e }} — <strong>{{ it.plan_name|e }}</strong> "
        "· expires {{ it.expiry_date }} ({{ it.days_left }}d) · "
        "<strong>{{ it.amount_eur|money }}</strong></li>{% endfor %}"
        "</ul>"
        "<p>Total to collect: <strong>{{ total_eur|money }}</strong>.</p>",
    ),
    ("manager_digest", "telegram"): _d(
        "💶 Ciao {{ name|tg }}, {{ count }} da incassare entro {{ window_days }} giorni:\n"
        "{% for it in items %}• {{ it.user_name|tg }} — {{ it.plan_name|tg }} · "
        "{{ it.expiry_date|tg }} · {{ it.amount_eur|tgmoney }}\n{% endfor %}"
        "Totale: *{{ total_eur|tgmoney }}*",
        "💶 Hi {{ name|tg }}, {{ count }} to collect within {{ window_days }} days:\n"
        "{% for it in items %}• {{ it.user_name|tg }} — {{ it.plan_name|tg }} · "
        "{{ it.expiry_date|tg }} · {{ it.amount_eur|tgmoney }}\n{% endfor %}"
        "Total: *{{ total_eur|tgmoney }}*",
    ),
    # ---- welcome ----
    ("welcome", "email_subject"): _d(
        "Benvenuto in {{ plan_name }}",
        "Welcome to {{ plan_name }}",
    ),
    ("welcome", "email_html"): _d(
        "<p>Ciao {{ name|e }},</p>"
        "<p>il tuo abbonamento <strong>{{ plan_name|e }}</strong> è attivo "
        "(scadenza: {{ expiry_date }}).</p>"
        "{% if public_url %}<p>Richiedi film e serie qui: "
        "<a href=\"{{ public_url }}\">{{ public_url }}</a></p>{% endif %}"
        "{% if telegram_link %}<p>Attiva i promemoria su Telegram: "
        "<a href=\"{{ telegram_link }}\">{{ telegram_link }}</a></p>{% endif %}",
        "<p>Hello {{ name|e }},</p>"
        "<p>your <strong>{{ plan_name|e }}</strong> subscription is now active "
        "(expires: {{ expiry_date }}).</p>"
        "{% if public_url %}<p>Request movies and shows here: "
        "<a href=\"{{ public_url }}\">{{ public_url }}</a></p>{% endif %}"
        "{% if telegram_link %}<p>Enable reminders on Telegram: "
        "<a href=\"{{ telegram_link }}\">{{ telegram_link }}</a></p>{% endif %}",
    ),
    ("welcome", "telegram"): _d(
        "🎉 Ciao {{ name|tg }}! Il tuo *{{ plan_name|tg }}* è attivo "
        "\\(scadenza: {{ expiry_date|tg }}\\)\\."
        "{% if public_url %}\nRichieste: {{ public_url|tg }}{% endif %}",
        "🎉 Hi {{ name|tg }}! Your *{{ plan_name|tg }}* is active "
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
        "<p>{{ inviter_name|e }} ti ha invitato a condividere il suo server Plex"
        "{% if libraries %} (librerie: {{ libraries|join(', ')|e }}){% endif %}"
        "{% if plan_name %}, piano <strong>{{ plan_name|e }}</strong>{% endif %}.</p>"
        "<p>L'invito è stato inviato a <strong>{{ email|e }}</strong>: usa "
        "<strong>questo stesso indirizzo</strong> in ogni passaggio, altrimenti "
        "l'accesso non verrà riconosciuto.</p>"
        "<p><strong>Se hai già un account Plex</strong></p>"
        "<ol>"
        "<li>Apri la mail di Plex e accetta la condivisione "
        "(oppure vai su <a href=\"https://app.plex.tv\">app.plex.tv</a>).</li>"
        "<li>Accedi qui con il tuo account Plex: "
        "<a href=\"{{ login_url }}\">{{ login_url }}</a></li>"
        "</ol>"
        "<p><strong>Se non hai ancora un account Plex</strong></p>"
        "<ol>"
        "<li>Crea un account gratuito su <a href=\"https://www.plex.tv\">plex.tv</a>, "
        "usando l'indirizzo {{ email|e }}.</li>"
        "<li>Apri la mail di Plex e accetta la condivisione.</li>"
        "<li>Accedi qui con il tuo account Plex: "
        "<a href=\"{{ login_url }}\">{{ login_url }}</a></li>"
        "</ol>",
        "<p>Hello {{ name|e }},</p>"
        "<p>{{ inviter_name|e }} has invited you to their Plex server"
        "{% if libraries %} (libraries: {{ libraries|join(', ')|e }}){% endif %}"
        "{% if plan_name %}, plan <strong>{{ plan_name|e }}</strong>{% endif %}.</p>"
        "<p>The invite was sent to <strong>{{ email|e }}</strong>: use "
        "<strong>that same address</strong> at every step, or your access "
        "will not be recognised.</p>"
        "<p><strong>If you already have a Plex account</strong></p>"
        "<ol>"
        "<li>Open the email from Plex and accept the share "
        "(or go to <a href=\"https://app.plex.tv\">app.plex.tv</a>).</li>"
        "<li>Sign in here with your Plex account: "
        "<a href=\"{{ login_url }}\">{{ login_url }}</a></li>"
        "</ol>"
        "<p><strong>If you do not have a Plex account yet</strong></p>"
        "<ol>"
        "<li>Create a free account at <a href=\"https://www.plex.tv\">plex.tv</a>, "
        "using the address {{ email|e }}.</li>"
        "<li>Open the email from Plex and accept the share.</li>"
        "<li>Sign in here with your Plex account: "
        "<a href=\"{{ login_url }}\">{{ login_url }}</a></li>"
        "</ol>",
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
    return _env.from_string(src).render(**ctx).strip()


def render_email(session: Session, type_: str, locale: str, ctx: dict) -> tuple[str, str, str]:
    """Returns (subject, html, plain_text)."""
    subject = render_part(session, type_, "email_subject", locale, ctx)
    html = render_part(session, type_, "email_html", locale, ctx)
    text = _html_to_text(html) if html else ""
    return subject, html, text


def render_telegram(session: Session, type_: str, locale: str, ctx: dict) -> str:
    return render_part(session, type_, "telegram", locale, ctx)


def validate(src: str) -> str | None:
    """None if `src` renders against SAMPLE_CTX, else the error message."""
    try:
        _env.from_string(src).render(**SAMPLE_CTX)
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
