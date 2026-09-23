"""Consistent SQLite backups using the online backup API, with retention."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings


def backup_database() -> Path | None:
    settings = get_settings()
    src = Path(settings.database_path)
    if not src.exists():
        return None
    backups_dir = src.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    # UTC + microseconds: local time can jump backwards (DST / tz change) and
    # produce out-of-order names, and a whole-second stamp collides if two run in
    # the same second — either way retention (below) would drop the wrong file.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dest = backups_dir / f"app-{stamp}.db"

    # Write under a name retention doesn't match; a failed run (disk full, db
    # locked) used to leave a truncated app-*.db that pushed out a good backup.
    part = dest.with_suffix(".db.part")
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(part))
    try:
        with target:
            source.backup(target)
    except BaseException:
        target.close()
        part.unlink(missing_ok=True)
        raise
    finally:
        target.close()
        source.close()
    part.replace(dest)

    # Retain the newest `keep` by actual modification time (not filename order),
    # so retention stays correct even if names ever sort oddly.
    keep = max(1, settings.backup_keep)
    existing = sorted(
        backups_dir.glob("app-*.db"), key=lambda p: p.stat().st_mtime
    )
    for old in existing[:-keep]:
        old.unlink()
    return dest
