"""SMTP email delivery (stdlib smtplib). No-op when SMTP is not configured."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app import runtime_config


def send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    c = runtime_config.smtp_config()
    if not c["host"] or not to:
        return False
    msg = EmailMessage()
    addr = c["from_addr"] or c["user"]
    # formataddr(("", addr)) -> bare addr; with a name -> "Name <addr>"
    msg["From"] = formataddr((c.get("from_name") or "", addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)  # plain-text part (fallback)
    if html:
        msg.add_alternative(html, subtype="html")  # multipart/alternative
    ctx = ssl.create_default_context()
    # ponytail: port 465 = implicit TLS (SMTPS); a setting only if some server
    # ever needs SMTPS on another port.
    if c["port"] == 465:
        server = smtplib.SMTP_SSL(c["host"], c["port"], timeout=15, context=ctx)
    else:
        server = smtplib.SMTP(c["host"], c["port"], timeout=15)
    with server:
        if c["tls"] and not isinstance(server, smtplib.SMTP_SSL):
            # Verify the server certificate + hostname: the bare starttls() uses an
            # unverified context, so an on-path attacker could MITM the connection
            # and capture the SMTP login credentials below.
            server.starttls(context=ctx)
        if c["user"]:
            server.login(c["user"], c["password"])
        server.send_message(msg)
    return True
