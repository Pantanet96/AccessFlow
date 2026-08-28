# Changelog

All notable changes to AccessFlow are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-08-28

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

- Plex/Tautulli now show "AccessFlow" instead of "Linux" plus the container
  hostname. plexapi defaults its identifying headers to `uname()`, so every
  admin call made through `plex_service.py` was reporting the container rather
  than the app; the `PLEXAPI_HEADER_*` environment variables are now set at the
  image level, which covers `MyPlexAccount` and `PlexServer` too.

## [1.0.0]

- First release.
