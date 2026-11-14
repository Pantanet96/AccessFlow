"""Building blocks an email template can call: button, notice, rows, facts.

Exposed as globals on the sandboxed template environment, so an admin writes
`{{ button(public_url, "Richiedi un film") }}` instead of hand-rolling table
markup they have no way to test. Each one returns finished HTML with its colours
and spacing already inline, because a mail client keeps what sits on the tag and
may drop everything else.

Every value that reaches the output is escaped here: the environment renders
with autoescape OFF (the HTML parts need markup through), so these functions
cannot lean on it.

The accent follows the instance's own colour theme, picked in Settings. Each
entry was checked against the surface it sits on: `link` and `button` carry
white text or sit on white at 16px, so they clear 4.5:1; `accent` is only ever
18px bold, which needs 3:1; `dark` is read on the dark card.
"""
import html as _h

_THEME_COLOURS = {
    "rame": {"accent": "#B5652E", "link": "#A0541F", "dark": "#E08A4C"},
    "inchiostro": {"accent": "#565B85", "link": "#4A4F75", "dark": "#9BA0D0"},
    "muschio": {"accent": "#5F7040", "link": "#55663A", "dark": "#A8B884"},
}
_FALLBACK = _THEME_COLOURS["rame"]


def missing_themes() -> set:
    """Themes the UI offers that this table has no colours for.

    Adding a fourth theme in runtime_config without one here would make
    every email quietly fall back to the default accent."""
    from app import runtime_config  # lazy: avoid an import cycle

    return set(runtime_config.COLOR_THEMES) - set(_THEME_COLOURS)

# Tones for notice(). Fixed rather than themed: "this is about to be cut off"
# has to read the same whatever accent the instance picked.
_TONES = {
    "warn": {"bg": "#FDF4E8", "line": "#C17A3E", "fg": "#6B4A1F"},
    "stop": {"bg": "#FBEDEB", "line": "#B04434", "fg": "#7A2B22"},
    "info": {"bg": "#F2F3F5", "line": "#9AA0A6", "fg": "#41464C"},
}

_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
         "Helvetica,Arial,sans-serif")


def palette() -> dict:
    """Accent colours for the instance's current theme."""
    from app import runtime_config  # lazy: avoid an import cycle at module load

    return _THEME_COLOURS.get(runtime_config.color_theme(), _FALLBACK)


def _esc(value) -> str:
    return _h.escape("" if value is None else str(value), quote=True)


def _url(value) -> str:
    """Only http(s) reaches an href: a template variable could otherwise carry
    a javascript: or data: URL into a link an admin never inspected."""
    raw = ("" if value is None else str(value)).strip()
    low = raw.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return ""
    return _h.escape(raw, quote=True)


def button(url, label) -> str:
    """The one action the message is asking for. Empty when the URL is unusable,
    so a template that calls it with an unset variable shows nothing rather than
    a dead button."""
    href = _url(url)
    if not href:
        return ""
    c = palette()
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        ' style="margin:22px 0 6px;"><tr>'
        f'<td align="center" bgcolor="{c["link"]}" style="border-radius:8px;'
        f' mso-padding-alt:13px 26px; background-color:{c["link"]};">'
        f'<a href="{href}" style="display:inline-block; padding:13px 26px;'
        f' font-family:{_FONT}; font-size:16px; line-height:20px; font-weight:600;'
        ' color:#FFFFFF; text-decoration:none; border-radius:8px;">'
        f'{_esc(label)}</a></td></tr></table>'
    )


def notice(text, tone: str = "warn") -> str:
    """A short line that needs to stand out from the paragraph around it."""
    t = _TONES.get(tone, _TONES["warn"])
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        ' width="100%" class="af-notice" style="margin:18px 0;"><tr>'
        f'<td bgcolor="{t["bg"]}" style="background-color:{t["bg"]};'
        f' border-left:3px solid {t["line"]}; border-radius:0 6px 6px 0;'
        f' padding:12px 14px; font-family:{_FONT}; font-size:15px;'
        f' line-height:22px; mso-line-height-rule:exactly; color:{t["fg"]};">'
        f'{_esc(text)}</td></tr></table>'
    )


def facts(*pairs) -> str:
    """Label/value lines in a quiet panel: the figures a message is about.

    Called as facts("Piano", plan_name, "Scadenza", expiry) -- flat arguments
    rather than a list, because a template author should not have to build a
    Python structure to show two rows.
    """
    items = [(pairs[i], pairs[i + 1]) for i in range(0, len(pairs) - 1, 2)]
    if not items:
        return ""
    out = [
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        ' width="100%" class="af-panel" bgcolor="#F6F5F2"'
        ' style="background-color:#F6F5F2; border-radius:8px; margin:18px 0;">'
    ]
    for n, (label, value) in enumerate(items):
        pad_top = "12px" if n == 0 else "4px"
        pad_bottom = "12px" if n == len(items) - 1 else "4px"
        out.append(
            f'<tr><td style="padding:{pad_top} 14px {pad_bottom};'
            f' font-family:{_FONT}; font-size:14px; line-height:20px;'
            ' mso-line-height-rule:exactly;">'
            f'<span class="af-dim" style="color:#6B6557;">{_esc(label)}</span>'
            '<br />'
            f'<strong class="af-strong" style="font-size:15px;'
            f' color:#1F1C16;">{_esc(value)}</strong>'
            '</td></tr>'
        )
    out.append("</table>")
    return "".join(out)


# A hard ceiling on how many entries reach the markup. Each one costs a few
# hundred bytes of inline style, and Gmail clips a message past ~102KB with a
# "message truncated" link -- the totals at the bottom are the first thing to
# disappear. A template is expected to slice and say "and N more"; this is the
# backstop for one that does not.
MAX_ROWS = 60


def rows(items, fmt_date=None, more_label=None) -> str:
    """The digest, one block per subscription.

    Two stacked lines per entry instead of one long one: at phone width a
    single line wraps wherever it runs out of room, which puts the amount
    under the plan name and makes the list unreadable exactly where it is
    read most.

    The font is declared once on the outer table rather than on each cell:
    repeated down a few hundred rows, that one declaration was most of the
    message.
    """
    from app.services.notification_templates import _money  # lazy: cycle

    try:
        seq = list(items)
    except TypeError:
        return ""
    if not seq:
        return ""
    hidden = max(0, len(seq) - MAX_ROWS)
    seq = seq[:MAX_ROWS]
    out = [
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        f' width="100%" style="margin:16px 0; font-family:{_FONT};">'
    ]
    for n, it in enumerate(seq):
        get = it.get if hasattr(it, "get") else (lambda k, d="": d)
        try:
            overdue = int(get("days_left", "")) < 0
        except (TypeError, ValueError):
            overdue = False
        when = get("expiry_date", "")
        if fmt_date is not None:
            when = fmt_date(when)
        detail = (f'{_esc(get("plan_name", ""))} &middot; '
                  f'{_esc(when)}')
        top = "0" if n == 0 else "1px solid #ECE9E3"
        out.append(
            f'<tr><td class="af-rowline" style="border-top:{top};'
            ' padding:11px 0;">'
            '<table role="presentation" cellpadding="0" cellspacing="0"'
            f' border="0" width="100%" style="font-family:{_FONT};"><tr>'
            '<td class="af-strong" style="font-size:15px; line-height:21px;'
            ' mso-line-height-rule:exactly; font-weight:600; color:#1F1C16;">'
            f'{_esc(get("user_name", ""))}</td>'
            '<td align="right" class="af-strong" style="font-size:15px;'
            ' line-height:21px; mso-line-height-rule:exactly; font-weight:600;'
            ' white-space:nowrap; padding-left:10px; color:#1F1C16;">'
            f'{_esc(_money(get("amount_eur", "")))}</td>'
            '</tr><tr>'
            f'<td colspan="2" class="{"af-late" if overdue else "af-dim"}"'
            ' style="font-size:13px; line-height:19px;'
            ' mso-line-height-rule:exactly; padding-top:2px; color:'
            f'{"#B04434" if overdue else "#6B6557"};">{detail}</td>'
            '</tr></table></td></tr>'
        )
    if hidden:
        # The caller supplies the wording, since this module has no locale.
        # A template that gets the placeholder wrong still sends: the count
        # alone is poor, but it beats raising in the middle of a run.
        try:
            label = (more_label or "+ %d") % hidden
        except (TypeError, ValueError):
            label = "+ %d" % hidden
        out.append(
            '<tr><td class="af-rowline af-dim" style="border-top:1px solid'
            ' #ECE9E3; padding:11px 0; font-size:13px; line-height:19px;'
            f' mso-line-height-rule:exactly; color:#6B6557;">{_esc(label)}'
            '</td></tr>'
        )
    out.append("</table>")
    return "".join(out)
