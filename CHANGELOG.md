# Changelog

All notable changes to AccessFlow are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.3] - 2026-09-01

### Security

- Bumped `postcss` and `nanoid` (build-time only) past two high-severity advisories.
- Bumped `pytest` to 9.1.1, which fixes CVE-2025-71176 in its tmp_path handling.
- Dropped the unused `aiosmtplib` from local environments, where two of its CVEs still applied.

### Changed

- The asset stage now builds on Node 22 (Node 20 stopped getting security patches) and installs from the lockfile with `npm ci`, so the image no longer resolves a different transitive tree on every build.
- Bumped `cryptography`, `alembic`, `uvicorn`, `sqlmodel` and `pydantic-settings` to their current patch releases.

## [1.2.2] - 2026-09-01

### Fixed

- Withdrawing an invite now really removes it from plex.tv when the invitee has no Plex account yet.
- Re-inviting recovers from an invite plex.tv still holds open, instead of failing with "already sharing".

## [1.2.1] - 2026-09-01

### Fixed

- **Withdrawing an invite left it pending on plex.tv** whenever the invitee had
  not created a Plex account yet -- the common case, since the invite mail is
  what tells them to. plexapi's `pendingInvite()` only matches an invite whose
  `username` is set, and plex.tv leaves that empty until the address has an
  account, so `cancelInvite(email)` raised `NotFound`. The withdrawal swallowed
  it, cleared the `shared_servers` row and reported success, while the friend
  invite stayed pending. The invite is now looked up by email and cancelled by
  object, which skips plexapi's own lookup.

## [1.2.0] - 2026-09-01

### Added

- **Invite email.** Inviting someone now emails them as well as sharing the
  server on Plex, spelling out both ways in: accept the share and sign in
  (existing Plex account), or create a free Plex account with the invited
  address first (the common case for a new user). The copy warns that signing
  up under a different address breaks activation, since first sign-in matches
  the invite by email. It is a `invite` notification template like every other
  message — editable per language from Settings → Notification templates — and
  email-only, because an invitee has no Telegram link until after first sign-in.
- **"Resend email" on a pending invite**, for a message that bounced or landed
  in spam. It re-sends only the email; the Plex share is left alone.
- **The public address is configurable** in Settings → System, instead of being
  reachable only through the `PUBLIC_BASE_URL` env var. Plex sign-in and the
  invite link are built from it, so a deploy behind a reverse proxy needs it
  right; the page warns when it is still pointing at localhost. A blank value
  falls back to the env var, and cookies stay `Secure` when either says https,
  so a typo can't downgrade a working HTTPS deploy.
- **Invite emails appear in the notification history** with their send status,
  like every other message. `notification_log.user_id` is now nullable and
  carries `invite_id` instead — the invitee has no user account yet.

### Fixed

- **The audit log and the notification history now render in Italian.** Their
  labels are stored in a dict and translated at render (`_(LABELS[key])`), and
  `pybabel extract` only sees string literals sitting inside a gettext call — so
  56 of them never reached the catalog and fell back to English regardless of
  the chosen language. The same held for the subscription and renewal statuses
  templates translate as `_(value|capitalize)`. Marking each literal with the
  new `N_()` at its definition puts them in `messages.pot`; a test now fails if
  any of them goes missing from the Italian catalog again.
- **Re-inviting an address whose invite was withdrawn no longer dead-ends.** A
  share lives in two places on plex.tv — the friend invite and the server's
  `shared_servers` row — and withdrawing an invite only removed the first, with
  any failure swallowed silently. The local invite disappeared while plex.tv
  still held the share, so the next invite for that address came back
  `400 You're already sharing this server with <email>`. Withdrawing now sweeps
  the share list too, and inviting recovers from that 400 by editing the
  existing share (or dropping the stale row and inviting again).
- **A Plex withdrawal that fails is now reported.** The invite is still removed
  locally, but the page says Plex did not confirm it and the reason is recorded
  in the audit log, instead of the two sides drifting apart in silence.
- **List items no longer run together in the plain-text part of an email.** The
  HTML-to-text fallback only translated `<br>` and `</p>`, so stripping the tags
  glued every `<li>` to the next one. It shows worst on the new invite mail,
  whose numbered steps are the instructions the invitee has to follow, and it
  had been affecting the manager digest's per-user rows all along.

## [1.1.0] - 2026-08-28

### Added

- [Unraid Community Applications](unraid/accessflow.xml) template, plus the
  `ca_profile.xml` needed to submit the app to the CA repository.
- MIT [LICENSE](LICENSE).

### Changed

- **Users table: 10 columns down to 8, no more horizontal scrolling.** On a
  1920px display the table overflowed its container and the Actions buttons sat
  off-screen. Username now sits under the real name (the pairing the mobile card
  already used), and Expiry merged into Subscription — an expired subscription
  and an expired date were two columns saying the same thing.
- **Action buttons are icon-only.** The "Subscription" and "Delete" labels cost
  roughly 180px of table width on every row. `title` and `aria-label` are kept,
  so screen readers and tooltips still name them.
- **Real name column widened to 14rem** and the inline rename input no longer
  carries its own `min-width`, which was clipping longer names mid-word.
- **Click anywhere on a row to open that user's subscription.** Clicks on form
  controls (inline rename, role and manager selects, the action buttons) and
  text selection are excluded. The subscription icon button stays in the Actions
  cell: a `<tr>` is not focusable, so it remains the keyboard path.
- **Subscription and Access columns are centered**, with the date stacked over
  its badge instead of sitting beside it — the column only has to be as wide as
  the wider of the two.
- **Page max width raised from 1440px to 1760px**, so a 27" 1080p display uses
  the room it has once the 256px sidebar is subtracted.
- **Mobile card shows the Plex username** next to the role on the same line, so
  the card keeps its height. Accounts without a Plex handle (local logins) show
  just the role rather than a placeholder dash.
- Table cell padding tightened from `px-4` to `px-3`.

### Fixed

- **Plex and Tautulli showed "Linux" and the container hostname instead of
  "AccessFlow".** plexapi defaults its identifying headers to `uname()`, so every
  admin call made through `plex_service.py` reported the container rather than
  the app. The `PLEXAPI_HEADER_*` variables are now set at the image level, which
  covers `MyPlexAccount` and `PlexServer` too — not only the manual httpx calls
  in `plex_oauth.py`, which were already correct.
- **Elements with the `hidden` attribute stayed visible when they also carried a
  display utility class.** Tailwind's preflight `[hidden]{display:none}` and
  `.flex{display:flex}` have equal specificity, so `.flex` won on source order —
  the users list showed "No users match your filters" while listing users. Now
  `[hidden]` is `!important`, as the HTML spec's own suggested rendering has it.
  This applies app-wide, not just to the users list.
- **The footer was hidden underneath the fixed mobile navigation bar.** The
  clearance padding was on `<main>`, but the footer is its sibling, not its
  child; the padding now sits on the element that contains both.

### Removed

- The `responsive` class and `data-label` attributes on the users table. That
  table is `hidden md:block` and the mobile viewport gets the card list instead,
  so the stacked-card CSS could never fire. The CSS itself stays — Plans,
  Reports, Requests, Invites, Index and Subscriptions still use it.

## [1.0.1]

### Added

- OCI image labels (`title`, `description`, `source`) on the Docker image, so
  registries and tooling can identify and link back to the project.
- README screenshots, including the selectable color themes.

### Changed

- The application version reported in the footer is now kept in sync with the
  release tag.

## [1.0.0]

First public release. Self-hosted Plex access and subscription manager:
user and library management, subscription plans with expiry and renewals,
payment collection, email and Telegram notifications, invites, broadcasts,
reports and an audit log.

[1.2.3]: https://github.com/Pantanet96/AccessFlow/releases/tag/v1.2.3
[1.2.2]: https://github.com/Pantanet96/AccessFlow/releases/tag/v1.2.2
[1.2.1]: https://github.com/Pantanet96/AccessFlow/releases/tag/v1.2.1
[1.2.0]: https://github.com/Pantanet96/AccessFlow/releases/tag/v1.2.0
[1.1.0]: https://github.com/Pantanet96/AccessFlow/releases/tag/v1.1.0
[1.0.1]: https://github.com/Pantanet96/AccessFlow/releases/tag/v1.0.1
[1.0.0]: https://github.com/Pantanet96/AccessFlow/releases/tag/v1.0.0
