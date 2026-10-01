"""
SQLite database module for test results storage and analysis.
Provides functionality to store and query test runs, test cases, and user metadata.
"""

import sqlite3
import json
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from contextlib import asynccontextmanager
import aiosqlite

UTC = timezone.utc


@dataclass
class TestRunData:  # pytest: disable=collection
    __test__ = False  # Tell pytest to ignore this class
    """Represents a test run in the database."""
    run_id: str
    status: str
    start_time: str
    end_time: Optional[str]
    retention_days: Optional[int]
    local_run: bool
    dut: str = "TestDevice-001"
    run_name: Optional[str] = None
    target_key: str = ""
    purpose: str = "manual"
    parent_run_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class TestCaseData:  # pytest: disable=collection
    __test__ = False  # Tell pytest to ignore this class
    """Represents a test case in the database."""
    id: int
    run_id: str
    tc_full_name: str
    tc_id: Optional[str]
    status: str
    start_time: str
    end_time: Optional[str]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class UserMetadata:
    """Represents user metadata for a test run."""
    id: int
    run_id: str
    key: str
    value: str
    url: Optional[str] = None
    created_at: Optional[str] = None


class TestResultsDatabase:
    """SQLite database for test results storage and analysis."""

    def __init__(self, db_path: str = "test_results.db"):
        self.db_path = db_path
        self._initialized = False

    async def initialize(self):
        """Initialize the database with required tables."""
        if self._initialized:
            return

        async with aiosqlite.connect(self.db_path) as db:
            # Enable foreign key constraints
            await db.execute("PRAGMA foreign_keys = ON")

            # Fresh Target/Collection schema. This project intentionally has no
            # migration path from the retired group-based database.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    setup_state TEXT NOT NULL CHECK (setup_state IN ('needs_setup', 'ready')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    description TEXT,
                    ai_summary_enabled BOOLEAN NOT NULL DEFAULT 0,
                    email_enabled BOOLEAN NOT NULL DEFAULT 0,
                    recipients_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS collection_targets (
                    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
                    PRIMARY KEY (collection_id, target_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS summary_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    is_primary BOOLEAN NOT NULL DEFAULT 0,
                    purpose TEXT NOT NULL,
                    window_hours INTEGER NOT NULL CHECK (window_hours > 0),
                    selection_policy TEXT NOT NULL CHECK (selection_policy = 'latest-completed-per-target'),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (collection_id, name)
                )
            """)
            await db.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_profiles_one_primary
                ON summary_profiles(collection_id) WHERE is_primary = 1
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS summary_profile_sources (
                    profile_id INTEGER NOT NULL REFERENCES summary_profiles(id) ON DELETE CASCADE,
                    source_role TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    target_id INTEGER REFERENCES targets(id) ON DELETE CASCADE,
                    PRIMARY KEY (profile_id, source_role, target_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS test_runs (
                    run_id TEXT PRIMARY KEY,
                    target_key TEXT NOT NULL REFERENCES targets(key),
                    purpose TEXT NOT NULL,
                    parent_run_id TEXT REFERENCES test_runs(run_id),
                    status TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    retention_days INTEGER,
                    local_run BOOLEAN NOT NULL DEFAULT 0,
                    dut TEXT NOT NULL DEFAULT 'TestDevice-001',
                    run_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Create test_cases table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS test_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    tc_full_name TEXT NOT NULL,
                    tc_id TEXT,
                    status TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES test_runs (run_id) ON DELETE CASCADE,
                    UNIQUE (run_id, tc_full_name)
                )
            """)

            # Create user_metadata table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    url TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES test_runs (run_id) ON DELETE CASCADE,
                    UNIQUE (run_id, key)
                )
            """)

            # Create indexes for better query performance
            await db.execute("CREATE INDEX IF NOT EXISTS idx_test_runs_status ON test_runs (status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_test_runs_start_time ON test_runs (start_time)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_test_runs_target_purpose ON test_runs (target_key, purpose)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_test_cases_run_id ON test_cases (run_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_test_cases_status ON test_cases (status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_user_metadata_run_id ON user_metadata (run_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_user_metadata_key ON user_metadata (key)")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS run_sources (
                    run_id TEXT NOT NULL REFERENCES test_runs(run_id) ON DELETE CASCADE,
                    source_role TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    repository_url TEXT,
                    dirty BOOLEAN,
                    PRIMARY KEY (run_id, source_role)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_run_sources_branch ON run_sources (source_role, branch)")

            # Commit details remain in per-Run artifacts; revision ownership is run_sources.
            # This table preserves commit/file-level details without duplicating current SHA state.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS run_commits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    repo_name TEXT NOT NULL,
                    commit_sha TEXT,
                    repo_url TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES test_runs (run_id) ON DELETE CASCADE,
                    UNIQUE (run_id, repo_name)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_run_commits_run_id ON run_commits (run_id)")

            # AI analysis tables
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ai_analyses (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint     TEXT NOT NULL,
                    summary         TEXT NOT NULL,
                    summary_html    TEXT,
                    references_json TEXT,
                    confidence      REAL NOT NULL,
                    category        TEXT NOT NULL,
                    model_used      TEXT NOT NULL,
                    tier_used       INTEGER NOT NULL,
                    reasoning       TEXT,
                    deep_html       TEXT,
                    context_hash    TEXT NOT NULL,
                    token_count     INTEGER,
                    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(fingerprint, context_hash)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_analyses_fingerprint ON ai_analyses(fingerprint)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS collection_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                    profile_id INTEGER NOT NULL REFERENCES summary_profiles(id) ON DELETE CASCADE,
                    requested_at TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    summary TEXT,
                    model_used TEXT,
                    prompt_version TEXT,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(collection_id, profile_id, requested_at)
                )
            """)

            # Ensure new columns exist for legacy databases
            cursor = await db.execute("PRAGMA table_info(ai_analyses)")
            columns = await cursor.fetchall()
            ai_col_names = {col[1] for col in columns}
            if "summary_html" not in ai_col_names:
                await db.execute("ALTER TABLE ai_analyses ADD COLUMN summary_html TEXT")
            if "deep_html" not in ai_col_names:
                await db.execute("ALTER TABLE ai_analyses ADD COLUMN deep_html TEXT")

            cursor = await db.execute("PRAGMA table_info(run_commits)")
            columns = await cursor.fetchall()
            if "commit_sha" not in {col[1] for col in columns}:
                await db.execute("ALTER TABLE run_commits ADD COLUMN commit_sha TEXT")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS test_case_analyses (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id          TEXT NOT NULL,
                    tc_full_name    TEXT NOT NULL,
                    analysis_id     INTEGER NOT NULL REFERENCES ai_analyses(id),
                    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(run_id, tc_full_name)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tc_analyses_run_id ON test_case_analyses(run_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tc_analyses_analysis_id ON test_case_analyses(analysis_id)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS ai_usage (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    month               TEXT NOT NULL,
                    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
                    completion_tokens   INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd  REAL NOT NULL DEFAULT 0.0,
                    warning_sent        BOOLEAN NOT NULL DEFAULT 0,
                    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(month)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    display_name TEXT NOT NULL,
                    email TEXT,
                    username TEXT,
                    password_hash TEXT,
                    role TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    auth_source TEXT NOT NULL,
                    oidc_issuer TEXT,
                    oidc_subject TEXT,
                    created_at TEXT NOT NULL,
                    last_login_at TEXT
                )
            """)
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users (username) WHERE username IS NOT NULL"
            )
            await db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oidc
                   ON users (oidc_issuer, oidc_subject)
                   WHERE oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL"""
            )

            await db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions (user_id)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS login_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempted_at TEXT NOT NULL,
                    username TEXT,
                    client_ip TEXT,
                    username_existed INTEGER NOT NULL DEFAULT 0
                )
            """)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_login_attempts_username_time ON login_attempts (username, attempted_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts (client_ip, attempted_at)"
            )

            await db.execute("""
                CREATE TABLE IF NOT EXISTS oidc_states (
                    state TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    code_verifier TEXT NOT NULL,
                    next_url TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK (scope IN ('run', 'log')),
                    tc_id TEXT,
                    line_start INTEGER,
                    line_end INTEGER,
                    body TEXT NOT NULL,
                    author_user_id INTEGER,
                    author_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES test_runs (run_id) ON DELETE CASCADE
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_comments_run_id ON comments (run_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_comments_run_tc ON comments (run_id, tc_id)")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor = await db.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 1"
            )
            if await cursor.fetchone() is None:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS kpi_batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES test_runs(run_id) ON DELETE CASCADE,
                        source_key TEXT NOT NULL,
                        checksum_sha256 TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        source_type TEXT NOT NULL,
                        payload_size INTEGER NOT NULL,
                        sample_count INTEGER NOT NULL,
                        raw_json TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(run_id, source_key)
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS kpi_samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        batch_id INTEGER NOT NULL REFERENCES kpi_batches(id) ON DELETE CASCADE,
                        run_id TEXT NOT NULL REFERENCES test_runs(run_id) ON DELETE CASCADE,
                        test_case_id INTEGER REFERENCES test_cases(id) ON DELETE SET NULL,
                        test_name TEXT NOT NULL,
                        match_status TEXT NOT NULL CHECK (match_status IN ('matched', 'unmatched')),
                        metric_key TEXT NOT NULL,
                        value REAL NOT NULL,
                        unit TEXT NOT NULL,
                        dimensions_json TEXT NOT NULL,
                        sample_time TEXT,
                        ordinal INTEGER NOT NULL,
                        source_type TEXT NOT NULL,
                        provenance_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(batch_id, ordinal)
                    )
                """)
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kpi_samples_metric_run "
                    "ON kpi_samples(metric_key, unit, run_id)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kpi_samples_test_case "
                    "ON kpi_samples(test_case_id, sample_time)"
                )
                await db.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (1, ?)",
                    ("generic-kpi-samples",),
                )

            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_kpi_samples_metric_test_time "
                "ON kpi_samples(metric_key, unit, test_name, sample_time)"
            )
            await db.commit()

        self._initialized = True

    @asynccontextmanager
    async def get_connection(self):
        """Get a database connection with proper initialization."""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    async def get_or_create_target(self, key: str, display_name: Optional[str] = None) -> Dict[str, Any]:
        """Return a Target, creating an unconfigured Target when it is first reported."""
        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO targets (key, display_name, setup_state)
                   VALUES (?, ?, 'needs_setup')
                   ON CONFLICT(key) DO NOTHING""",
                (key, display_name or key),
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM targets WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return dict(zip([column[0] for column in cursor.description], row))

    async def list_targets(self, needs_setup_only: bool = False) -> List[Dict[str, Any]]:
        async with self.get_connection() as db:
            query = "SELECT * FROM targets"
            if needs_setup_only:
                query += " WHERE setup_state = 'needs_setup'"
            cursor = await db.execute(query + " ORDER BY key")
            return [dict(zip([column[0] for column in cursor.description], row)) for row in await cursor.fetchall()]

    async def get_target(self, key: str) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM targets WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return dict(zip([column[0] for column in cursor.description], row)) if row else None

    async def update_target(self, key: str, display_name: str, setup_state: str) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE targets SET display_name = ?, setup_state = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
                (display_name, setup_state, key),
            )
            await db.commit()
        return await self.get_target(key)

    async def complete_target_setup(self, key: str, display_name: str, collection_ids: List[int]) -> Optional[Dict[str, Any]]:
        """Atomically mark a Target ready and replace its Collection memberships."""
        if len(collection_ids) != len(set(collection_ids)):
            raise ValueError("Target membership cannot contain duplicate Collections")
        async with self.get_connection() as db:
            try:
                await db.execute("BEGIN")
                target_cursor = await db.execute("SELECT id FROM targets WHERE key = ?", (key,))
                target = await target_cursor.fetchone()
                if not target:
                    await db.rollback()
                    return None
                target_id = target[0]
                if collection_ids:
                    placeholders = ", ".join("?" for _ in collection_ids)
                    collection_cursor = await db.execute(f"SELECT id FROM collections WHERE id IN ({placeholders})", collection_ids)
                    if len(await collection_cursor.fetchall()) != len(collection_ids):
                        raise ValueError("Target membership contains an unknown Collection")
                await db.execute("UPDATE targets SET display_name = ?, setup_state = 'ready', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (display_name, target_id))
                await db.execute("DELETE FROM collection_targets WHERE target_id = ?", (target_id,))
                await db.executemany("INSERT INTO collection_targets (collection_id, target_id) VALUES (?, ?)", [(collection_id, target_id) for collection_id in collection_ids])
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.get_target(key)

    async def delete_target(self, key: str) -> bool:
        async with self.get_connection() as db:
            cursor = await db.execute("DELETE FROM targets WHERE key = ?", (key,))
            await db.commit()
            return cursor.rowcount == 1

    async def get_collection_keys_for_target(self, target_key: str) -> List[str]:
        """Return server-managed Collection keys for one Target."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """SELECT collections.key
                   FROM collections
                   JOIN collection_targets ON collection_targets.collection_id = collections.id
                   JOIN targets ON targets.id = collection_targets.target_id
                   WHERE targets.key = ?
                   ORDER BY collections.key""",
                (target_key,),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def get_run_ids_for_target(self, target_key: str) -> List[str]:
        """Return all visible Run IDs for a Target in deterministic order."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """SELECT run_id FROM test_runs
                   WHERE target_key = ? AND status != 'preparing'
                   ORDER BY start_time DESC, run_id DESC""",
                (target_key,),
            )
            return [row[0] for row in await cursor.fetchall()]

    async def create_collection(
        self,
        key: str,
        display_name: str,
        description: Optional[str] = None,
        ai_summary_enabled: bool = False,
        email_enabled: bool = False,
        recipients: Optional[List[str]] = None,
    ) -> int:
        """Create a Collection and return its stable database ID."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO collections
                   (key, display_name, description, ai_summary_enabled, email_enabled, recipients_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key, display_name, description, ai_summary_enabled, email_enabled, json.dumps(recipients or [])),
            )
            await db.commit()
            return cursor.lastrowid

    async def list_collections(self) -> List[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM collections ORDER BY key")
            return [self._collection_row(cursor, row) for row in await cursor.fetchall()]

    async def get_collection(self, key: str) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM collections WHERE key = ?", (key,))
            row = await cursor.fetchone()
            if not row:
                return None
            collection = self._collection_row(cursor, row)
            members = await db.execute(
                "SELECT targets.* FROM targets JOIN collection_targets ON collection_targets.target_id = targets.id WHERE collection_targets.collection_id = ? ORDER BY targets.key",
                (collection["id"],),
            )
            collection["targets"] = [dict(zip([column[0] for column in members.description], item)) for item in await members.fetchall()]
            profiles = await db.execute("SELECT * FROM summary_profiles WHERE collection_id = ? ORDER BY name", (collection["id"],))
            collection["profiles"] = [dict(zip([column[0] for column in profiles.description], item)) for item in await profiles.fetchall()]
            return collection

    async def get_collection_profile_filter_options(self, collection_id: int) -> Dict[str, List[Dict[str, Any]]]:
        """Return observed finished-run metadata for a Collection's profile builder."""
        async with self.get_connection() as db:
            purposes = await (
                await db.execute(
                    """SELECT runs.purpose, COUNT(DISTINCT runs.run_id) AS run_count
                       FROM test_runs AS runs
                       JOIN collection_targets ON collection_targets.target_id = (
                           SELECT id FROM targets WHERE key = runs.target_key
                       )
                       WHERE collection_targets.collection_id = ?
                         AND runs.status = 'finished' AND runs.purpose IS NOT NULL
                       GROUP BY runs.purpose ORDER BY run_count DESC, runs.purpose""",
                    (collection_id,),
                )
            ).fetchall()
            source_pairs = await (
                await db.execute(
                    """SELECT sources.source_role, sources.branch, COUNT(DISTINCT runs.run_id) AS run_count
                       FROM run_sources AS sources
                       JOIN test_runs AS runs ON runs.run_id = sources.run_id
                       JOIN collection_targets ON collection_targets.target_id = (
                           SELECT id FROM targets WHERE key = runs.target_key
                       )
                       WHERE collection_targets.collection_id = ?
                         AND runs.status = 'finished'
                         AND sources.source_role != '' AND sources.branch != ''
                       GROUP BY sources.source_role, sources.branch
                       ORDER BY run_count DESC, sources.source_role, sources.branch""",
                    (collection_id,),
                )
            ).fetchall()
            return {
                "purposes": [{"value": row[0], "run_count": row[1]} for row in purposes],
                "source_pairs": [
                    {"source_role": row[0], "branch": row[1], "run_count": row[2]}
                    for row in source_pairs
                ],
            }

    @staticmethod
    def _collection_row(cursor: Any, row: Any) -> Dict[str, Any]:
        collection = dict(zip([column[0] for column in cursor.description], row))
        collection["recipients"] = json.loads(collection.pop("recipients_json"))
        return collection

    async def update_collection(self, key: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute(
                """UPDATE collections SET display_name = ?, description = ?, ai_summary_enabled = ?,
                   email_enabled = ?, recipients_json = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?""",
                (values["display_name"], values.get("description"), values.get("ai_summary_enabled", False), values.get("email_enabled", False), json.dumps(values.get("recipients", [])), key),
            )
            await db.commit()
            if cursor.rowcount != 1:
                return None
        return await self.get_collection(key)

    async def delete_collection(self, key: str) -> bool:
        async with self.get_connection() as db:
            cursor = await db.execute("DELETE FROM collections WHERE key = ?", (key,))
            await db.commit()
            return cursor.rowcount == 1

    async def get_summary_profile(self, profile_id: int) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM summary_profiles WHERE id = ?", (profile_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            profile = dict(zip([column[0] for column in cursor.description], row))
            selectors = await db.execute("SELECT source_role, branch, target_id FROM summary_profile_sources WHERE profile_id = ?", (profile_id,))
            profile["selectors"] = [dict(zip([column[0] for column in selectors.description], item)) for item in await selectors.fetchall()]
            return profile

    async def update_summary_profile(self, profile_id: int, values: Dict[str, Any]) -> bool:
        async with self.get_connection() as db:
            if values.get("is_primary"):
                await db.execute(
                    """UPDATE summary_profiles SET is_primary = 0
                       WHERE collection_id = (SELECT collection_id FROM summary_profiles WHERE id = ?)
                         AND id != ?""",
                    (profile_id, profile_id),
                )
            cursor = await db.execute(
                """UPDATE summary_profiles SET name = ?, is_primary = ?, purpose = ?, window_hours = ?,
                   selection_policy = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (values["name"], values.get("is_primary", False), values["purpose"], values["window_hours"], values.get("selection_policy", "latest-completed-per-target"), profile_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def delete_summary_profile(self, profile_id: int) -> bool:
        async with self.get_connection() as db:
            cursor = await db.execute("DELETE FROM summary_profiles WHERE id = ?", (profile_id,))
            await db.commit()
            return cursor.rowcount == 1

    async def replace_collection_membership(self, collection_id: int, target_ids: List[int]) -> None:
        """Atomically replace a Collection's explicitly managed Target membership."""
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("Collection membership cannot contain duplicate Targets")
        async with self.get_connection() as db:
            try:
                await db.execute("BEGIN")
                await db.execute("DELETE FROM collection_targets WHERE collection_id = ?", (collection_id,))
                await db.executemany(
                    "INSERT INTO collection_targets (collection_id, target_id) VALUES (?, ?)",
                    [(collection_id, target_id) for target_id in target_ids],
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def create_summary_profile(
        self,
        collection_id: int,
        name: str,
        purpose: str,
        window_hours: int,
        is_primary: bool = False,
        selection_policy: str = "latest-completed-per-target",
    ) -> int:
        """Create a deterministic Collection Summary profile."""
        async with self.get_connection() as db:
            if is_primary:
                await db.execute(
                    "UPDATE summary_profiles SET is_primary = 0 WHERE collection_id = ?",
                    (collection_id,),
                )
            cursor = await db.execute(
                """INSERT INTO summary_profiles
                   (collection_id, name, is_primary, purpose, window_hours, selection_policy)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (collection_id, name, is_primary, purpose, window_hours, selection_policy),
            )
            await db.commit()
            return cursor.lastrowid

    async def replace_summary_profile_sources(
        self,
        profile_id: int,
        selectors: List[Tuple[str, str, Optional[int]]],
    ) -> None:
        """Atomically replace source-role/branch selectors for a Summary profile."""
        async with self.get_connection() as db:
            try:
                await db.execute("BEGIN")
                await db.execute("DELETE FROM summary_profile_sources WHERE profile_id = ?", (profile_id,))
                await db.executemany(
                    """INSERT INTO summary_profile_sources (profile_id, source_role, branch, target_id)
                       VALUES (?, ?, ?, ?)""",
                    [(profile_id, role, branch, target_id) for role, branch, target_id in selectors],
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def get_summary_profile_selection_inputs(self, profile_id: int) -> Dict[str, Any]:
        """Load persisted inputs needed for deterministic Summary selection."""
        async with self.get_connection() as db:
            profile_cursor = await db.execute(
                """SELECT id, collection_id, purpose, window_hours
                   FROM summary_profiles WHERE id = ?""",
                (profile_id,),
            )
            profile_row = await profile_cursor.fetchone()
            if not profile_row:
                raise ValueError(f"Summary profile {profile_id} does not exist")
            profile = dict(zip([column[0] for column in profile_cursor.description], profile_row))

            members_cursor = await db.execute(
                """SELECT targets.key
                   FROM collection_targets
                   JOIN targets ON targets.id = collection_targets.target_id
                   WHERE collection_targets.collection_id = ?
                   ORDER BY targets.key""",
                (profile["collection_id"],),
            )
            target_keys = [row[0] for row in await members_cursor.fetchall()]

            selector_cursor = await db.execute(
                """SELECT summary_profile_sources.source_role, summary_profile_sources.branch,
                          targets.key AS target_key
                   FROM summary_profile_sources
                   LEFT JOIN targets ON targets.id = summary_profile_sources.target_id
                   WHERE summary_profile_sources.profile_id = ?""",
                (profile_id,),
            )
            selectors = [
                dict(zip([column[0] for column in selector_cursor.description], row))
                for row in await selector_cursor.fetchall()
            ]

            if not target_keys:
                return {"profile": profile, "target_keys": [], "selectors": selectors, "runs": []}

            run_cursor = await db.execute(
                """SELECT run_id, target_key, purpose, status, end_time
                    FROM test_runs
                    WHERE status = 'finished' AND target_key IN (SELECT value FROM json_each(?))""",
                (json.dumps(target_keys),),
            )
            runs = [dict(zip([column[0] for column in run_cursor.description], row)) for row in await run_cursor.fetchall()]
            if not runs:
                return {"profile": profile, "target_keys": target_keys, "selectors": selectors, "runs": []}

            run_ids = [run["run_id"] for run in runs]
            source_cursor = await db.execute(
                """SELECT run_id, source_role, branch, revision, repository_url, dirty
                    FROM run_sources WHERE run_id IN (SELECT value FROM json_each(?))""",
                (json.dumps(run_ids),),
            )
            sources_by_run: Dict[str, Dict[str, Dict[str, Any]]] = {run_id: {} for run_id in run_ids}
            for row in await source_cursor.fetchall():
                run_id, role, branch, revision, repository_url, dirty = row
                sources_by_run[run_id][role] = {
                    "branch": branch,
                    "revision": revision,
                    "repository_url": repository_url,
                    "dirty": bool(dirty) if dirty is not None else None,
                }
            for run in runs:
                run["sources"] = sources_by_run[run["run_id"]]
            return {"profile": profile, "target_keys": target_keys, "selectors": selectors, "runs": runs}

    async def replace_run_sources(self, run_id: str, sources: Dict[str, Dict[str, Any]]) -> None:
        """Persist the complete structured source snapshot for one Run."""
        async with self.get_connection() as db:
            try:
                await db.execute("BEGIN")
                await db.execute("DELETE FROM run_sources WHERE run_id = ?", (run_id,))
                await db.executemany(
                    """INSERT INTO run_sources
                       (run_id, source_role, branch, revision, repository_url, dirty)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            run_id,
                            role,
                            source["branch"],
                            source["revision"],
                            source.get("repository_url"),
                            source.get("dirty"),
                        )
                        for role, source in sources.items()
                    ],
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def insert_test_run(
        self,
        test_run: TestRunData,
        user_metadata: Dict[str, Any] = None,
        sources: Dict[str, Dict[str, Any]] = None,
    ) -> bool:
        """Insert a new test run into the database."""
        async with self.get_connection() as db:
            try:
                target_key = test_run.target_key or "unknown-target"
                await db.execute(
                    """INSERT INTO targets (key, display_name, setup_state)
                       VALUES (?, ?, 'needs_setup') ON CONFLICT(key) DO NOTHING""",
                    (target_key, target_key),
                )
                # Insert test run
                await db.execute("""
                    INSERT OR REPLACE INTO test_runs
                    (run_id, target_key, purpose, parent_run_id, status, start_time, end_time, retention_days, local_run, dut, run_name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    test_run.run_id,
                    target_key,
                    test_run.purpose,
                    test_run.parent_run_id,
                    test_run.status,
                    test_run.start_time,
                    test_run.end_time,
                    test_run.retention_days,
                    test_run.local_run,
                    test_run.dut,
                    test_run.run_name,
                    datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
                ))

                # Insert user metadata if provided
                if user_metadata:
                    for key, meta_value in user_metadata.items():
                        value = meta_value.get("value", "") if isinstance(meta_value, dict) else str(meta_value)
                        url = meta_value.get("url") if isinstance(meta_value, dict) else None

                        await db.execute("""
                            INSERT OR REPLACE INTO user_metadata (run_id, key, value, url)
                            VALUES (?, ?, ?, ?)
                        """, (test_run.run_id, key, value, url))

                if sources:
                    await db.executemany(
                        """INSERT INTO run_sources
                           (run_id, source_role, branch, revision, repository_url, dirty)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        [
                            (
                                test_run.run_id,
                                role,
                                source["branch"],
                                source["revision"],
                                source.get("repository_url"),
                                source.get("dirty"),
                            )
                            for role, source in sources.items()
                        ],
                    )

                await db.commit()
                return True
            except Exception as e:
                print(f"Error inserting test run: {e}")
                await db.rollback()
                return False

    async def update_test_run(self, run_id: str, **updates) -> bool:
        """Update an existing test run."""
        async with self.get_connection() as db:
            try:
                # Build dynamic update query
                set_clauses = []
                values = []

                for key, value in updates.items():
                    if key in ['status', 'end_time']:
                        set_clauses.append(f"{key} = ?")
                        values.append(value)

                if set_clauses:
                    set_clauses.append("updated_at = ?")
                    values.append(datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z")
                    values.append(run_id)

                    await db.execute(f"""
                        UPDATE test_runs
                        SET {', '.join(set_clauses)}
                        WHERE run_id = ?
                    """, values)

                    await db.commit()
                    return True
                return False
            except Exception as e:
                print(f"Error updating test run: {e}")
                await db.rollback()
                return False

    async def insert_test_case(self, test_case: TestCaseData) -> bool:
        """Insert a new test case into the database."""
        async with self.get_connection() as db:
            try:
                await db.execute("""
                    INSERT OR REPLACE INTO test_cases
                    (run_id, tc_full_name, tc_id, status, start_time, end_time, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    test_case.run_id,
                    test_case.tc_full_name,
                    test_case.tc_id,
                    test_case.status,
                    test_case.start_time,
                    test_case.end_time,
                    datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
                ))

                await db.commit()
                return True
            except Exception as e:
                print(f"Error inserting test case: {e}")
                await db.rollback()
                return False

    async def get_test_runs(
        self,
        limit: int = 100,
        offset: int = 0,
        status_filter: Optional[str] = None,
        metadata_filters: Optional[Dict[str, str]] = None,
        target_key: Optional[str] = None,
        purpose: Optional[str] = None,
        source_role: Optional[str] = None,
        source_branch: Optional[str] = None,
        revision: Optional[str] = None,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        collection_key: Optional[str] = None,
        run_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get test runs with optional filtering."""
        async with self.get_connection() as db:
            # Build query with joins for metadata filtering
            # Note: user_metadata JOIN removed to prevent duplicate rows inflating counts
            # Metadata filtering is handled via EXISTS subqueries in WHERE clause
            query = """
                SELECT tr.*,
                       COUNT(tc.id) as test_case_count,
                       SUM(CASE WHEN tc.status = 'passed' THEN 1 ELSE 0 END) as passed_count,
                       SUM(CASE WHEN tc.status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                       SUM(CASE WHEN tc.status = 'skipped' THEN 1 ELSE 0 END) as skipped_count,
                       SUM(CASE WHEN tc.status = 'aborted' THEN 1 ELSE 0 END) as aborted_count,
                       SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) as error_count
                FROM test_runs tr
                LEFT JOIN test_cases tc ON tr.run_id = tc.run_id
            """

            conditions = []
            params = []

            # Always exclude 'preparing' runs from UI - they're not yet active
            conditions.append("tr.status != 'preparing'")

            if status_filter:
                conditions.append("tr.status = ?")
                params.append(status_filter)

            if metadata_filters:
                for key, value in metadata_filters.items():
                    conditions.append("EXISTS (SELECT 1 FROM user_metadata um2 WHERE um2.run_id = tr.run_id AND um2.key = ? AND um2.value = ?)")
                    params.extend([key, value])

            if target_key:
                conditions.append("tr.target_key = ?")
                params.append(target_key)
            if purpose:
                conditions.append("tr.purpose = ?")
                params.append(purpose)
            if start_at:
                conditions.append("tr.start_time >= ?")
                params.append(start_at)
            if end_at:
                conditions.append("tr.start_time <= ?")
                params.append(end_at)
            if source_role or source_branch or revision:
                source_conditions = ["run_sources.run_id = tr.run_id"]
                source_params = []
                if source_role:
                    source_conditions.append("run_sources.source_role = ?")
                    source_params.append(source_role)
                if source_branch:
                    source_conditions.append("run_sources.branch = ?")
                    source_params.append(source_branch)
                if revision:
                    source_conditions.append("run_sources.revision = ?")
                    source_params.append(revision)
                conditions.append("EXISTS (SELECT 1 FROM run_sources WHERE " + " AND ".join(source_conditions) + ")")
                params.extend(source_params)
            if collection_key:
                conditions.append("EXISTS (SELECT 1 FROM collection_targets JOIN collections ON collections.id = collection_targets.collection_id JOIN targets ON targets.id = collection_targets.target_id WHERE targets.key = tr.target_key AND collections.key = ?)")
                params.append(collection_key)
            if run_ids is not None:
                if not run_ids:
                    return []
                conditions.append("tr.run_id IN (SELECT value FROM json_each(?))")
                params.append(json.dumps(run_ids))

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " GROUP BY tr.run_id ORDER BY tr.start_time DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            # Convert to list of dictionaries
            columns = [desc[0] for desc in cursor.description]
            runs = [dict(zip(columns, row)) for row in rows]
            await self._annotate_runs_has_comments(db, runs)
            return runs

    async def _annotate_runs_has_comments(self, db, runs: List[Dict[str, Any]]) -> None:
        """Set has_comments and first_comment_id on each run dict. Uses the open connection."""
        if not runs:
            return
        run_ids = [run["run_id"] for run in runs]
        cursor = await db.execute(
            """SELECT run_id, id FROM comments
               WHERE run_id IN (SELECT value FROM json_each(?))
               ORDER BY line_end ASC, created_at ASC, id ASC""",
            (json.dumps(run_ids),),
        )
        first_ids: Dict[str, int] = {}
        commented = set()
        for run_id, comment_id in await cursor.fetchall():
            commented.add(run_id)
            if run_id not in first_ids:
                first_ids[run_id] = comment_id
        for run in runs:
            run["has_comments"] = run["run_id"] in commented
            run["first_comment_id"] = first_ids.get(run["run_id"])

    async def get_run_names_starting_with(self, base_name: str, target_key: str) -> List[str]:
        """Get all Run names that start with a base name for one Target."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT run_name FROM test_runs
                WHERE (run_name = ? OR run_name LIKE ?) AND target_key = ?
                ORDER BY run_name
            """, (base_name, f"{base_name} %", target_key))

            rows = await cursor.fetchall()
            return [row[0] for row in rows if row[0]]

    async def get_test_run_by_id(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get a single test run by ID."""
        async with self.get_connection() as db:
            query = """
                SELECT tr.*,
                       COUNT(tc.id) as test_case_count,
                       SUM(CASE WHEN tc.status = 'passed' THEN 1 ELSE 0 END) as passed_count,
                       SUM(CASE WHEN tc.status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                       SUM(CASE WHEN tc.status = 'skipped' THEN 1 ELSE 0 END) as skipped_count,
                       SUM(CASE WHEN tc.status = 'aborted' THEN 1 ELSE 0 END) as aborted_count,
                       SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) as error_count
                FROM test_runs tr
                LEFT JOIN test_cases tc ON tr.run_id = tc.run_id
                WHERE tr.run_id = ?
                GROUP BY tr.run_id
            """

            cursor = await db.execute(query, (run_id,))
            row = await cursor.fetchone()

            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    async def replace_kpi_batch(
        self,
        run_id: str,
        source_key: str,
        checksum_sha256: str,
        schema_version: int,
        source_type: str,
        raw_json: str,
        payload_size: int,
        provenance: Dict[str, Any],
        samples: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Atomically replace one run-bound KPI source and its normalized samples."""
        async with self.get_connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            run_cursor = await db.execute(
                "SELECT 1 FROM test_runs WHERE run_id = ?", (run_id,)
            )
            if await run_cursor.fetchone() is None:
                await db.rollback()
                raise ValueError("Run not found")

            existing_cursor = await db.execute(
                "SELECT id, checksum_sha256 FROM kpi_batches "
                "WHERE run_id = ? AND source_key = ?",
                (run_id, source_key),
            )
            existing = await existing_cursor.fetchone()
            if existing and existing[1] == checksum_sha256:
                count_cursor = await db.execute(
                    "SELECT COUNT(*) FROM kpi_samples WHERE batch_id = ?", (existing[0],)
                )
                count = (await count_cursor.fetchone())[0]
                await db.commit()
                return {"idempotent": True, "batch_id": existing[0], "sample_count": count}

            if existing:
                await db.execute("DELETE FROM kpi_batches WHERE id = ?", (existing[0],))

            cursor = await db.execute(
                """INSERT INTO kpi_batches
                   (run_id, source_key, checksum_sha256, schema_version, source_type,
                    payload_size, sample_count, raw_json, provenance_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    source_key,
                    checksum_sha256,
                    schema_version,
                    source_type,
                    payload_size,
                    len(samples),
                    raw_json,
                    json.dumps(provenance, separators=(",", ":")),
                ),
            )
            batch_id = cursor.lastrowid
            matched_count = 0
            unmatched_count = 0
            for ordinal, sample in enumerate(samples):
                test_case_cursor = await db.execute(
                    "SELECT id FROM test_cases WHERE run_id = ? AND tc_full_name = ?",
                    (run_id, sample["test_name"]),
                )
                test_case = await test_case_cursor.fetchone()
                test_case_id = test_case[0] if test_case else None
                match_status = "matched" if test_case else "unmatched"
                matched_count += test_case is not None
                unmatched_count += test_case is None
                await db.execute(
                    """INSERT INTO kpi_samples
                       (batch_id, run_id, test_case_id, test_name, match_status,
                        metric_key, value, unit, dimensions_json, sample_time,
                        ordinal, source_type, provenance_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        batch_id,
                        run_id,
                        test_case_id,
                        sample["test_name"],
                        match_status,
                        sample["metric_key"],
                        sample["value"],
                        sample["unit"],
                        json.dumps(sample.get("dimensions", {}), separators=(",", ":")),
                        sample.get("timestamp_utc"),
                        ordinal,
                        source_type,
                        json.dumps(provenance, separators=(",", ":")),
                    ),
                )

            await db.commit()
            return {
                "idempotent": False,
                "batch_id": batch_id,
                "sample_count": len(samples),
                "matched_count": matched_count,
                "unmatched_count": unmatched_count,
            }

    @staticmethod
    def _kpi_filter_sql(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
        clauses = []
        params: List[Any] = []
        for key, column in (("run_id", "ks.run_id"), ("target_key", "tr.target_key"),
                    ("metric_key", "ks.metric_key"), ("unit", "ks.unit"),
                    ("test_name", "ks.test_name")):
            if filters.get(key) is not None:
                clauses.append(f"{column} = ?")
                params.append(filters[key])
        target_keys = filters.get("target_keys") or []
        if target_keys:
            clauses.append("tr.target_key IN (" + ", ".join("?" for _ in target_keys) + ")")
            params.extend(target_keys)
        if filters.get("start_time"):
            clauses.append("julianday(COALESCE(ks.sample_time, tr.start_time)) >= julianday(?)")
            params.append(filters["start_time"])
        if filters.get("end_time"):
            clauses.append("julianday(COALESCE(ks.sample_time, tr.start_time)) <= julianday(?)")
            params.append(filters["end_time"])
        for key, value in (filters.get("dimensions") or {}).items():
            if not key.isascii() or not key.replace("_", "").isalnum() or not key[0].isalpha():
                raise ValueError("Invalid KPI dimension name")
            clauses.append("json_extract(ks.dimensions_json, ?) = ?")
            params.extend((f"$.{key}", value))
        source_filters = [(key, column) for key, column in (
            ("source_role", "source_role"),
            ("source_branch", "branch"),
            ("source_revision", "revision"),
        ) if filters.get(key)]
        if source_filters:
            source_clauses = ["sources.run_id = ks.run_id"]
            for key, column in source_filters:
                source_clauses.append(f"sources.{column} = ?")
                params.append(filters[key])
            clauses.append(
                "EXISTS (SELECT 1 FROM run_sources sources WHERE "
                + " AND ".join(source_clauses)
                + ")"
            )
        return (" AND ".join(clauses) if clauses else "1 = 1", params)

    async def get_kpi_samples(
        self,
        filters: Dict[str, Any],
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        where, params = self._kpi_filter_sql(filters)
        query = f"""SELECT ks.*, tr.target_key, tr.purpose, tr.start_time AS run_start_time,
                          tr.run_name, tc.tc_id
                   FROM kpi_samples ks
                   JOIN test_runs tr ON tr.run_id = ks.run_id
                   LEFT JOIN test_cases tc ON tc.id = ks.test_case_id
                   WHERE {where}"""
        async with self.get_connection() as db:
            count_cursor = await db.execute(
                f"SELECT COUNT(*) FROM kpi_samples ks JOIN test_runs tr ON tr.run_id = ks.run_id WHERE {where}",
                params,
            )
            total = (await count_cursor.fetchone())[0]
            cursor = await db.execute(
                query + " ORDER BY COALESCE(ks.sample_time, tr.start_time), ks.run_id, ks.ordinal LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in await cursor.fetchall()], total

    async def get_kpi_history(self, filters: Dict[str, Any]) -> Dict[str, Any]:
        """Return testcase choices and per-run aggregates for a family or one test."""
        testcase_filters = {key: value for key, value in filters.items()
                            if key not in ("test_name", "test_group", "catalog_only")}
        testcase_where, testcase_params = self._kpi_filter_sql(testcase_filters)
        if filters.get("catalog_only") and not filters.get("metric_key"):
            query = f"""SELECT ks.metric_key, ks.unit, ks.test_name,
                               COUNT(*) AS sample_count,
                               COUNT(DISTINCT ks.run_id) AS run_count,
                               MAX(COALESCE(ks.sample_time, tr.start_time)) AS last_sample,
                               MAX(ABS(ks.value)) AS max_abs_value
                        FROM kpi_samples ks
                        JOIN test_runs tr ON tr.run_id = ks.run_id
                        WHERE {testcase_where}
                        GROUP BY ks.metric_key, ks.unit, ks.test_name
                        ORDER BY ks.metric_key, ks.unit, last_sample DESC, ks.test_name"""
            async with self.get_connection() as db:
                cursor = await db.execute(query, testcase_params)
                columns = [column[0] for column in cursor.description]
                testcases = [dict(zip(columns, row)) for row in await cursor.fetchall()]
            return {"testcases": testcases, "selected_test_name": None,
                    "series_test_names": [], "data": [],
                    "summary": {"run_count": 0, "sample_count": 0}}
        testcase_query = f"""SELECT ks.test_name,
                                    COUNT(*) AS sample_count,
                                    COUNT(DISTINCT ks.run_id) AS run_count,
                                    MAX(COALESCE(ks.sample_time, tr.start_time)) AS last_sample
                             FROM kpi_samples ks
                             JOIN test_runs tr ON tr.run_id = ks.run_id
                             WHERE {testcase_where}
                             GROUP BY ks.test_name
                             ORDER BY last_sample DESC, ks.test_name"""

        async with self.get_connection() as db:
            cursor = await db.execute(testcase_query, testcase_params)
            columns = [column[0] for column in cursor.description]
            testcases = [dict(zip(columns, row)) for row in await cursor.fetchall()]
            selected_test_name = filters.get("test_name")
            testcase_names = {item["test_name"] for item in testcases}
            if selected_test_name not in testcase_names:
                selected_test_name = None
            test_group = filters.get("test_group")
            if test_group and not any(item["test_name"].rpartition(".")[0] == test_group
                                      for item in testcases):
                raise ValueError("Unknown KPI test group")
            series_test_names = ([selected_test_name] if selected_test_name else
                                 [item["test_name"] for item in testcases
                                  if item["test_name"].rpartition(".")[0] == test_group]
                                 if test_group else
                                 [item["test_name"] for item in sorted(
                                     testcases, key=lambda item: (-item["run_count"], item["test_name"])
                                 )[:8]])

            points = []
            sample_count = 0
            if series_test_names and not filters.get("catalog_only"):
                where, params = self._kpi_filter_sql(testcase_filters)
                placeholders = ", ".join("?" for _ in series_test_names)
                query = f"""SELECT ks.run_id,
                                   tr.target_key,
                                   tr.run_name,
                                   tr.start_time AS run_start_time,
                                   ks.test_name,
                            MAX(tc.tc_id) AS test_case_id,
                                   COUNT(*) AS sample_count,
                                   AVG(ks.value) AS value,
                                   MIN(ks.value) AS minimum,
                                   MAX(ks.value) AS maximum
                            FROM kpi_samples ks
                            JOIN test_runs tr ON tr.run_id = ks.run_id
                        LEFT JOIN test_cases tc ON tc.id = ks.test_case_id
                            WHERE {where} AND ks.test_name IN ({placeholders})
                            GROUP BY ks.run_id, tr.target_key, tr.run_name, tr.start_time, ks.test_name
                            ORDER BY tr.start_time, ks.run_id, ks.test_name"""
                cursor = await db.execute(query, (*params, *series_test_names))
                columns = [column[0] for column in cursor.description]
                points = [dict(zip(columns, row)) for row in await cursor.fetchall()]
                sample_count = sum(point["sample_count"] for point in points)

        return {
            "testcases": testcases,
            "selected_test_name": selected_test_name,
            "series_test_names": series_test_names,
            "data": points,
            "summary": {"run_count": len({point["run_id"] for point in points}), "sample_count": sample_count},
        }

    async def get_kpi_catalog(
        self,
        filters: Dict[str, Any],
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        where, params = self._kpi_filter_sql(filters)
        query = f"""SELECT ks.metric_key, ks.unit, tr.target_key, ks.dimensions_json,
                          COUNT(*) AS sample_count,
                          MIN(COALESCE(ks.sample_time, tr.start_time)) AS first_sample,
                          MAX(COALESCE(ks.sample_time, tr.start_time)) AS last_sample
                   FROM kpi_samples ks
                   JOIN test_runs tr ON tr.run_id = ks.run_id
                   WHERE {where}
                   GROUP BY ks.metric_key, ks.unit, tr.target_key, ks.dimensions_json"""
        async with self.get_connection() as db:
            count_cursor = await db.execute(
                f"SELECT COUNT(*) FROM ({query})", params
            )
            total = (await count_cursor.fetchone())[0]
            cursor = await db.execute(
                query + " ORDER BY ks.metric_key, ks.unit, tr.target_key, ks.dimensions_json LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in await cursor.fetchall()], total

    async def get_kpi_dimension_options(
        self,
        filters: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        where, params = self._kpi_filter_sql(filters)
        query = f"""WITH dimension_sets AS (
                       SELECT DISTINCT ks.dimensions_json
                       FROM kpi_samples ks
                       JOIN test_runs tr ON tr.run_id = ks.run_id
                       WHERE {where}
                   )
                   SELECT DISTINCT dimension.key, dimension.type, dimension.value
                   FROM dimension_sets,
                        json_each(dimension_sets.dimensions_json) AS dimension
                   ORDER BY dimension.key, dimension.type, dimension.value"""
        async with self.get_connection() as db:
            cursor = await db.execute(query, params)
            options: Dict[str, List[Any]] = {}
            for key, value_type, value in await cursor.fetchall():
                if value_type == "true":
                    value = True
                elif value_type == "false":
                    value = False
                elif value_type in ("array", "object"):
                    value = json.loads(value)
                options.setdefault(key, []).append(value)
            return [
                {"dimension_key": key, "values": values}
                for key, values in options.items()
            ]

    async def get_kpi_metrics(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """List available metric/unit pairs without dimension-level duplication."""
        where, params = self._kpi_filter_sql(filters)
        query = f"""SELECT ks.metric_key, ks.unit, tr.target_key,
                            COUNT(*) AS sample_count,
                            MIN(COALESCE(ks.sample_time, tr.start_time)) AS first_sample,
                            MAX(COALESCE(ks.sample_time, tr.start_time)) AS last_sample
                     FROM kpi_samples ks
                     JOIN test_runs tr ON tr.run_id = ks.run_id
                     WHERE {where}
                     GROUP BY ks.metric_key, ks.unit, tr.target_key
                     ORDER BY ks.metric_key, ks.unit"""
        async with self.get_connection() as db:
            cursor = await db.execute(query, params)
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in await cursor.fetchall()]

    async def get_kpi_source_options(
        self,
        filters: Dict[str, Any],
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        where, params = self._kpi_filter_sql(filters)
        query = f"""SELECT DISTINCT sources.source_role, sources.branch, sources.revision
                   FROM kpi_samples ks
                   JOIN test_runs tr ON tr.run_id = ks.run_id
                   JOIN run_sources sources ON sources.run_id = ks.run_id
                   WHERE {where}"""
        async with self.get_connection() as db:
            count_cursor = await db.execute(f"SELECT COUNT(*) FROM ({query})", params)
            total = (await count_cursor.fetchone())[0]
            cursor = await db.execute(
                query + " ORDER BY sources.source_role, sources.branch, sources.revision LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in await cursor.fetchall()], total

    async def get_kpi_runs(
        self,
        filters: Dict[str, Any],
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """List runs that contain samples matching a KPI selection."""
        where, params = self._kpi_filter_sql(filters)
        query = f"""SELECT tr.run_id, tr.run_name, tr.start_time, tr.end_time,
                          tr.status, COUNT(ks.id) AS sample_count
                   FROM kpi_samples ks
                   JOIN test_runs tr ON tr.run_id = ks.run_id
                   WHERE {where}
                   GROUP BY tr.run_id"""
        async with self.get_connection() as db:
            count_cursor = await db.execute(f"SELECT COUNT(*) FROM ({query})", params)
            total = (await count_cursor.fetchone())[0]
            cursor = await db.execute(
                query + " ORDER BY tr.start_time DESC, tr.run_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in await cursor.fetchall()], total

    async def get_test_cases_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        """Get all test cases for a specific run."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT * FROM test_cases
                WHERE run_id = ?
                ORDER BY start_time
            """, (run_id,))

            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_test_results_for_runs(self, run_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Get all test results for multiple runs efficiently."""
        if not run_ids:
            return {}

        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT * FROM test_cases
                WHERE run_id IN (SELECT value FROM json_each(?))
                ORDER BY run_id, start_time
            """, (json.dumps(run_ids),))

            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]

            # Group results by run_id
            results = {}
            for row in rows:
                row_dict = dict(zip(columns, row))
                run_id = row_dict['run_id']
                if run_id not in results:
                    results[run_id] = []
                results[run_id].append(row_dict)

            return results

    async def get_user_metadata_for_run(self, run_id: str) -> Dict[str, Any]:
        """Get user metadata for a specific run."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT key, value, url FROM user_metadata
                WHERE run_id = ?
            """, (run_id,))

            rows = await cursor.fetchall()
            metadata = {}
            for key, value, url in rows:
                metadata[key] = {"value": value, "url": url}
            return metadata

    async def get_test_runs_over_time(
        self,
        days_back: int = 30,
        metadata_filters: Optional[Dict[str, str]] = None,
        run_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get individual test runs over time for trending analysis."""
        async with self.get_connection() as db:
            # Calculate date threshold
            cutoff_date = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_back)
            cutoff_str = cutoff_date.isoformat() + "Z"

            query = """
                SELECT
                    tr.run_id,
                    tr.run_name,
                    tr.start_time,
                    tr.end_time,
                    tr.status,
                    COUNT(tc.id) as total_tests,
                    SUM(CASE WHEN tc.status = 'passed' THEN 1 ELSE 0 END) as passed_tests,
                    SUM(CASE WHEN tc.status = 'failed' THEN 1 ELSE 0 END) as failed_tests,
                    SUM(CASE WHEN tc.status = 'skipped' THEN 1 ELSE 0 END) as skipped_tests,
                    SUM(CASE WHEN tc.status = 'aborted' THEN 1 ELSE 0 END) as aborted_tests,
                    SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) as error_tests
                FROM test_runs tr
                LEFT JOIN test_cases tc ON tr.run_id = tc.run_id
            """

            conditions = ["tr.start_time >= ?", "tr.status = 'finished'"]
            params = [cutoff_str]

            if metadata_filters:
                for key, value in metadata_filters.items():
                    conditions.append("EXISTS (SELECT 1 FROM user_metadata um WHERE um.run_id = tr.run_id AND um.key = ? AND um.value = ?)")
                    params.extend([key, value])

            if run_ids is not None:
                if not run_ids:
                    return []
                conditions.append("tr.run_id IN (SELECT value FROM json_each(?))")
                params.append(json.dumps(run_ids))

            query += " WHERE " + " AND ".join(conditions)
            query += " GROUP BY tr.run_id, tr.start_time, tr.end_time, tr.status ORDER BY tr.start_time ASC"

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_test_case_history(
        self,
        tc_full_name: str,
        limit: int = 50,
        metadata_filters: Optional[Dict[str, str]] = None,
        run_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get execution history for a specific test case."""
        async with self.get_connection() as db:
            query = """
                SELECT tc.id, tc.run_id, tc.tc_full_name, tc.tc_id, tc.status, tc.start_time, tc.end_time,
                       tr.start_time as run_start_time, tr.status as run_status, tr.run_name
                FROM test_cases tc
                JOIN test_runs tr ON tc.run_id = tr.run_id
            """

            conditions = ["tc.tc_full_name = ?"]
            params = [tc_full_name]

            if metadata_filters:
                for key, value in metadata_filters.items():
                    conditions.append("EXISTS (SELECT 1 FROM user_metadata um2 WHERE um2.run_id = tr.run_id AND um2.key = ? AND um2.value = ?)")
                    params.extend([key, value])

            if run_ids is not None:
                if not run_ids:
                    return []
                conditions.append("tr.run_id IN (SELECT value FROM json_each(?))")
                params.append(json.dumps(run_ids))

            query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY tc.start_time DESC LIMIT ?"
            params.append(limit)

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def classify_test_case(
        self,
        tc_full_name: str,
        run_ids: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Classify a test case based on recent history.

        Returns a classification string:
        - "new_failure": first time this TC failed (or no history)
        - "regression": was passing recently, now failing
        - "persistent_failure": has been failing consistently
        - "flaky": intermittent pass/fail pattern
        """
        history = await self.get_test_case_history(tc_full_name, limit=10, run_ids=run_ids)
        if not history:
            return "new_failure"

        statuses = [h.get("status", "").lower() for h in history]

        fail_count = sum(1 for s in statuses if s in ("failed", "error"))
        pass_count = sum(1 for s in statuses if s == "passed")

        if fail_count == 0:
            return "new_failure"
        if pass_count == 0:
            return "persistent_failure"
        if fail_count >= 2 and pass_count >= 2:
            return "flaky"
        if len(statuses) >= 2 and statuses[0] in ("failed", "error") and statuses[1] == "passed":
            return "regression"
        return "persistent_failure"

    async def get_unique_metadata_values(self, key: str) -> List[str]:
        """Get unique values for a specific metadata key."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT DISTINCT value FROM user_metadata
                WHERE key = ? AND value IS NOT NULL AND value != ''
                ORDER BY value
            """, (key,))

            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def get_all_metadata_keys(self) -> List[str]:
        """Get all unique metadata keys."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT DISTINCT key FROM user_metadata
                WHERE key IS NOT NULL AND key != ''
                ORDER BY key
            """)

            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def get_failed_test_cases(
        self,
        days_back: int = 30,
        limit: int = 100,
        metadata_filters: Optional[Dict[str, str]] = None,
        run_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get failed test cases within a time range for failure analysis."""
        async with self.get_connection() as db:
            query = """
                SELECT tc.run_id, tc.tc_full_name, tc.tc_id, tc.status, tc.start_time, tc.end_time,
                       tr.start_time as run_start_time, tr.target_key
                FROM test_cases tc
                JOIN test_runs tr ON tc.run_id = tr.run_id
            """

            conditions = [
                "tc.status = 'failed'",
                "tr.start_time >= datetime('now', ?)"
            ]
            params = [f"-{days_back} days"]

            if run_ids is not None:
                if not run_ids:
                    return []
                conditions.append("tr.run_id IN (SELECT value FROM json_each(?))")
                params.append(json.dumps(run_ids))

            if metadata_filters:
                for key, value in metadata_filters.items():
                    conditions.append("""
                        EXISTS (SELECT 1 FROM user_metadata um
                                WHERE um.run_id = tr.run_id AND um.key = ? AND um.value = ?)
                    """)
                    params.extend([key, value])

            query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY tc.start_time DESC LIMIT ?"
            params.append(limit)

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_failure_counts_by_test_case(
        self,
        days_back: int = 30,
        top_n: int = 20,
        metadata_filters: Optional[Dict[str, str]] = None,
        run_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get top N test cases by failure count, including run_id of last failure."""
        async with self.get_connection() as db:
            # Build conditions for the base query
            base_conditions = [
                "tc.status = 'failed'",
                "tr.start_time >= datetime('now', ?)"
            ]
            params = [f"-{days_back} days"]

            if run_ids is not None:
                if not run_ids:
                    return []
                base_conditions.append("tr.run_id IN (SELECT value FROM json_each(?))")
                params.append(json.dumps(run_ids))

            if metadata_filters:
                for key, value in metadata_filters.items():
                    base_conditions.append("""
                        EXISTS (SELECT 1 FROM user_metadata um
                                WHERE um.run_id = tr.run_id AND um.key = ? AND um.value = ?)
                    """)
                    params.extend([key, value])

            where_clause = " AND ".join(base_conditions)

            # Use a subquery to get the run_id and tc_id of the last failure for each test case
            query = f"""
                WITH failure_stats AS (
                    SELECT tc.tc_full_name,
                           COUNT(*) as failure_count,
                           MAX(tc.start_time) as last_failure
                    FROM test_cases tc
                    JOIN test_runs tr ON tc.run_id = tr.run_id
                    WHERE {where_clause}
                    GROUP BY tc.tc_full_name
                    ORDER BY failure_count DESC
                    LIMIT ?
                ),
                last_failures AS (
                    SELECT tc.tc_full_name, tc.run_id as last_failure_run_id, tc.tc_id as last_failure_tc_id, tc.start_time,
                           ROW_NUMBER() OVER (PARTITION BY tc.tc_full_name ORDER BY tc.start_time DESC) as rn
                    FROM test_cases tc
                    JOIN test_runs tr ON tc.run_id = tr.run_id
                    WHERE tc.status = 'failed' AND {where_clause}
                )
                SELECT fs.tc_full_name, fs.failure_count, fs.last_failure, lf.last_failure_run_id, lf.last_failure_tc_id
                FROM failure_stats fs
                LEFT JOIN last_failures lf ON fs.tc_full_name = lf.tc_full_name AND lf.rn = 1
                ORDER BY fs.failure_count DESC
            """

            # Add top_n param and duplicate the other params for the second subquery
            params.append(top_n)
            params.extend(params[:-1])  # Duplicate params for the last_failures subquery (excluding top_n)

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_test_case_classification_data(
        self,
        tc_full_name: str,
        limit: int = 10,
        current_run_id: Optional[str] = None,
        current_run_start_time: Optional[str] = None,
        run_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Get test case history data needed for classification.

        Returns the last N results for a test case within the same group,
        ordered from most recent to oldest.
        Excludes the current run and any runs executed later than the current run.
        """
        async with self.get_connection() as db:
            query = """
                SELECT tc.status, tc.tc_id, tr.start_time as run_start_time, tr.run_id, tr.run_name
                FROM test_cases tc
                JOIN test_runs tr ON tc.run_id = tr.run_id
                WHERE tc.tc_full_name = ?
            """
            params = [tc_full_name]

            if run_ids is not None:
                if not run_ids:
                    return []
                query += " AND tr.run_id IN (SELECT value FROM json_each(?))"
                params.append(json.dumps(run_ids))

            # Exclude current run
            if current_run_id:
                query += " AND tr.run_id != ?"
                params.append(current_run_id)

            # Exclude runs executed later than current run (based on start_time)
            if current_run_start_time:
                query += " AND tr.start_time <= ?"
                params.append(current_run_start_time)

            query += " ORDER BY tr.start_time DESC LIMIT ?"
            params.append(limit)

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_test_run_history(
        self,
        run_ids: List[str],
        limit: int = 10,
        exclude_run_id: Optional[str] = None,
        current_run_start_time: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent Run summaries constrained to an exact Run set."""
        if not run_ids:
            return []
        async with self.get_connection() as db:
            query = """
                SELECT tr.run_id, tr.run_name, tr.status, tr.start_time, tr.end_time,
                       COUNT(tc.id) as test_case_count,
                       SUM(CASE WHEN tc.status = 'passed' THEN 1 ELSE 0 END) as passed_count,
                       SUM(CASE WHEN tc.status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                       SUM(CASE WHEN tc.status = 'skipped' THEN 1 ELSE 0 END) as skipped_count,
                       SUM(CASE WHEN tc.status = 'error' THEN 1 ELSE 0 END) as error_count
                FROM test_runs tr
                LEFT JOIN test_cases tc ON tr.run_id = tc.run_id
                WHERE tr.run_id IN (SELECT value FROM json_each(?))
            """
            params = [json.dumps(run_ids)]
            if exclude_run_id:
                query += " AND tr.run_id != ?"
                params.append(exclude_run_id)
            if current_run_start_time:
                query += " AND tr.start_time < ?"
                params.append(current_run_start_time)
            query += " GROUP BY tr.run_id ORDER BY tr.start_time DESC, tr.run_id DESC LIMIT ?"
            params.append(limit)
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_previous_run_test_cases_in_set(
        self,
        run_ids: List[str],
        current_run_id: str,
    ) -> List[str]:
        """Get test names from the previous Run in an exact Run set."""
        if not run_ids:
            return []
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT start_time FROM test_runs WHERE run_id = ?", (current_run_id,))
            row = await cursor.fetchone()
            if not row:
                return []
            cursor = await db.execute(
                "SELECT run_id FROM test_runs WHERE run_id IN (SELECT value FROM json_each(?)) AND start_time < ? ORDER BY start_time DESC, run_id DESC LIMIT 1",
                (json.dumps(run_ids), row[0]),
            )
            previous = await cursor.fetchone()
            if not previous:
                return []
            cursor = await db.execute("SELECT tc_full_name FROM test_cases WHERE run_id = ?", (previous[0],))
            return [item[0] for item in await cursor.fetchall()]

    async def get_classifications_for_run(
        self,
        run_id: str,
        run_ids: Optional[List[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Get classification data for all test cases in a run.

        Returns a dict mapping tc_full_name to classification info:
        - classification: 'flaky', 'fixed', 'regression', or None
        - is_new: True if TC wasn't in previous run
        - history: list of last 10 statuses (for hover tooltip)
        """
        async with self.get_connection() as db:
            # Get current run's start time
            cursor = await db.execute(
                "SELECT start_time FROM test_runs WHERE run_id = ?",
                (run_id,)
            )
            run_row = await cursor.fetchone()
            current_run_start_time = run_row[0] if run_row else None

            # Get all test cases in the run
            cursor = await db.execute(
                "SELECT tc_full_name, status FROM test_cases WHERE run_id = ?",
                (run_id,)
            )
            test_cases = await cursor.fetchall()

            if not test_cases:
                return {}

            previous_tc_ids = set()
            if run_ids is not None:
                previous_tc_ids = set(await self.get_previous_run_test_cases_in_set(run_ids, run_id))

            result = {}
            for tc_id, current_status in test_cases:
                # Get history for this TC - only previous runs (excludes current and future runs)
                history = await self.get_test_case_classification_data(
                    tc_id,
                    limit=10,
                    current_run_id=run_id,
                    current_run_start_time=current_run_start_time,
                    run_ids=run_ids,
                )

                # Calculate classification based on previous runs only
                classification = self._calculate_classification(current_status, history)

                # Determine if TC is new (wasn't in previous run)
                # Returns True if:
                # - We have a Run set (so we can compare runs)
                # - There were test cases in the previous run
                # - This TC was not in the previous run
                is_new = bool(
                    run_ids is not None
                    and len(previous_tc_ids) > 0
                    and tc_id not in previous_tc_ids
                )

                result[tc_id] = {
                    'classification': classification,
                    'is_new': is_new,
                    'history': [
                        {
                            'status': h['status'],
                            'run_id': h['run_id'],
                            'run_name': h.get('run_name'),
                            'run_start_time': h.get('run_start_time')
                        }
                        for h in history
                    ]
                }

            return result

    def _calculate_classification(
        self,
        current_status: str,
        history: List[Dict[str, Any]]
    ) -> Optional[str]:
        """Calculate classification based on current status and history.

        - flaky: More than 4 transitions in last 10 results
        - fixed: Last 5 results were fail/error, new one is pass
        - regression: Last 5 results were pass, new one is fail/error
        """
        if not history:
            return None

        current_is_pass = current_status.lower() == 'passed'
        current_is_fail = current_status.lower() in ('failed', 'error')

        # Filter out skipped for classification purposes
        relevant_history = [
            h for h in history
            if h['status'].lower() not in ('skipped', 'running', 'aborted')
        ]

        if not relevant_history:
            return None

        # Check for flaky (count transitions in history + current)
        statuses = [current_status.lower()] + [h['status'].lower() for h in relevant_history]
        # Filter to just pass/fail for transition counting
        statuses = [
            'pass' if s == 'passed' else 'fail'
            for s in statuses
            if s in ('passed', 'failed', 'error')
        ]

        if len(statuses) >= 2:
            transitions = sum(
                1 for i in range(len(statuses) - 1)
                if statuses[i] != statuses[i + 1]
            )
            if transitions > 4:
                return 'flaky'

        # Check for fixed (last 5 were fail, now pass)
        if current_is_pass and len(relevant_history) >= 5:
            last_5 = [h['status'].lower() for h in relevant_history[:5]]
            if all(s in ('failed', 'error') for s in last_5):
                return 'fixed'

        # Check for regression (last 5 were pass, now fail)
        if current_is_fail and len(relevant_history) >= 5:
            last_5 = [h['status'].lower() for h in relevant_history[:5]]
            if all(s == 'passed' for s in last_5):
                return 'regression'

        return None

    async def insert_run_commits(
        self,
        run_id: str,
        commits: List[Dict[str, str]]
    ) -> bool:
        """
        Insert commit SHAs for a run.

        Args:
            run_id: The run ID
            commits: List of dicts with repo_name, commit_sha, and optional repo_url

        Returns:
            True if successful
        """
        async with self.get_connection() as db:
            try:
                for commit in commits:
                    await db.execute("""
                        INSERT OR REPLACE INTO run_commits (run_id, repo_name, commit_sha, repo_url)
                        VALUES (?, ?, ?, ?)
                    """, (
                        run_id,
                        commit.get("repo_name"),
                        commit.get("commit_sha"),
                        commit.get("repo_url"),
                    ))
                await db.commit()
                return True
            except Exception as e:
                print(f"Error inserting run commits: {e}")
                await db.rollback()
                return False

    async def get_commit_baselines_for_run(self, run_id: str) -> Dict[str, Dict[str, str]]:
        """Find earlier finished exact-Target/purpose/branch source baselines."""
        async with self.get_connection() as db:
            current_cursor = await db.execute(
                "SELECT target_key, purpose, start_time FROM test_runs WHERE run_id = ?", (run_id,)
            )
            current = await current_cursor.fetchone()
            if not current:
                raise ValueError("Run not found")
            target_key, purpose, start_time = current
            source_cursor = await db.execute(
                "SELECT source_role, branch FROM run_sources WHERE run_id = ?", (run_id,)
            )
            baselines = {}
            for role, branch in await source_cursor.fetchall():
                candidate_cursor = await db.execute(
                    """SELECT runs.run_id, sources.revision
                       FROM test_runs AS runs
                       JOIN run_sources AS sources ON sources.run_id = runs.run_id
                       WHERE runs.target_key = ? AND runs.purpose = ? AND runs.status = 'finished'
                         AND runs.start_time < ? AND sources.source_role = ? AND sources.branch = ?
                       ORDER BY runs.end_time DESC, runs.run_id DESC LIMIT 1""",
                    (target_key, purpose, start_time, role, branch),
                )
                candidate = await candidate_cursor.fetchone()
                if candidate:
                    baselines[role] = {"run_id": candidate[0], "revision": candidate[1]}
            return baselines

    async def get_run_sources(self, run_id: str) -> Dict[str, str]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT source_role, revision FROM run_sources WHERE run_id = ?", (run_id,))
            return {role: revision for role, revision in await cursor.fetchall()}

    async def get_or_create_collection_report(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Persist an idempotent Collection report context and return its stored record."""
        async with self.get_connection() as db:
            await db.execute(
                """INSERT INTO collection_reports (collection_id, profile_id, requested_at, context_json)
                   VALUES (?, ?, ?, ?) ON CONFLICT(collection_id, profile_id, requested_at) DO NOTHING""",
                (context["collection_id"], context["profile_id"], context["requested_at"], json.dumps(context)),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM collection_reports WHERE collection_id = ? AND profile_id = ? AND requested_at = ?",
                (context["collection_id"], context["profile_id"], context["requested_at"]),
            )
            row = await cursor.fetchone()
            return dict(zip([column[0] for column in cursor.description], row))

    async def get_commits_for_run(self, run_id: str) -> List[Dict[str, str]]:
        """
        Get all commit records for a run.

        Returns:
            List of dicts with repo_name, commit_sha, repo_url
        """
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT repo_name, commit_sha, repo_url FROM run_commits
                WHERE run_id = ?
            """, (run_id,))
            rows = await cursor.fetchall()

            return [
                {"repo_name": row[0], "commit_sha": row[1], "repo_url": row[2]}
                for row in rows
            ]

    # --- AI Analysis Methods ---

    async def get_analysis_by_fingerprint(self, fingerprint: str, max_age_days: int = 30) -> Optional[Dict]:
        """Find an existing analysis by symptom fingerprint within the dedup window."""
        async with self.get_connection() as db:
            cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=max_age_days)).isoformat() + "Z"
            cursor = await db.execute("""
                SELECT id, fingerprint, summary, references_json, confidence, category,
                       model_used, tier_used, reasoning, context_hash, token_count, created_at
                FROM ai_analyses
                WHERE fingerprint = ? AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
            """, (fingerprint, cutoff))
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    async def insert_ai_analysis(self, fingerprint: str, summary: str, references_json: str,
                                  confidence: float, category: str, model_used: str,
                                  tier_used: int, reasoning: Optional[str],
                                  context_hash: str, token_count: Optional[int],
                                  summary_html: Optional[str] = None) -> int:
        """Insert a new AI analysis result. Returns the analysis ID."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                INSERT OR REPLACE INTO ai_analyses
                (fingerprint, summary, summary_html, references_json, confidence, category,
                 model_used, tier_used, reasoning, context_hash, token_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (fingerprint, summary, summary_html, references_json, confidence, category,
                  model_used, tier_used, reasoning, context_hash, token_count))
            await db.commit()
            return cursor.lastrowid

    async def link_analysis_to_test_case(self, run_id: str, tc_full_name: str, analysis_id: int):
        """Link an AI analysis to a specific test case in a run."""
        async with self.get_connection() as db:
            await db.execute("""
                INSERT OR REPLACE INTO test_case_analyses (run_id, tc_full_name, analysis_id)
                VALUES (?, ?, ?)
            """, (run_id, tc_full_name, analysis_id))
            await db.commit()

    async def get_analyses_for_run(self, run_id: str) -> List[Dict]:
        """Get all AI analyses for a run."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT tca.tc_full_name, tc.tc_id,
                       aa.id as analysis_id, aa.fingerprint,
                       aa.summary, aa.summary_html, aa.references_json,
                       aa.confidence, aa.category,
                       aa.model_used, aa.tier_used, aa.reasoning, aa.deep_html,
                       aa.token_count, aa.created_at
                FROM test_case_analyses tca
                JOIN ai_analyses aa ON tca.analysis_id = aa.id
                LEFT JOIN test_cases tc ON tca.run_id = tc.run_id AND tca.tc_full_name = tc.tc_full_name
                WHERE tca.run_id = ?
                ORDER BY tca.tc_full_name
            """, (run_id,))
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_analysis_for_test_case(self, run_id: str, tc_full_name: str) -> Optional[Dict]:
        """Get AI analysis for a specific test case in a run."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT aa.id as analysis_id, aa.fingerprint, aa.summary,
                       aa.summary_html, aa.references_json, aa.confidence,
                       aa.category, aa.model_used, aa.tier_used, aa.reasoning,
                       aa.deep_html, aa.token_count, aa.created_at
                FROM test_case_analyses tca
                JOIN ai_analyses aa ON tca.analysis_id = aa.id
                WHERE tca.run_id = ? AND tca.tc_full_name = ?
            """, (run_id, tc_full_name))
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    async def update_deep_analysis(self, analysis_id: int, deep_html: str,
                                    token_count_add: int = 0):
        """Store deep analysis HTML for an existing analysis."""
        async with self.get_connection() as db:
            await db.execute("""
                UPDATE ai_analyses
                SET deep_html = ?, token_count = COALESCE(token_count, 0) + ?
                WHERE id = ?
            """, (deep_html, token_count_add, analysis_id))
            await db.commit()

    async def record_ai_usage(self, month: str, prompt_tokens: int,
                               completion_tokens: int, cost_usd: float) -> Dict:
        """Record AI token usage for budget tracking. Returns current month totals."""
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO ai_usage (month, prompt_tokens, completion_tokens, estimated_cost_usd, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(month) DO UPDATE SET
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                    updated_at = CURRENT_TIMESTAMP
            """, (month, prompt_tokens, completion_tokens, cost_usd))
            await db.commit()

            cursor = await db.execute("""
                SELECT month, prompt_tokens, completion_tokens, estimated_cost_usd, warning_sent
                FROM ai_usage WHERE month = ?
            """, (month,))
            row = await cursor.fetchone()
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))

    async def mark_budget_warning_sent(self, month: str):
        """Mark that a budget warning email has been sent for this month."""
        async with self.get_connection() as db:
            await db.execute("""
                UPDATE ai_usage SET warning_sent = 1, updated_at = CURRENT_TIMESTAMP
                WHERE month = ?
            """, (month,))
            await db.commit()

    async def get_ai_usage_for_month(self, month: str) -> Optional[Dict]:
        """Get AI usage data for a specific month."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT month, prompt_tokens, completion_tokens, estimated_cost_usd, warning_sent
                FROM ai_usage WHERE month = ?
            """, (month,))
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    async def get_setting(self, key: str) -> Optional[str]:
        """Get a setting value by key."""
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        """Set a setting value."""
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, value))
            await db.commit()

    async def delete_setting(self, key: str):
        """Delete a setting."""
        async with self.get_connection() as db:
            await db.execute("DELETE FROM settings WHERE key = ?", (key,))
            await db.commit()

    def _row_to_dict(self, cursor, row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))

    async def create_user(
        self,
        *,
        display_name: str,
        email: Optional[str],
        username: Optional[str],
        password_hash: Optional[str],
        role: str,
        enabled: bool = True,
        auth_source: str = "local",
        oidc_issuer: Optional[str] = None,
        oidc_subject: Optional[str] = None,
        created_at: str,
    ) -> int:
        """Insert a user and return the new id."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                """
                INSERT INTO users (
                    display_name, email, username, password_hash, role, enabled,
                    auth_source, oidc_issuer, oidc_subject, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    display_name,
                    email,
                    username,
                    password_hash,
                    role,
                    1 if enabled else 0,
                    auth_source,
                    oidc_issuer,
                    oidc_subject,
                    created_at,
                ),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_user_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            return self._row_to_dict(cursor, await cursor.fetchone())

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username,))
            return self._row_to_dict(cursor, await cursor.fetchone())

    async def get_user_by_oidc(self, issuer: str, subject: str) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM users WHERE oidc_issuer = ? AND oidc_subject = ?",
                (issuer, subject),
            )
            return self._row_to_dict(cursor, await cursor.fetchone())

    async def list_users(self) -> List[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM users ORDER BY id ASC")
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def update_user(self, user_id: int, **fields) -> bool:
        """Update selected user columns. Returns False if the user does not exist."""
        allowed = {
            "display_name",
            "email",
            "password_hash",
            "role",
            "enabled",
            "last_login_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        if not updates:
            return await self.get_user_by_id(user_id) is not None
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [user_id]
        async with self.get_connection() as db:
            cursor = await db.execute(
                f"UPDATE users SET {assignments} WHERE id = ?",
                values,
            )
            await db.commit()
            return cursor.rowcount > 0

    async def count_enabled_admins(self) -> int:
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND enabled = 1"
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def create_session(
        self,
        *,
        token_hash: str,
        user_id: int,
        created_at: str,
        last_seen_at: str,
        expires_at: str,
    ) -> int:
        async with self.get_connection() as db:
            cursor = await db.execute(
                """
                INSERT INTO sessions (token_hash, user_id, created_at, last_seen_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, created_at, last_seen_at, expires_at),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_session_by_token_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE token_hash = ?",
                (token_hash,),
            )
            return self._row_to_dict(cursor, await cursor.fetchone())

    async def touch_session(self, token_hash: str, last_seen_at: str) -> None:
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
                (last_seen_at, token_hash),
            )
            await db.commit()

    async def delete_session(self, token_hash: str) -> None:
        async with self.get_connection() as db:
            await db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            await db.commit()

    async def delete_sessions_for_user(self, user_id: int) -> None:
        async with self.get_connection() as db:
            await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            await db.commit()

    async def record_login_attempt(
        self,
        *,
        attempted_at: str,
        username: Optional[str],
        client_ip: str,
        username_existed: bool,
    ) -> None:
        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT INTO login_attempts (attempted_at, username, client_ip, username_existed)
                VALUES (?, ?, ?, ?)
                """,
                (attempted_at, username, client_ip, 1 if username_existed else 0),
            )
            await db.commit()

    async def count_login_failures(
        self,
        *,
        since: str,
        username: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> int:
        if username is None and client_ip is None:
            raise ValueError("username or client_ip is required")
        async with self.get_connection() as db:
            if username is not None:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) FROM login_attempts
                    WHERE username = ? AND attempted_at >= ?
                    """,
                    (username, since),
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT COUNT(*) FROM login_attempts
                    WHERE client_ip = ? AND attempted_at >= ?
                    """,
                    (client_ip, since),
                )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def earliest_login_failure(
        self,
        *,
        username: str,
        since: str,
    ) -> Optional[str]:
        async with self.get_connection() as db:
            cursor = await db.execute(
                """
                SELECT attempted_at FROM login_attempts
                WHERE username = ? AND attempted_at >= ?
                ORDER BY attempted_at ASC LIMIT 1
                """,
                (username, since),
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def clear_login_attempts_for_username(self, username: str) -> None:
        async with self.get_connection() as db:
            await db.execute("DELETE FROM login_attempts WHERE username = ?", (username,))
            await db.commit()

    async def create_oidc_state(
        self,
        *,
        state: str,
        nonce: str,
        code_verifier: str,
        next_url: str,
        created_at: str,
    ) -> None:
        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT INTO oidc_states (state, nonce, code_verifier, next_url, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (state, nonce, code_verifier, next_url, created_at),
            )
            await db.commit()

    async def take_oidc_state(self, state: str) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM oidc_states WHERE state = ?", (state,))
            row = self._row_to_dict(cursor, await cursor.fetchone())
            if row:
                await db.execute("DELETE FROM oidc_states WHERE state = ?", (state,))
                await db.commit()
            return row

    async def delete_expired_oidc_states(self, older_than: str) -> None:
        async with self.get_connection() as db:
            await db.execute("DELETE FROM oidc_states WHERE created_at < ?", (older_than,))
            await db.commit()

    async def get_test_case_info(self, run_id: str, tc_full_name: str) -> Optional[Dict]:
        """Get basic info for a single test case."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM test_cases WHERE run_id = ? AND tc_full_name = ?",
                (run_id, tc_full_name)
            )
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    async def get_run_info(self, run_id: str) -> Optional[Dict]:
        """Get basic info for a run."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM test_runs WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    async def get_test_case_info(self, run_id: str, tc_full_name: str) -> Optional[Dict]:
        """Get basic info for a single test case."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM test_cases WHERE run_id = ? AND tc_full_name = ?",
                (run_id, tc_full_name)
            )
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    async def get_run_info(self, run_id: str) -> Optional[Dict]:
        """Get basic info for a run."""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM test_runs WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    async def get_failed_test_cases_for_run(self, run_id: str) -> List[Dict]:
        """Get all failed/error test cases for a run, ordered by priority."""
        async with self.get_connection() as db:
            cursor = await db.execute("""
                SELECT tc.*, tr.target_key
                FROM test_cases tc
                JOIN test_runs tr ON tc.run_id = tr.run_id
                WHERE tc.run_id = ? AND tc.status IN ('failed', 'error')
                ORDER BY tc.start_time
            """, (run_id,))
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    def _comment_row(self, row, columns) -> Dict[str, Any]:
        data = dict(zip(columns, row))
        return data

    async def insert_comment(
        self,
        run_id: str,
        scope: str,
        body: str,
        author_name: str,
        author_user_id: Optional[int] = None,
        tc_id: Optional[str] = None,
        line_start: Optional[int] = None,
        line_end: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Insert a comment and return the stored row."""
        now = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
        async with self.get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO comments
                   (run_id, scope, tc_id, line_start, line_end, body,
                    author_user_id, author_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, scope, tc_id, line_start, line_end, body,
                 author_user_id, author_name, now, now),
            )
            await db.commit()
            comment_id = cursor.lastrowid
        return await self.get_comment_by_id(comment_id)

    async def get_comment_by_id(self, comment_id: int) -> Optional[Dict[str, Any]]:
        async with self.get_connection() as db:
            cursor = await db.execute("SELECT * FROM comments WHERE id = ?", (comment_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))

    async def update_comment_body(self, comment_id: int, body: str) -> Optional[Dict[str, Any]]:
        now = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
        async with self.get_connection() as db:
            await db.execute(
                "UPDATE comments SET body = ?, updated_at = ? WHERE id = ?",
                (body, now, comment_id),
            )
            await db.commit()
        return await self.get_comment_by_id(comment_id)

    async def delete_comment(self, comment_id: int) -> bool:
        async with self.get_connection() as db:
            cursor = await db.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def get_comments_for_run(self, run_id: str, scope: Optional[str] = None,
                                   tc_id: Optional[str] = None) -> List[Dict[str, Any]]:
        async with self.get_connection() as db:
            query = "SELECT * FROM comments WHERE run_id = ?"
            params: List[Any] = [run_id]
            if scope:
                query += " AND scope = ?"
                params.append(scope)
            if tc_id is not None:
                query += " AND tc_id = ?"
                params.append(tc_id)
            query += " ORDER BY line_end ASC, created_at ASC, id ASC"
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in rows]

    async def get_comments_presence(self, run_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Per-run comment flags and first comment ids, for list pages and the run tree."""
        result: Dict[str, Dict[str, Any]] = {
            run_id: {
                "has_comments": False,
                "run_level": False,
                "first_run_comment_id": None,
                "first_comment_id": None,
                "test_cases": {},
            }
            for run_id in run_ids
        }
        if not run_ids:
            return result
        async with self.get_connection() as db:
            cursor = await db.execute(
                """SELECT id, run_id, scope, tc_id, line_end, created_at
                   FROM comments
                   WHERE run_id IN (SELECT value FROM json_each(?))
                   ORDER BY line_end ASC, created_at ASC, id ASC""",
                (json.dumps(run_ids),),
            )
            rows = await cursor.fetchall()
        for comment_id, run_id, scope, tc_id, _line_end, _created in rows:
            entry = result.get(run_id)
            if entry is None:
                continue
            entry["has_comments"] = True
            if entry["first_comment_id"] is None:
                entry["first_comment_id"] = comment_id
            if scope == "run":
                entry["run_level"] = True
                if entry["first_run_comment_id"] is None:
                    entry["first_run_comment_id"] = comment_id
            elif tc_id:
                tc_map = entry["test_cases"]
                if tc_id not in tc_map:
                    tc_map[tc_id] = {"first_comment_id": comment_id}
        return result


# Global database instance - will be initialized with config path
db = None

def initialize_database(data_dir: str = "data"):
    """Initialize the global database instance with the configured data directory."""
    global db
    # Ensure the data directory exists
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)

    db_path = data_path / "test_results.db"
    db = TestResultsDatabase(str(db_path))
    return db

async def log_test_run_finished(run_id: str, status: str):
    """Log a test run completion to the database."""
    return await db.update_test_run(run_id, status=status, end_time=datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z")


async def log_test_case_started(run_id: str, tc_full_name: str, tc_id: str, start_time: str = None):
    """Log a test case start to the database."""
    if start_time is None:
        start_time = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"

    test_case = TestCaseData(
        id=0,  # Will be auto-generated
        run_id=run_id,
        tc_full_name=tc_full_name,
        tc_id=tc_id,
        status="running",
        start_time=start_time,
        end_time=None
    )
    return await db.insert_test_case(test_case)


async def log_test_case_finished(run_id: str, tc_full_name: str, status: str):
    """Log a test case completion to the database."""
    async with db.get_connection() as connection:
        await connection.execute("""
            UPDATE test_cases
            SET status = ?, end_time = ?, updated_at = ?
            WHERE run_id = ? AND tc_full_name = ?
        """, (
            status,
            datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
            datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
            run_id,
            tc_full_name
        ))
        await connection.commit()
        return True
