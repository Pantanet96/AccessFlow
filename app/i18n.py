"""Minimal i18n: locale detection + gettext, per-request via contextvar.

Catalogs live in app/translations/<locale>/LC_MESSAGES/messages.mo.
Until catalogs are compiled, NullTranslations returns the source string,
so the app works with English source strings out of the box.

Uses stdlib gettext, not babel.support: Babel is only needed at build time to
compile .po -> .mo (pybabel), and keeping it out of the runtime image drops
~33MB of CLDR locale-data the app never reads.
"""
import contextvars
import gettext as _gettext
from pathlib import Path

from app.config import get_settings

LOCALES = ("it", "en")
_TRANS_DIR = Path(__file__).resolve().parent / "translations"
_current_locale: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "locale", default=None
)
_cache: dict[str, _gettext.NullTranslations] = {}


def _load(locale: str) -> _gettext.NullTranslations:
    if locale not in _cache:
        # fallback=True -> NullTranslations (source strings) if the .mo is missing.
        _cache[locale] = _gettext.translation(
            "messages", str(_TRANS_DIR), languages=[locale], fallback=True
        )
    return _cache[locale]


def detect_locale(request) -> str:
    query = request.query_params.get("locale")
    if query in LOCALES:
        return query
    cookie = request.cookies.get("locale")
    if cookie in LOCALES:
        return cookie
    accept = request.headers.get("accept-language", "")
    for part in accept.split(","):
        code = part.split(";")[0].strip()[:2].lower()
        if code in LOCALES:
            return code
    return get_settings().default_locale


def set_current_locale(locale: str) -> None:
    _current_locale.set(locale)


def get_current_locale() -> str:
    return _current_locale.get() or get_settings().default_locale


def gettext(message: str) -> str:
    return _load(get_current_locale()).gettext(message)


def install_jinja_i18n(templates) -> None:
    templates.env.globals["_"] = gettext
    templates.env.globals["get_locale"] = get_current_locale
    templates.env.globals["locales"] = LOCALES
