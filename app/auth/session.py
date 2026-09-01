"""Signed session cookie (itsdangerous) holding the user id."""
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import runtime_config
from app.config import get_settings

COOKIE_NAME = "pum_session"
MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().app_secret_key, salt="pum-session")


def make_token(user_id: int, gen: int = 0) -> str:
    return _serializer().dumps({"uid": user_id, "gen": gen})


def read_token(token: str) -> tuple[int, int] | None:
    """Return (user_id, session_gen) from a valid token, else None."""
    try:
        data = _serializer().loads(token, max_age=MAX_AGE)
        return int(data["uid"]), int(data.get("gen", 0))
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def sign_value(data: dict, salt: str = "pum-state") -> str:
    return URLSafeTimedSerializer(get_settings().app_secret_key, salt=salt).dumps(data)


def read_value(token: str, salt: str = "pum-state", max_age: int = 600) -> dict | None:
    try:
        return URLSafeTimedSerializer(
            get_settings().app_secret_key, salt=salt
        ).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None


def set_session_cookie(response, user) -> None:
    response.set_cookie(
        COOKIE_NAME,
        make_token(user.id, getattr(user, "session_gen", 0) or 0),
        max_age=MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=runtime_config.cookies_secure(),
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME)
