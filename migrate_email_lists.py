"""
Email List Migration Script - Issue 6 (Option A: minimal cleanup)

Goals:
1. Backfill `source: "manual"` on every existing email list JSON file
   that does not have a `source` field. This fixes the legacy-data half
   of the Pydantic ValidationError crash on GET /email-lists/{id}.

2. Build / refresh a small SQLite index at
   `data/email_lists_index.db` holding the searchable fields
   `{id, user_id, name, source, total_emails, valid_count,
   invalid_count, created_at, updated_at, file_path}` so we can answer
   "find list by name" / "count lists by source" without scanning every
   JSON file.

3. (Optional) Add a `Email` table index for the IMAP cache (the table
   is already defined in `database.py`; this migration just ensures it
   exists and has the right indexes).

Run:
    python migrate_email_lists.py
    python migrate_email_lists.py --dry-run     # show what would change
    python migrate_email_lists.py --user fv-s-06425d79-...   # one user

The script is idempotent. Re-running it is safe.
"""
import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---- Project imports (work both as a module and as a script) ----
try:
    from config import USERS_BASE_PATH  # type: ignore
except Exception:
    # Fall back to the conventional directory layout
    USERS_BASE_PATH = Path(__file__).resolve().parent / "users"

DATA_DIR = Path(__file__).resolve().parent / "data"
INDEX_DB_PATH = DATA_DIR / "email_lists_index.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("migrate_email_lists")


# ---------------------------------------------------------------------------
# Index schema
# ---------------------------------------------------------------------------

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS email_lists_index (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT,
    source          TEXT,
    total_emails    INTEGER DEFAULT 0,
    valid_count     INTEGER DEFAULT 0,
    invalid_count   INTEGER DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT,
    file_path       TEXT NOT NULL,
    indexed_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_eli_user
    ON email_lists_index(user_id);

CREATE INDEX IF NOT EXISTS idx_eli_source
    ON email_lists_index(source);

CREATE INDEX IF NOT EXISTS idx_eli_name
    ON email_lists_index(name);
"""


def open_index() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(INDEX_DB_PATH))
    conn.executescript(INDEX_SCHEMA)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# File walking
# ---------------------------------------------------------------------------

def find_user_folders(base: Path, only_user: Optional[str] = None) -> Iterable[Path]:
    """Yield user folder paths under USERS_BASE_PATH."""
    if not base.exists():
        logger.warning("USERS_BASE_PATH does not exist: %s", base)
        return
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if not child.name.startswith("fv-s-"):
            continue
        if only_user and child.name != only_user:
            continue
        yield child


def find_email_list_files(user_folder: Path) -> Iterable[Path]:
    """Yield email-list JSON files inside the user's emails/ folder."""
    emails_dir = user_folder / "emails"
    if not emails_dir.exists():
        return
    for f in sorted(emails_dir.iterdir()):
        if f.is_file() and f.suffix == ".json" and f.stem.startswith("elist_"):
            yield f


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------

def backfill_source(
    list_file: Path,
    dry_run: bool,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Backfill `source: "manual"` on a single email-list JSON file if missing.

    Returns (changed, payload-after).
    """
    try:
        with open(list_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Skipping invalid JSON %s: %s", list_file, e)
        return False, {}

    if not isinstance(data, dict):
        logger.warning("Skipping non-dict payload %s", list_file)
        return False, {}

    changed = False
    if "source" not in data or not data.get("source"):
        data["source"] = "manual"
        changed = True
    if "updated_at" not in data or not data.get("updated_at"):
        data["updated_at"] = datetime.utcnow().isoformat() + "Z"
        changed = True

    if changed and not dry_run:
        # Write back atomically: write to .tmp then rename.
        tmp_path = list_file.with_suffix(list_file.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_path.replace(list_file)

    return changed, data


def upsert_index_row(
    conn: sqlite3.Connection,
    list_id: str,
    user_id: str,
    data: Dict[str, Any],
    file_path: Path,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO email_lists_index
            (id, user_id, name, source, total_emails, valid_count,
             invalid_count, created_at, updated_at, file_path, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            list_id,
            user_id,
            data.get("name", ""),
            data.get("source", "manual"),
            int(data.get("total_emails", 0) or 0),
            int(data.get("valid_count", 0) or 0),
            int(data.get("invalid_count", 0) or 0),
            data.get("created_at", ""),
            data.get("updated_at", ""),
            str(file_path),
            datetime.utcnow().isoformat() + "Z",
        ),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill email list source field and build SQLite index"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Process only this user folder (e.g. fv-s-06425d79-a16b-4c4c-9f4a-b0f2ae988bd7)",
    )
    args = parser.parse_args()

    logger.info("USERS_BASE_PATH: %s", USERS_BASE_PATH)
    logger.info("INDEX_DB_PATH:   %s", INDEX_DB_PATH)
    if args.dry_run:
        logger.info("[DRY RUN] No files will be modified")

    conn = None if args.dry_run else open_index()
    try:
        total_scanned = 0
        total_changed = 0
        total_indexed = 0
        per_user: Dict[str, Dict[str, int]] = {}

        for user_folder in find_user_folders(USERS_BASE_PATH, only_user=args.user):
            user_id = user_folder.name
            user_stats = {"scanned": 0, "changed": 0, "indexed": 0}
            for list_file in find_email_list_files(user_folder):
                total_scanned += 1
                user_stats["scanned"] += 1
                changed, data = backfill_source(list_file, dry_run=args.dry_run)
                if changed:
                    total_changed += 1
                    user_stats["changed"] += 1
                    logger.info(
                        "  backfilled source: %s",
                        list_file.relative_to(USERS_BASE_PATH),
                    )
                if not args.dry_run and data:
                    list_id = data.get("id") or list_file.stem
                    upsert_index_row(conn, list_id, user_id, data, list_file)
                    total_indexed += 1
                    user_stats["indexed"] += 1
            per_user[user_id] = user_stats

        if conn is not None:
            conn.commit()
            cur = conn.execute("SELECT COUNT(*) FROM email_lists_index")
            index_count = cur.fetchone()[0]
            logger.info("Index now has %d rows", index_count)
        else:
            index_count = None

        logger.info("=" * 60)
        logger.info("Migration complete")
        logger.info("  users processed : %d", len(per_user))
        logger.info("  files scanned   : %d", total_scanned)
        logger.info("  files changed   : %d", total_changed)
        if index_count is not None:
            logger.info("  index rows      : %d", index_count)
        logger.info("=" * 60)

        if per_user:
            for uid, s in per_user.items():
                logger.info("  %-50s scanned=%d changed=%d", uid, s["scanned"], s["changed"])
        return 0
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
