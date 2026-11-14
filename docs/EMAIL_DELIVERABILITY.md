# Email deliverability (SPF, DKIM, DMARC)

Practical companion to the SMTP settings in [.env.example](../.env.example) and
**Settings → Notifications**: the DNS side needed so the reminders, invites and
digests AccessFlow sends actually reach the inbox.

## Why this matters

Without SPF, DKIM and DMARC published for your sending domain, receiving
providers (Gmail, Outlook, Yahoo...) have no way to confirm a notification
really comes from you — and unverified mail defaults to spam. The send still
looks successful in AccessFlow (test button green, nothing in the logs); the
provider just drops or spam-folders it on the way in. The three records below,
published on the domain behind `SMTP_FROM`, fix that.

## SPF

Sender Policy Framework: a DNS TXT record on your domain listing which servers
are allowed to send mail as that domain. The receiving server checks the
connecting IP against it.

```
yourdomain.tld.   TXT   "v=spf1 include:_spf.yourprovider.tld ~all"
```

The `include:` depends on your relay:

| Relay | Include |
|---|---|
| SendGrid | `include:sendgrid.net` |
| Mailgun | `include:mailgun.org` |
| Brevo (Sendinblue) | `include:spf.brevo.com` |
| Amazon SES | `include:amazonses.com` |
| Own server, static IP | `ip4:203.0.113.10` (no `include:`) |

**Common mistake: two SPF records.** A domain may publish only **one** `v=spf1`
TXT record. If you already have one (e.g. for Google Workspace) and add a
second for AccessFlow's relay, SPF breaks entirely — validators see two
records and return `permerror`, which most providers treat as a fail. Merge
them into a single record instead:

```
v=spf1 include:_spf.google.com include:sendgrid.net ~all
```

## DKIM

DomainKeys Identified Mail: the sending server signs every message with a
private key; the receiving server checks the signature against a public key
you publish in DNS. It proves the message wasn't altered and came from a
server allowed to sign for your domain.

You don't generate anything yourself: your SMTP provider (SendGrid, Mailgun,
Brevo, your hoster...) generates the key pair and gives you the TXT record to
publish, shaped like:

```
selector._domainkey.yourdomain.tld.   TXT   "v=DKIM1; k=rsa; p=MIGfMA0GCSq..."
```

`selector` and the key are provider-specific — use their "domain
authentication" / "verify a sending domain" page, it hands you the exact
record (some, like SendGrid or Mailgun, use a couple of CNAMEs instead of a
raw TXT; same purpose).

Verify it with a test send (Settings → Notifications → "Send test email") to
mail-tester.com, or look for `dkim=pass` in the received message's
`Authentication-Results` header (see "How to check" below).

## DMARC

Tells receiving servers what to do when a message fails SPF/DKIM alignment,
and where to send aggregate reports of who's sending mail as your domain.

Start in monitoring mode — it changes nothing, just collects data:

```
_dmarc.yourdomain.tld.   TXT   "v=DMARC1; p=none; rua=mailto:dmarc@yourdomain.tld; fo=1"
```

Read the `rua` reports for a couple of weeks to confirm nothing legitimate is
failing alignment, then tighten the policy in steps:

```
v=DMARC1; p=quarantine; rua=mailto:dmarc@yourdomain.tld; pct=100
```

```
v=DMARC1; p=reject; rua=mailto:dmarc@yourdomain.tld
```

`p=quarantine` tells providers to file failing mail as spam; `p=reject` tells
them to refuse it. Don't jump straight to `p=reject` — read the `p=none`
reports first, a mistake there silently kills every email from the domain,
not just AccessFlow's.

## From alignment

The part that actually breaks things for self-hosters: **`SMTP_FROM` must be
an address on a domain you control** — the same one carrying the SPF/DKIM/DMARC
records above.

Set `SMTP_FROM=you@gmail.com` (or any other free-mail provider) while relaying
through a different SMTP server — your hoster, SendGrid, Mailgun — and:

- **SPF fails** — gmail.com's SPF record doesn't list your relay's IP.
- **DKIM fails or is missing** — your relay doesn't hold Gmail's private DKIM
  key, so it can't sign as `gmail.com`.
- **Gmail's own DMARC policy** then tells the receiving server to reject or
  spam-file the message, because neither SPF nor DKIM aligns with the From
  domain.

AccessFlow detects this: **Settings → Notifications** shows a warning when the
From domain is a known free-mail provider (Gmail, Outlook, Yahoo, iCloud,
Libero...) and the configured SMTP host isn't that provider's own server —
exactly the broken combination above. If you genuinely send through that
provider's own SMTP (e.g. `smtp.gmail.com` with a matching `@gmail.com` From
and an app password), there's nothing to fix; the warning only fires on the
mismatch.

Leaving `SMTP_FROM` empty does not avoid this: with no From address the
messages go out as the SMTP username, so a Gmail login relayed elsewhere is
the same broken combination. The warning reads whichever of the two actually
reaches the header.

Fix: use an address on a domain you own, e.g. `notifications@yourdomain.tld`,
with SPF/DKIM/DMARC published for that domain and covering whichever relay you
actually send through.

## One-click unsubscribe

Every notification email to a registered user carries a `List-Unsubscribe`
header, plus `List-Unsubscribe-Post` for one-click (RFC 8058). Gmail and Yahoo
require both from bulk senders and use them to show their own "Unsubscribe"
button next to the sender. Without it a reader who wants out hits "Report
spam" instead, and that does far more damage to the sending domain's
reputation than a lost subscriber. The message footer also carries a visible
link to the same page, for clients that show no button.

There is nothing to configure, but two things must be true:

- **`PUBLIC_BASE_URL` must be your real public address.** While it is still
  the local default no header is emitted at all: a link to `localhost` is
  worse than none.
- **It must be `https`** for the provider's one-click button to appear. On
  plain `http` only the simple `List-Unsubscribe` header goes out, which
  providers treat as an ordinary link.

The one-click button works by the provider POSTing to the link from its own
servers, so the instance must be reachable from the internet. On a LAN-only
instance the link still works from the browser of anyone on that network.
Your relay must also DKIM-sign these headers, which most do by default; if it
doesn't, providers ignore them.

What the user sees: the link opens a confirmation page, and nothing changes
until they press the button (a plain GET never unsubscribes, so mail scanners
that prefetch links cannot do it by accident). It turns off **email**
notifications only; Telegram is untouched. The user can switch email back on
from their profile. The link is signed, has no expiry, and is tied to the
address it was sent to: if the user changes their notification email, older
links stop working. They also stop working if you rotate `APP_SECRET_KEY`,
which signs them: messages already delivered then lead to a "link no longer
valid" page, and the user manages notifications from their profile instead.

Messages that carry it: reminders, follow-ups, the welcome email, the manager
digest and broadcasts. Messages that don't: invites (the recipient has no
account yet) and the SMTP test email.

## How to check

- **mail-tester.com** — send a test email (Settings → Notifications → "Send
  test email") to the address it gives you, then open the report: SPF, DKIM
  and DMARC are scored separately from general spam signals.
- **`Authentication-Results` header** — open the raw source of a received
  notification ("Show original" in Gmail) and look for:
  ```
  Authentication-Results: mx.google.com;
     spf=pass smtp.mailfrom=notifications@yourdomain.tld;
     dkim=pass header.i=@yourdomain.tld;
     dmarc=pass header.from=yourdomain.tld
  ```
  All three should say `pass`; anything else points at the matching section
  above.
- **`List-Unsubscribe` headers** — in the same raw source, a reminder should
  show `List-Unsubscribe: <https://your-host/unsubscribe/...>` and
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. If both are missing,
  check that `PUBLIC_BASE_URL` is set; if only the second is missing, it is
  not `https`.
- **`dig TXT`** — check what's actually published, since typos and stale
  caches are the usual culprit:
  ```bash
  dig TXT yourdomain.tld +short                        # SPF
  dig TXT _dmarc.yourdomain.tld +short                 # DMARC
  dig TXT selector._domainkey.yourdomain.tld +short    # DKIM (selector from your provider)
  ```

## Common setups

| Setup | SPF / DKIM | Typical gotcha |
|---|---|---|
| Provider relay (SendGrid, Mailgun, Brevo, SES) | Provider's dashboard walks you through both records | Forgetting to re-verify the domain after changing `SMTP_FROM` |
| Your hoster's own SMTP | Hoster's mail servers are usually pre-covered by SPF; DKIM may need manual setup in their panel | `SMTP_FROM` on a domain the hoster's SPF doesn't actually cover |
| Gmail SMTP (`smtp.gmail.com`) | Already aligned, but only with a matching `@gmail.com`/Workspace From | Needs an app password (2FA account); fine for testing, not meant for volume |

## Reply-To and send pacing

Two more SMTP variables, not about DNS but relevant here:

- **`SMTP_REPLY_TO`** — where a reply to a notification lands. Empty = the
  `SMTP_FROM` address; set it to redirect replies elsewhere (e.g. a shared
  support inbox).
- **`SMTP_SEND_DELAY`** — seconds between two messages in the same bulk run
  (the daily expiry scan, manager digests, a broadcast). `0` disables it.
  Raise it only if your relay throttles bursts and you see failures or
  rate-limiting during a run with many recipients: `1`-`5` is the useful
  range, and values above `10` are capped at `10` so a figure meant as
  "messages per minute" cannot stall the scheduler for hours.

  Each run already reuses a single SMTP connection for every message it sends,
  so this is a second line of defence, not the main one.
