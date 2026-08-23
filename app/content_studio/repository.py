"""Dedicated SQLite repository for Content Studio.

The database is intentionally separate from Ravuna's user, Gallery and payment data.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import (
    ContentPlanEntry,
    DemoAsset,
    DemoPost,
    DemoResult,
    DemoTransformation,
    PostStatus,
    utc_now,
)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS content_schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_assets (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type='created_for_pixora'),
    license_status TEXT NOT NULL CHECK(license_status IN ('pending','verified','rejected')),
    commercial_allowed INTEGER NOT NULL CHECK(commercial_allowed IN (0,1)),
    created_at TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    category TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    checksum TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_content_assets_category
    ON demo_assets(category, created_at DESC);
CREATE TABLE IF NOT EXISTS demo_transformations (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES demo_assets(id) ON DELETE RESTRICT,
    transformation_type TEXT NOT NULL,
    instructions_en TEXT NOT NULL,
    scene_intent_json TEXT NOT NULL,
    edit_plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_results (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES demo_assets(id) ON DELETE RESTRICT,
    transformation_id TEXT NOT NULL UNIQUE REFERENCES demo_transformations(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    scene_intent_json TEXT NOT NULL,
    edit_plan_json TEXT NOT NULL,
    prompt_en TEXT NOT NULL,
    before_path TEXT NOT NULL,
    after_path TEXT NOT NULL,
    thumbnail_path TEXT NOT NULL,
    watermark_preview_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('needs_review','approved','rejected','archived')),
    quality_issues_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_results_asset
    ON demo_results(asset_id, created_at DESC);
CREATE TABLE IF NOT EXISTS demo_posts (
    id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL UNIQUE REFERENCES demo_results(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    hashtags_json TEXT NOT NULL,
    cta TEXT NOT NULL,
    disclosure TEXT NOT NULL,
    publish_status TEXT NOT NULL CHECK(
        publish_status IN ('draft','needs_review','approved','scheduled','published','archived')
    ),
    scheduled_time TEXT,
    published_time TEXT,
    platform TEXT NOT NULL,
    utm_url TEXT NOT NULL,
    source_code TEXT NOT NULL DEFAULT '',
    generator_prompt_en TEXT NOT NULL,
    published_external_id TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count>=0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_posts_queue
    ON demo_posts(publish_status, scheduled_time, created_at);
CREATE TABLE IF NOT EXISTS content_review_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES demo_posts(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_publication_attempts (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES demo_posts(id) ON DELETE RESTRICT,
    platform TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('preview','dry_run','manual_publish','publish','retry')),
    status TEXT NOT NULL CHECK(status IN ('planned','simulated','succeeded','failed')),
    payload_json TEXT NOT NULL,
    external_id TEXT,
    error_safe TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_publication_post
    ON content_publication_attempts(post_id, created_at DESC);
CREATE TABLE IF NOT EXISTS content_publication_slots (
    platform TEXT NOT NULL,
    scheduled_time TEXT NOT NULL,
    post_id TEXT NOT NULL UNIQUE REFERENCES demo_posts(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    claim_token TEXT,
    claimed_at TEXT,
    PRIMARY KEY(platform, scheduled_time)
);
CREATE TABLE IF NOT EXISTS content_publication_history (
    post_id TEXT PRIMARY KEY REFERENCES demo_posts(id) ON DELETE RESTRICT,
    platform TEXT NOT NULL,
    asset_id TEXT NOT NULL REFERENCES demo_assets(id) ON DELETE RESTRICT,
    asset_checksum TEXT NOT NULL,
    transformation_type TEXT NOT NULL,
    category TEXT NOT NULL,
    before_phash TEXT NOT NULL,
    after_phash TEXT NOT NULL,
    card_phash TEXT NOT NULL,
    normalized_text_hash TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    published_external_id TEXT NOT NULL,
    scheduled_time TEXT,
    published_at TEXT NOT NULL,
    novelty_score REAL NOT NULL,
    duplicate_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_publication_history_recent
    ON content_publication_history(platform, published_at DESC, post_id DESC);
CREATE INDEX IF NOT EXISTS idx_content_publication_history_asset
    ON content_publication_history(platform, asset_checksum);
CREATE TABLE IF NOT EXISTS content_novelty_events (
    id TEXT PRIMARY KEY,
    post_id TEXT REFERENCES demo_posts(id) ON DELETE SET NULL,
    platform TEXT NOT NULL,
    asset_checksum TEXT,
    scheduled_time TEXT,
    reason TEXT NOT NULL,
    novelty_score REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_novelty_events_recent
    ON content_novelty_events(platform, created_at DESC);
CREATE TABLE IF NOT EXISTS content_analytics (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES demo_posts(id) ON DELETE RESTRICT,
    observed_at TEXT NOT NULL,
    views INTEGER NOT NULL CHECK(views>=0),
    clicks INTEGER NOT NULL CHECK(clicks>=0),
    reactions INTEGER NOT NULL CHECK(reactions>=0),
    comments INTEGER NOT NULL CHECK(comments>=0),
    ctr REAL NOT NULL CHECK(ctr>=0),
    conversion_to_bot INTEGER NOT NULL CHECK(conversion_to_bot>=0),
    starts INTEGER NOT NULL DEFAULT 0 CHECK(starts>=0),
    first_photos INTEGER NOT NULL DEFAULT 0 CHECK(first_photos>=0),
    generations INTEGER NOT NULL DEFAULT 0 CHECK(generations>=0),
    payments INTEGER NOT NULL DEFAULT 0 CHECK(payments>=0),
    utm_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_analytics_post
    ON content_analytics(post_id, observed_at DESC);
CREATE TABLE IF NOT EXISTS content_plan_entries (
    id TEXT PRIMARY KEY,
    planned_date TEXT NOT NULL,
    content_type TEXT NOT NULL,
    category TEXT,
    status TEXT NOT NULL CHECK(status IN ('planned','assigned','completed','skipped')),
    post_id TEXT REFERENCES demo_posts(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(planned_date, content_type)
);
"""


ALLOWED_POST_TRANSITIONS: dict[str, frozenset[str]] = {
    PostStatus.DRAFT.value: frozenset({PostStatus.NEEDS_REVIEW.value, PostStatus.ARCHIVED.value}),
    PostStatus.NEEDS_REVIEW.value: frozenset({PostStatus.APPROVED.value, PostStatus.ARCHIVED.value}),
    PostStatus.APPROVED.value: frozenset({PostStatus.SCHEDULED.value, PostStatus.PUBLISHED.value, PostStatus.ARCHIVED.value}),
    PostStatus.SCHEDULED.value: frozenset({PostStatus.APPROVED.value, PostStatus.PUBLISHED.value, PostStatus.ARCHIVED.value}),
    PostStatus.PUBLISHED.value: frozenset({PostStatus.ARCHIVED.value}),
    PostStatus.ARCHIVED.value: frozenset(),
}


class PublicationSlotConflictError(ValueError):
    """Raised when another post durably owns a platform/time slot."""


class PublicationClaimConflictError(RuntimeError):
    """Raised when another scheduler process already owns the send claim."""


class ContentStudioRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        connection = self.connect()
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO content_schema_migrations(version,name,applied_at) VALUES(1,?,?)",
                ("content_studio_v1", utc_now()),
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(demo_posts)").fetchall()
            }
            if "generator_prompt_en" not in columns:
                connection.execute(
                    "ALTER TABLE demo_posts "
                    "ADD COLUMN generator_prompt_en TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "INSERT OR IGNORE INTO content_schema_migrations(version,name,applied_at) VALUES(2,?,?)",
                ("post_generator_prompt_metadata", utc_now()),
            )
            if "source_code" not in columns:
                connection.execute(
                    "ALTER TABLE demo_posts "
                    "ADD COLUMN source_code TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "INSERT OR IGNORE INTO content_schema_migrations(version,name,applied_at) VALUES(3,?,?)",
                ("post_source_attribution", utc_now()),
            )
            analytics_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(content_analytics)").fetchall()
            }
            for name in ("starts", "first_photos", "generations", "payments"):
                if name not in analytics_columns:
                    connection.execute(
                        f"ALTER TABLE content_analytics ADD COLUMN {name} "
                        "INTEGER NOT NULL DEFAULT 0 CHECK(" + name + ">=0)"
                    )
            connection.execute(
                "INSERT OR IGNORE INTO content_schema_migrations(version,name,applied_at) VALUES(4,?,?)",
                ("content_funnel_metrics", utc_now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO content_schema_migrations(version,name,applied_at) VALUES(5,?,?)",
                ("content_publication_novelty_history", utc_now()),
            )
            slot_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(content_publication_slots)"
                ).fetchall()
            }
            if "claim_token" not in slot_columns:
                connection.execute(
                    "ALTER TABLE content_publication_slots ADD COLUMN claim_token TEXT"
                )
            if "claimed_at" not in slot_columns:
                connection.execute(
                    "ALTER TABLE content_publication_slots ADD COLUMN claimed_at TEXT"
                )
            connection.execute(
                """DELETE FROM content_publication_slots
                   WHERE NOT EXISTS(
                       SELECT 1 FROM demo_posts p
                       WHERE p.id=content_publication_slots.post_id
                         AND p.platform=content_publication_slots.platform
                         AND p.scheduled_time=content_publication_slots.scheduled_time
                         AND p.publish_status IN ('scheduled','published')
                   )"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO content_publication_slots(
                       platform,scheduled_time,post_id,created_at
                   )
                   SELECT platform,scheduled_time,MIN(id),?
                   FROM demo_posts
                   WHERE scheduled_time IS NOT NULL
                     AND publish_status IN ('scheduled','published')
                   GROUP BY platform,scheduled_time""",
                (utc_now(),),
            )
        finally:
            connection.close()

    def create_asset(self, asset: DemoAsset) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO demo_assets(
                       id,title,description,source_type,license_status,commercial_allowed,
                       created_at,tags_json,category,storage_path,checksum
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    asset.id,
                    asset.title,
                    asset.description,
                    asset.source_type.value,
                    asset.license_status.value,
                    int(asset.commercial_allowed),
                    asset.created_at,
                    _json(asset.tags),
                    asset.category.value,
                    asset.storage_path,
                    asset.checksum,
                ),
            )

    def create_bundle(
        self,
        transformation: DemoTransformation,
        result: DemoResult,
        post: DemoPost,
    ) -> None:
        """Persist transformation, result and review post atomically."""

        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO demo_transformations(
                       id,asset_id,transformation_type,instructions_en,
                       scene_intent_json,edit_plan_json,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    transformation.id,
                    transformation.asset_id,
                    transformation.transformation_type.value,
                    transformation.instructions_en,
                    _json(transformation.scene_intent),
                    _json(transformation.edit_plan),
                    transformation.created_at,
                ),
            )
            connection.execute(
                """INSERT INTO demo_results(
                       id,asset_id,transformation_id,provider,scene_intent_json,
                       edit_plan_json,prompt_en,before_path,after_path,thumbnail_path,
                       watermark_preview_path,status,quality_issues_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.id,
                    result.asset_id,
                    result.transformation_id,
                    result.provider,
                    _json(result.scene_intent),
                    _json(result.edit_plan),
                    result.prompt_en,
                    result.before_path,
                    result.after_path,
                    result.thumbnail_path,
                    result.watermark_preview_path,
                    result.status.value,
                    _json(tuple(issue.value for issue in result.quality_issues)),
                    result.created_at,
                ),
            )
            connection.execute(
                """INSERT INTO demo_posts(
                       id,result_id,title,body,hashtags_json,cta,disclosure,publish_status,
                       scheduled_time,published_time,platform,utm_url,source_code,generator_prompt_en,
                       published_external_id,retry_count,last_error,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    post.id,
                    post.result_id,
                    post.title,
                    post.body,
                    _json(post.hashtags),
                    post.cta,
                    post.disclosure,
                    post.publish_status.value,
                    post.scheduled_time,
                    post.published_time,
                    post.platform,
                    post.utm_url,
                    post.source_code,
                    post.generator_prompt_en,
                    post.published_external_id,
                    post.retry_count,
                    post.last_error,
                    post.created_at,
                    post.updated_at,
                ),
            )

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._required("SELECT * FROM demo_assets WHERE id=?", (asset_id,), "DemoAsset")

    def find_asset_by_checksum(self, checksum: str) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM demo_assets WHERE checksum=?", (checksum,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def get_result(self, result_id: str) -> dict[str, Any]:
        return self._required("SELECT * FROM demo_results WHERE id=?", (result_id,), "DemoResult")

    def get_post(self, post_id: str) -> dict[str, Any]:
        return self._required("SELECT * FROM demo_posts WHERE id=?", (post_id,), "DemoPost")

    def post_exists(self, post_id: str) -> bool:
        connection = self.connect()
        try:
            return bool(
                connection.execute(
                    "SELECT 1 FROM demo_posts WHERE id=?", (post_id,)
                ).fetchone()
            )
        finally:
            connection.close()

    def published_posts(self, limit: int = 500) -> list[dict[str, Any]]:
        return self.queue(PostStatus.PUBLISHED.value, limit=limit)

    def scheduled_through(self) -> str | None:
        connection = self.connect()
        try:
            row = connection.execute(
                """SELECT MAX(scheduled_time) FROM demo_posts
                   WHERE publish_status IN ('scheduled','published')"""
            ).fetchone()
            return str(row[0]) if row and row[0] else None
        finally:
            connection.close()

    def post_with_result(self, post_id: str) -> dict[str, Any]:
        return self._required(
            """SELECT p.*,r.edit_plan_json,r.watermark_preview_path,r.quality_issues_json
               FROM demo_posts p JOIN demo_results r ON r.id=p.result_id
               WHERE p.id=?""",
            (post_id,),
            "DemoPost",
        )

    def novelty_candidate(self, post_id: str) -> dict[str, Any]:
        return self._required(
            """SELECT p.*,r.asset_id,r.before_path,r.after_path,
                      r.watermark_preview_path,t.transformation_type,
                      a.checksum asset_checksum,a.category asset_category
               FROM demo_posts p
               JOIN demo_results r ON r.id=p.result_id
               JOIN demo_transformations t ON t.id=r.transformation_id
               JOIN demo_assets a ON a.id=r.asset_id
               WHERE p.id=?""",
            (post_id,),
            "DemoPost",
        )

    def publication_history(
        self, *, platform: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        connection = self.connect()
        try:
            if platform:
                rows = connection.execute(
                    """SELECT * FROM content_publication_history
                       WHERE platform=? ORDER BY published_at DESC,post_id DESC LIMIT ?""",
                    (platform, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM content_publication_history
                       ORDER BY published_at DESC,post_id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def published_candidates_missing_history(
        self, limit: int = 500
    ) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """SELECT p.* FROM demo_posts p
                   LEFT JOIN content_publication_history h ON h.post_id=p.id
                   WHERE p.publish_status='published' AND h.post_id IS NULL
                   ORDER BY COALESCE(p.published_time,p.updated_at),p.id LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def record_publication_history(
        self,
        fingerprint: dict[str, Any],
        *,
        external_id: str,
        published_at: str,
        novelty_score: float,
        duplicate_reason: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            self._insert_publication_history(
                connection,
                fingerprint,
                external_id=external_id,
                published_at=published_at,
                novelty_score=novelty_score,
                duplicate_reason=duplicate_reason,
            )

    def record_novelty_event(
        self,
        event_id: str,
        *,
        platform: str,
        reason: str,
        novelty_score: float = 0.0,
        post_id: str | None = None,
        asset_checksum: str | None = None,
        scheduled_time: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO content_novelty_events(
                       id,post_id,platform,asset_checksum,scheduled_time,reason,
                       novelty_score,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    post_id,
                    platform,
                    asset_checksum,
                    scheduled_time,
                    reason,
                    novelty_score,
                    utc_now(),
                ),
            )

    def slot_has_post(self, platform: str, scheduled_time: str) -> bool:
        connection = self.connect()
        try:
            if connection.execute(
                """SELECT 1 FROM content_publication_slots
                   WHERE platform=? AND scheduled_time=? LIMIT 1""",
                (platform, scheduled_time),
            ).fetchone():
                return True
            if connection.execute(
                """SELECT 1 FROM demo_posts
                   WHERE platform=? AND scheduled_time=?
                     AND publish_status IN ('scheduled','published') LIMIT 1""",
                (platform, scheduled_time),
            ).fetchone():
                return True
            if connection.execute(
                """SELECT 1 FROM content_novelty_events
                   WHERE platform=? AND scheduled_time=?
                     AND reason IN (
                         'candidate_pool_exhausted','recent_category_saturation'
                     ) LIMIT 1""",
                (platform, scheduled_time),
            ).fetchone():
                return True
            if platform != "max":
                return False
            return bool(
                connection.execute(
                    """SELECT 1 FROM content_publication_history
                       WHERE platform='max'
                         AND substr(COALESCE(scheduled_time,published_at),1,10)=substr(?,1,10)
                       LIMIT 1""",
                    (scheduled_time,),
                ).fetchone()
            )
        finally:
            connection.close()

    def publication_slot_owner(
        self, platform: str, scheduled_time: str
    ) -> str | None:
        connection = self.connect()
        try:
            row = connection.execute(
                """SELECT post_id FROM content_publication_slots
                   WHERE platform=? AND scheduled_time=?""",
                (platform, scheduled_time),
            ).fetchone()
            return str(row[0]) if row is not None else None
        finally:
            connection.close()

    def claim_publication(self, post_id: str, claim_token: str) -> bool:
        """Atomically claim one scheduled send across concurrent timer processes."""

        if not claim_token:
            raise ValueError("publication claim token is required")
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT publish_status FROM demo_posts WHERE id=?", (post_id,)
            ).fetchone()
            if current is None:
                raise LookupError("DemoPost not found")
            if current["publish_status"] != PostStatus.SCHEDULED.value:
                return False
            updated = connection.execute(
                """UPDATE content_publication_slots
                   SET claim_token=?,claimed_at=?
                   WHERE post_id=? AND claim_token IS NULL""",
                (claim_token, utc_now(), post_id),
            )
            return updated.rowcount == 1

    def release_publication_claim(self, post_id: str, claim_token: str) -> None:
        """Release only the caller's claim after a confirmed pre-send/send failure."""

        with self.transaction() as connection:
            connection.execute(
                """UPDATE content_publication_slots
                   SET claim_token=NULL,claimed_at=NULL
                   WHERE post_id=? AND claim_token=?""",
                (post_id, claim_token),
            )

    def reserved_asset_checksums(self, platform: str) -> set[str]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """SELECT DISTINCT a.checksum
                   FROM demo_posts p
                   JOIN demo_results r ON r.id=p.result_id
                   JOIN demo_assets a ON a.id=r.asset_id
                   WHERE p.platform=?
                     AND p.publish_status IN ('scheduled','published')""",
                (platform,),
            ).fetchall()
            return {str(row[0]) for row in rows}
        finally:
            connection.close()

    def recent_publication_categories(
        self, platform: str, limit: int = 3
    ) -> list[str]:
        return [
            str(row["category"])
            for row in self.publication_history(platform=platform, limit=limit)
        ]

    def unpublished_asset_categories(
        self, platform: str, *, excluding_checksum: str
    ) -> set[str]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """SELECT DISTINCT a.category
                   FROM demo_assets a
                   WHERE a.checksum<>?
                     AND NOT EXISTS(
                         SELECT 1 FROM content_publication_history h
                         WHERE h.platform=? AND h.asset_checksum=a.checksum
                     )""",
                (excluding_checksum, platform),
            ).fetchall()
            return {str(row[0]) for row in rows}
        finally:
            connection.close()

    def complete_publication(
        self,
        *,
        attempt_id: str,
        post_id: str,
        platform: str,
        mode: str,
        payload: dict[str, Any],
        external_id: str,
        fingerprint: dict[str, Any],
        novelty_score: float,
        claim_token: str | None = None,
        increment_retry: bool = False,
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT publish_status FROM demo_posts WHERE id=?", (post_id,)
            ).fetchone()
            if current is None:
                raise LookupError("DemoPost not found")
            if current["publish_status"] not in {
                PostStatus.APPROVED.value,
                PostStatus.SCHEDULED.value,
            }:
                raise ValueError("publication requires an approved or scheduled post")
            if current["publish_status"] == PostStatus.SCHEDULED.value:
                claimed = connection.execute(
                    """SELECT claim_token FROM content_publication_slots
                       WHERE post_id=?""",
                    (post_id,),
                ).fetchone()
                if (
                    claimed is None
                    or not claim_token
                    or claimed["claim_token"] != claim_token
                ):
                    raise PublicationClaimConflictError(
                        "scheduled publication claim is not owned by this process"
                    )
            connection.execute(
                """INSERT INTO content_publication_attempts(
                       id,post_id,platform,mode,status,payload_json,external_id,
                       error_safe,created_at,completed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    post_id,
                    platform,
                    mode,
                    "succeeded",
                    _json(payload),
                    external_id,
                    None,
                    now,
                    now,
                ),
            )
            connection.execute(
                """UPDATE demo_posts SET publish_status='published',published_time=?,
                       published_external_id=?,last_error=NULL,retry_count=retry_count+?,
                       updated_at=? WHERE id=?""",
                (now, external_id, int(increment_retry), now, post_id),
            )
            connection.execute(
                """UPDATE content_publication_slots
                   SET claim_token=NULL,claimed_at=NULL WHERE post_id=?""",
                (post_id,),
            )
            self._insert_publication_history(
                connection,
                fingerprint,
                external_id=external_id,
                published_at=now,
                novelty_score=novelty_score,
                duplicate_reason=None,
            )

    @staticmethod
    def _insert_publication_history(
        connection: sqlite3.Connection,
        fingerprint: dict[str, Any],
        *,
        external_id: str,
        published_at: str,
        novelty_score: float,
        duplicate_reason: str | None,
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO content_publication_history(
                   post_id,platform,asset_id,asset_checksum,transformation_type,
                   category,before_phash,after_phash,card_phash,
                   normalized_text_hash,normalized_text,published_external_id,
                   scheduled_time,published_at,novelty_score,duplicate_reason
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fingerprint["post_id"],
                fingerprint["platform"],
                fingerprint["asset_id"],
                fingerprint["asset_checksum"],
                fingerprint["transformation_type"],
                fingerprint["category"],
                fingerprint["before_phash"],
                fingerprint["after_phash"],
                fingerprint["card_phash"],
                fingerprint["normalized_text_hash"],
                fingerprint["normalized_text"],
                external_id,
                fingerprint.get("scheduled_time"),
                published_at,
                novelty_score,
                duplicate_reason,
            ),
        )

    def performance_rows(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """WITH latest AS (
                       SELECT a.*,ROW_NUMBER() OVER(
                           PARTITION BY a.post_id ORDER BY a.observed_at DESC,a.id DESC
                       ) AS rank
                       FROM content_analytics a
                   )
                   SELECT p.id,p.platform,p.source_code,r.edit_plan_json,
                          COALESCE(l.views,0) views,COALESCE(l.starts,0) starts,
                          COALESCE(l.first_photos,0) first_photos,
                          COALESCE(l.generations,0) generations,
                          COALESCE(l.payments,0) payments
                   FROM demo_posts p
                   JOIN demo_results r ON r.id=p.result_id
                   LEFT JOIN latest l ON l.post_id=p.id AND l.rank=1
                   WHERE p.publish_status='published'"""
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def queue(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        connection = self.connect()
        try:
            if status:
                rows = connection.execute(
                    "SELECT * FROM demo_posts WHERE publish_status=? ORDER BY COALESCE(scheduled_time,created_at),created_at LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM demo_posts ORDER BY COALESCE(scheduled_time,created_at),created_at LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def due_posts(self, now: str, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        connection = self.connect()
        try:
            rows = connection.execute(
                """SELECT * FROM demo_posts
                   WHERE publish_status='scheduled'
                     AND scheduled_time IS NOT NULL
                     AND scheduled_time<=?
                   ORDER BY scheduled_time,created_at,id
                   LIMIT ?""",
                (now, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def transition_post(
        self,
        post_id: str,
        target: PostStatus,
        *,
        scheduled_time: str | None = None,
        published_time: str | None = None,
        external_id: str | None = None,
        last_error: str | None = None,
        increment_retry: bool = False,
    ) -> dict[str, Any]:
        try:
            with self.transaction() as connection:
                current = connection.execute(
                    """SELECT publish_status,retry_count,platform,scheduled_time
                       FROM demo_posts WHERE id=?""",
                    (post_id,),
                ).fetchone()
                if current is None:
                    raise LookupError("DemoPost not found")
                if target.value not in ALLOWED_POST_TRANSITIONS[current["publish_status"]]:
                    raise ValueError(
                        f"invalid post transition {current['publish_status']} -> {target.value}"
                    )
                if target is PostStatus.SCHEDULED:
                    if not scheduled_time:
                        raise ValueError("scheduled transition requires scheduled_time")
                    connection.execute(
                        """INSERT INTO content_publication_slots(
                               platform,scheduled_time,post_id,created_at
                           ) SELECT platform,?,?,? FROM demo_posts WHERE id=?""",
                        (scheduled_time, post_id, utc_now(), post_id),
                    )
                elif target is PostStatus.ARCHIVED:
                    connection.execute(
                        "DELETE FROM content_publication_slots WHERE post_id=?",
                        (post_id,),
                    )
                connection.execute(
                    """UPDATE demo_posts SET publish_status=?,scheduled_time=?,published_time=?,
                           published_external_id=COALESCE(?,published_external_id),last_error=?,
                           retry_count=retry_count+?,updated_at=? WHERE id=?""",
                    (
                        target.value,
                        scheduled_time,
                        published_time,
                        external_id,
                        last_error,
                        int(increment_retry),
                        utc_now(),
                        post_id,
                    ),
                )
                if target is PostStatus.ARCHIVED and current["scheduled_time"]:
                    connection.execute(
                        """INSERT OR IGNORE INTO content_publication_slots(
                               platform,scheduled_time,post_id,created_at
                           )
                           SELECT platform,scheduled_time,MIN(id),?
                           FROM demo_posts
                           WHERE platform=? AND scheduled_time=?
                             AND publish_status IN ('scheduled','published')
                           GROUP BY platform,scheduled_time""",
                        (
                            utc_now(),
                            current["platform"],
                            current["scheduled_time"],
                        ),
                    )
        except sqlite3.IntegrityError as error:
            if target is PostStatus.SCHEDULED:
                raise PublicationSlotConflictError(
                    "publication slot is already reserved"
                ) from error
            raise
        return self.get_post(post_id)

    def add_review(self, post_id: str, action: str, reviewer: str, reason: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO content_review_actions(post_id,action,reviewer,reason,created_at) VALUES(?,?,?,?,?)",
                (post_id, action, reviewer, reason, utc_now()),
            )

    def approve_post(self, post_id: str, reviewer: str, reason: str) -> dict[str, Any]:
        """Approve the result and its post with one durable review record."""

        with self.transaction() as connection:
            current = connection.execute(
                """SELECT p.publish_status,p.result_id,r.status result_status
                   FROM demo_posts p
                   JOIN demo_results r ON r.id=p.result_id
                   WHERE p.id=?""",
                (post_id,),
            ).fetchone()
            if current is None:
                raise LookupError("DemoPost not found")
            if current["publish_status"] != PostStatus.NEEDS_REVIEW.value:
                raise ValueError("only a needs_review post can be approved")
            if current["result_status"] != "needs_review":
                raise ValueError("only a needs_review result can be approved")
            now = utc_now()
            connection.execute(
                "UPDATE demo_results SET status='approved' WHERE id=?",
                (current["result_id"],),
            )
            connection.execute(
                """UPDATE demo_posts SET publish_status='approved',updated_at=?
                   WHERE id=?""",
                (now, post_id),
            )
            connection.execute(
                """INSERT INTO content_review_actions(
                       post_id,action,reviewer,reason,created_at
                   ) VALUES(?,?,?,?,?)""",
                (post_id, "approved", reviewer, reason, now),
            )
        return self.get_post(post_id)

    def add_publication_attempt(
        self,
        attempt_id: str,
        post_id: str,
        platform: str,
        mode: str,
        status: str,
        payload: dict[str, Any],
        *,
        external_id: str | None = None,
        error_safe: str | None = None,
        completed: bool = True,
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO content_publication_attempts(
                       id,post_id,platform,mode,status,payload_json,external_id,error_safe,
                       created_at,completed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    post_id,
                    platform,
                    mode,
                    status,
                    _json(payload),
                    external_id,
                    error_safe,
                    now,
                    now if completed else None,
                ),
            )

    def add_analytics(
        self,
        analytics_id: str,
        post_id: str,
        *,
        views: int,
        clicks: int,
        reactions: int,
        comments: int,
        conversion_to_bot: int,
        starts: int = 0,
        first_photos: int = 0,
        generations: int = 0,
        payments: int = 0,
        utm: dict[str, str],
    ) -> None:
        metrics = (
            views,
            clicks,
            reactions,
            comments,
            conversion_to_bot,
            starts,
            first_photos,
            generations,
            payments,
        )
        if any(value < 0 for value in metrics):
            raise ValueError("analytics values must not be negative")
        ctr = clicks / views if views else 0.0
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO content_analytics(
                       id,post_id,observed_at,views,clicks,reactions,comments,ctr,
                       conversion_to_bot,starts,first_photos,generations,payments,utm_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    analytics_id,
                    post_id,
                    utc_now(),
                    views,
                    clicks,
                    reactions,
                    comments,
                    ctr,
                    conversion_to_bot,
                    starts,
                    first_photos,
                    generations,
                    payments,
                    _json(utm),
                ),
            )

    def analytics_summary(self, post_id: str | None = None) -> dict[str, Any]:
        connection = self.connect()
        try:
            where = " WHERE post_id=?" if post_id else ""
            params: Sequence[Any] = (post_id,) if post_id else ()
            snapshots = int(
                connection.execute(
                    "SELECT COUNT(*) FROM content_analytics" + where,
                    params,
                ).fetchone()[0]
            )
            row = connection.execute(
                """WITH ranked AS (
                       SELECT *,ROW_NUMBER() OVER(
                           PARTITION BY post_id ORDER BY observed_at DESC,id DESC
                       ) AS rank
                       FROM content_analytics"""
                + where
                + """
                   )
                   SELECT COALESCE(SUM(views),0) views,
                          COALESCE(SUM(clicks),0) clicks,
                          COALESCE(SUM(reactions),0) reactions,
                           COALESCE(SUM(comments),0) comments,
                           COALESCE(SUM(conversion_to_bot),0) conversions,
                           COALESCE(SUM(starts),0) starts,
                           COALESCE(SUM(first_photos),0) first_photos,
                           COALESCE(SUM(generations),0) generations,
                           COALESCE(SUM(payments),0) payments
                   FROM ranked WHERE rank=1""",
                params,
            ).fetchone()
            result = dict(row)
            result["snapshots"] = snapshots
            result["ctr"] = result["clicks"] / result["views"] if result["views"] else 0.0
            return result
        finally:
            connection.close()

    def create_plan(self, entries: Sequence[ContentPlanEntry]) -> int:
        with self.transaction() as connection:
            before = connection.total_changes
            for entry in entries:
                connection.execute(
                    """INSERT OR IGNORE INTO content_plan_entries(
                           id,planned_date,content_type,category,status,post_id,created_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        entry.id,
                        entry.planned_date.isoformat(),
                        entry.content_type,
                        entry.category.value if entry.category else None,
                        entry.status,
                        entry.post_id,
                        utc_now(),
                    ),
                )
            return connection.total_changes - before

    def status(self) -> dict[str, Any]:
        connection = self.connect()
        try:
            counts: dict[str, int] = {}
            for table, key in (
                ("demo_assets", "assets"),
                ("demo_transformations", "transformations"),
                ("demo_results", "results"),
                ("demo_posts", "posts"),
                ("content_plan_entries", "plan_entries"),
                ("content_publication_attempts", "publication_attempts"),
                ("content_publication_slots", "publication_slots"),
                ("content_publication_history", "publication_history"),
                ("content_novelty_events", "novelty_events"),
            ):
                counts[key] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            counts["published_posts"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM demo_posts WHERE publish_status='published'"
                ).fetchone()[0]
            )
            counts["needs_review"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM demo_posts WHERE publish_status='needs_review'"
                ).fetchone()[0]
            )
            counts["schema_version"] = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version),0) FROM content_schema_migrations"
                ).fetchone()[0]
            )
            counts["quick_check"] = connection.execute("PRAGMA quick_check").fetchone()[0]
            return counts
        finally:
            connection.close()

    def _required(
        self, query: str, params: Sequence[Any], entity: str
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            row = connection.execute(query, params).fetchone()
            if row is None:
                raise LookupError(f"{entity} not found")
            return dict(row)
        finally:
            connection.close()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
