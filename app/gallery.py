"""Personal AI studio: gallery, version history, collections and retention API."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from uuid import uuid4

from app.config import Settings
from app.database import Database
from app.domain import InvalidInputError
from app.edit_intent import EditPlan
from app.processing_modes import ProcessingPlan, legacy_processing_plan
from app.storage import PrivateStorage
from app.provider_context import ProviderContextService


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class GalleryItem:
    id: str
    user_id: str
    title: str
    scenario_id: Optional[str]
    current_best_version_id: Optional[str]
    favorite: bool
    deleted: bool
    folder_id: Optional[str]
    generation_count: int
    last_opened_at: Optional[str]
    unlock_status: str
    cover_preview_path: Optional[Path]
    secondary_source_path: Optional[Path]


@dataclass(frozen=True)
class GalleryVersion:
    id: str
    gallery_item_id: str
    version_number: int
    parent_version_id: Optional[str]
    source_path: Path
    preview_path: Optional[Path]
    original_path: Optional[Path]
    prompt: str
    correction_prompt: Optional[str]
    effective_prompt: str
    favorite: bool
    unlock_status: str
    status: str
    edit_plan: EditPlan
    provider_prompt: str
    source_version_id: Optional[str]
    prompt_builder_version: str
    processing_plan: ProcessingPlan
    provider_mode: str
    provider_response_id: Optional[str]
    provider_context_id: Optional[str]
    context_depth: int
    context_fallback_reason: Optional[str]
    secondary_source_path: Optional[Path]


class GalleryService:
    def __init__(
        self,
        database: Database,
        storage: PrivateStorage,
        settings: Settings,
        clock: Callable[[], datetime] = _now,
        provider_context_service: Optional[ProviderContextService] = None,
    ) -> None:
        self.database = database
        self.storage = storage
        self.settings = settings
        self.clock = clock
        self.provider_context_service = provider_context_service

    @staticmethod
    def _item(row: sqlite3.Row) -> GalleryItem:
        return GalleryItem(
            row["id"], row["user_id"], row["title"], row["scenario_id"],
            row["current_best_version_id"], bool(row["favorite"]), bool(row["deleted"]),
            row["folder_id"], row["generation_count"], row["last_opened_at"],
            row["unlock_status"], Path(row["cover_preview_path"]) if row["cover_preview_path"] else None,
            Path(row["secondary_source_path"]) if row["secondary_source_path"] else None,
        )

    @staticmethod
    def _version(row: sqlite3.Row) -> GalleryVersion:
        plan = (
            EditPlan.from_json(row["edit_plan_json"])
            if row["edit_plan_json"]
            else EditPlan.from_legacy(
                row["correction_prompt"] or row["prompt"],
                correction=bool(row["correction_prompt"]),
                correction_target_version_id=row["parent_version_id"],
            )
        )
        processing_plan = (
            ProcessingPlan.from_json(row["processing_plan_json"])
            if row["processing_plan_json"]
            else legacy_processing_plan(row["provider"], row["model"])
        )
        return GalleryVersion(
            row["id"], row["gallery_item_id"], row["version_number"],
            row["parent_version_id"], Path(row["source_path"]),
            Path(row["preview_watermarked_path"]) if row["preview_watermarked_path"] else None,
            Path(row["original_path"])
            if row["original_path"] and row["unlock_status"] == "unlocked"
            else None,
            row["prompt"], row["correction_prompt"], row["effective_prompt"],
            bool(row["favorite"]), row["unlock_status"], row["status"],
            plan, row["provider_prompt"] or row["effective_prompt"],
            row["source_version_id"], row["prompt_builder_version"] or "legacy-concatenation",
            processing_plan,
            row["provider_mode"] or "stateless", row["provider_response_id"],
            row["provider_context_id"], int(row["context_depth"] or 0),
            row["context_fallback_reason"],
            Path(row["secondary_source_path"]) if row["secondary_source_path"] else None,
        )

    def create_item(
        self,
        user_id: str,
        title: str,
        source: Path,
        scenario_id: Optional[str] = None,
        folder_id: Optional[str] = None,
    ) -> GalleryItem:
        clean_title = title.strip()
        if not clean_title or len(clean_title) > 200:
            raise InvalidInputError("Gallery title is empty or too long")
        item_id = uuid4().hex
        stored_source, _digest, _size = self.storage.create_gallery_item_source(
            user_id, item_id, source
        )
        now = self.clock()
        retention = now + timedelta(days=self.settings.demo_retention_days)
        with self.database.transaction() as connection:
            self._require_user(connection, user_id)
            gallery_id = self._ensure_gallery(connection, user_id, now)
            if folder_id:
                self._require_collection(connection, user_id, folder_id)
            connection.execute(
                """INSERT INTO gallery_items(
                       id,gallery_id,user_id,title,created_at,updated_at,scenario_id,original_source_path,
                       storage_root_path,folder_id,last_opened_at,retention_until
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id, gallery_id, user_id, clean_title, _iso(now), _iso(now), scenario_id,
                    str(stored_source), str(self.storage.gallery_item_root(user_id, item_id)),
                    folder_id, _iso(now), _iso(retention),
                ),
            )
            row = connection.execute("SELECT * FROM gallery_items WHERE id=?", (item_id,)).fetchone()
        return self._item(row)

    def create_linked_demo_item(
        self,
        connection: sqlite3.Connection,
        user_id: str,
        session_id: str,
        source_path: Path,
        now: datetime,
    ) -> str:
        gallery_id = self._ensure_gallery(connection, user_id, now)
        item_id = uuid4().hex
        stored_source, _digest, _size = self.storage.create_gallery_item_source(
            user_id, item_id, source_path
        )
        root = self.storage.gallery_item_root(user_id, item_id)
        connection.execute(
            """INSERT INTO gallery_items(
                   id,gallery_id,user_id,title,created_at,updated_at,original_source_path,storage_root_path,
                   last_opened_at,retention_until
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                item_id, gallery_id, user_id, "Моя работа", _iso(now), _iso(now), str(stored_source),
                str(root), _iso(now),
                _iso(now + timedelta(days=self.settings.demo_retention_days)),
            ),
        )
        connection.execute(
            "UPDATE demo_sessions SET gallery_item_id=? WHERE id=?", (item_id, session_id)
        )
        return item_id

    def compose_prompt(
        self,
        connection: sqlite3.Connection,
        gallery_item_id: str,
        requested_prompt: str,
        parent_version_id: Optional[str],
        correction: bool,
    ) -> tuple[str, Optional[str], Optional[str]]:
        parent = None
        if parent_version_id:
            parent = connection.execute(
                "SELECT * FROM gallery_versions WHERE id=? AND gallery_item_id=?",
                (parent_version_id, gallery_item_id),
            ).fetchone()
            if parent is None:
                raise InvalidInputError("Parent gallery version does not belong to this work")
        elif correction:
            parent = connection.execute(
                """SELECT * FROM gallery_versions WHERE gallery_item_id=?
                   ORDER BY version_number DESC LIMIT 1""",
                (gallery_item_id,),
            ).fetchone()
            if parent is None:
                raise InvalidInputError("Correction requires an existing version")
            parent_version_id = parent["id"]
        if correction:
            effective = f"{parent['effective_prompt']}\nИсправление: {requested_prompt}"
            return effective, parent_version_id, requested_prompt
        if parent is not None:
            return parent["effective_prompt"], parent_version_id, None
        return requested_prompt, None, None

    def record_attempt_version(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        gallery_item_id: str,
    ) -> str:
        existing = connection.execute(
            "SELECT id FROM gallery_versions WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if existing:
            return existing["id"]
        attempt = connection.execute(
            "SELECT * FROM generation_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        item = connection.execute(
            "SELECT * FROM gallery_items WHERE id=?", (gallery_item_id,)
        ).fetchone()
        if not attempt or not item:
            raise InvalidInputError("Generation attempt or gallery work is missing")
        number = connection.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM gallery_versions WHERE gallery_item_id=?",
            (gallery_item_id,),
        ).fetchone()[0]
        version_id = uuid4().hex
        unlock = "unlocked" if attempt["result_unlocked"] else "demo"
        connection.execute(
            """INSERT INTO gallery_versions(
                   id,gallery_item_id,attempt_id,version_number,parent_version_id,source_path,
                   secondary_source_path,
                   prompt,correction_prompt,effective_prompt,provider,model,
                   preview_watermarked_path,original_path,created_at,processing_time_ms,
                   estimated_cost,status,unlock_status,edit_plan_json,provider_prompt,
                   source_version_id,prompt_builder_version
                   ,selected_mode,mode_reason,mode_confidence,fallback_mode,
                   asset_source_type,asset_id,asset_checksum,mask_strategy,processing_provider,
                   processing_provider_model,processing_pipeline_version,processing_plan_json
                   ,provider_mode,provider_response_id,provider_conversation_id,
                   provider_context_id,context_parent_response_id,context_depth,
                   context_fallback_reason,provider_http_status,provider_duration_ms
                   ,provider_parent_response_id,provider_context_used,
                   provider_context_fallback_reason,input_version_id,
                   effective_prompt_hash,scene_intent_hash,provider_request_id,
                   provider_usage_json
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id, gallery_item_id, attempt_id, number,
                attempt["parent_version_id"], attempt["source_path"],
                attempt["secondary_source_path"], attempt["prompt"],
                attempt["correction_prompt"], attempt["effective_prompt"] or attempt["prompt"],
                attempt["provider"], attempt["model"], attempt["demo_result_path"],
                attempt["original_result_path"], attempt["completed_at"] or attempt["created_at"],
                attempt["duration_ms"], attempt["estimated_cost"], attempt["status"], unlock,
                attempt["edit_plan_json"], attempt["provider_prompt"],
                attempt["source_version_id"], attempt["prompt_builder_version"],
                attempt["selected_mode"], attempt["mode_reason"], attempt["mode_confidence"],
                attempt["fallback_mode"], attempt["asset_source_type"], attempt["asset_id"],
                attempt["asset_checksum"],
                attempt["mask_strategy"], attempt["processing_provider"],
                attempt["processing_provider_model"], attempt["processing_pipeline_version"],
                attempt["processing_plan_json"],
                attempt["provider_mode"], attempt["provider_response_id"],
                attempt["provider_conversation_id"], attempt["provider_context_id"],
                attempt["context_parent_response_id"], attempt["context_depth"],
                attempt["context_fallback_reason"], attempt["provider_http_status"],
                attempt["provider_duration_ms"],
                attempt["context_parent_response_id"],
                int(attempt["provider_mode"] == "responses"),
                attempt["context_fallback_reason"], attempt["source_version_id"],
                hashlib.sha256(
                    (attempt["effective_prompt"] or attempt["prompt"]).encode("utf-8")
                ).hexdigest(),
                hashlib.sha256((attempt["edit_plan_json"] or "").encode("utf-8")).hexdigest(),
                attempt["external_request_id"], attempt["usage_json"],
            ),
        )
        connection.execute(
            """UPDATE gallery_items SET current_best_version_id=?,cover_preview_path=?,
               preview_small_path=?,preview_large_path=?,generation_count=generation_count+1,
               scenario_id=COALESCE(?,scenario_id),updated_at=?,last_opened_at=? WHERE id=?""",
            (
                version_id, attempt["demo_result_path"], attempt["demo_result_path"],
                attempt["demo_result_path"], attempt["scenario_id"], attempt["completed_at"],
                attempt["completed_at"], gallery_item_id,
            ),
        )
        return version_id

    def record_feedback(
        self,
        user_id: str,
        version_id: str,
        sentiment: str,
        reason_category: Optional[str] = None,
    ) -> None:
        if sentiment not in {"positive", "negative"}:
            raise InvalidInputError("Unknown feedback sentiment")
        now = _iso(self.clock())
        with self.database.transaction() as connection:
            self._require_version(connection, user_id, version_id)
            connection.execute(
                """INSERT INTO version_feedback(
                       version_id,user_id,sentiment,reason_category,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(version_id,user_id) DO UPDATE SET
                       sentiment=excluded.sentiment,
                       reason_category=excluded.reason_category,
                       updated_at=excluded.updated_at""",
                (version_id, user_id, sentiment, reason_category, now, now),
            )

    def list_versions(self, user_id: str, item_id: str) -> list[GalleryVersion]:
        with self.database.read() as connection:
            self._require_item(connection, user_id, item_id, include_deleted=True)
            rows = connection.execute(
                "SELECT * FROM gallery_versions WHERE gallery_item_id=? ORDER BY version_number",
                (item_id,),
            ).fetchall()
        return [self._version(row) for row in rows]

    def set_version_favorite(self, user_id: str, version_id: str, favorite: bool) -> None:
        with self.database.transaction() as connection:
            row = self._require_version(connection, user_id, version_id)
            connection.execute(
                "UPDATE gallery_versions SET favorite=? WHERE id=?", (int(favorite), version_id)
            )
            if favorite:
                connection.execute(
                    "UPDATE gallery_items SET favorite=1,updated_at=? WHERE id=?",
                    (_iso(self.clock()), row["gallery_item_id"]),
                )

    def set_current_best(self, user_id: str, item_id: str, version_id: str) -> None:
        with self.database.transaction() as connection:
            self._require_item(connection, user_id, item_id)
            version = connection.execute(
                "SELECT * FROM gallery_versions WHERE id=? AND gallery_item_id=?",
                (version_id, item_id),
            ).fetchone()
            if not version:
                raise InvalidInputError("Version does not belong to this gallery work")
            connection.execute(
                """UPDATE gallery_items SET current_best_version_id=?,cover_preview_path=?,
                   preview_small_path=?,preview_large_path=?,updated_at=? WHERE id=?""",
                (
                    version_id, version["preview_watermarked_path"],
                    version["preview_watermarked_path"], version["preview_watermarked_path"],
                    _iso(self.clock()), item_id,
                ),
            )

    def set_item_favorite(self, user_id: str, item_id: str, favorite: bool) -> None:
        with self.database.transaction() as connection:
            self._require_item(connection, user_id, item_id)
            connection.execute(
                "UPDATE gallery_items SET favorite=?,updated_at=? WHERE id=?",
                (int(favorite), _iso(self.clock()), item_id),
            )

    def rename_item(self, user_id: str, item_id: str, title: str) -> None:
        clean = title.strip()
        if not clean or len(clean) > 200:
            raise InvalidInputError("Gallery title is empty or too long")
        with self.database.transaction() as connection:
            self._require_item(connection, user_id, item_id)
            connection.execute(
                "UPDATE gallery_items SET title=?,updated_at=? WHERE id=?",
                (clean, _iso(self.clock()), item_id),
            )

    def rate_version(self, user_id: str, version_id: str, rating: Optional[int]) -> None:
        if rating is not None and rating not in range(1, 6):
            raise InvalidInputError("Version rating must be between 1 and 5")
        with self.database.transaction() as connection:
            self._require_version(connection, user_id, version_id)
            connection.execute(
                "UPDATE gallery_versions SET rating=? WHERE id=?", (rating, version_id)
            )

    def create_collection(self, user_id: str, name: str) -> str:
        clean = name.strip()
        if not clean or len(clean) > 100:
            raise InvalidInputError("Collection name is empty or too long")
        collection_id = uuid4().hex
        now = _iso(self.clock())
        try:
            with self.database.transaction() as connection:
                self._require_user(connection, user_id)
                connection.execute(
                    "INSERT INTO collections(id,user_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (collection_id, user_id, clean, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise InvalidInputError("Collection name already exists") from exc
        return collection_id

    def move_to_collection(self, user_id: str, item_id: str, folder_id: Optional[str]) -> None:
        with self.database.transaction() as connection:
            self._require_item(connection, user_id, item_id)
            if folder_id:
                self._require_collection(connection, user_id, folder_id)
            connection.execute(
                "UPDATE gallery_items SET folder_id=?,updated_at=? WHERE id=?",
                (folder_id, _iso(self.clock()), item_id),
            )

    def list_collections(self, user_id: str) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT c.id,c.name,COUNT(g.id) AS item_count
                   FROM collections c LEFT JOIN gallery_items g
                     ON g.folder_id=c.id AND g.deleted=0
                   WHERE c.user_id=? AND c.deleted=0
                   GROUP BY c.id,c.name ORDER BY c.name""",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_tags(self, user_id: str, item_id: str, names: Sequence[str]) -> None:
        normalized = {name.strip().casefold(): name.strip() for name in names if name.strip()}
        if len(normalized) > 20 or any(len(value) > 60 for value in normalized.values()):
            raise InvalidInputError("Too many tags or tag is too long")
        with self.database.transaction() as connection:
            self._require_item(connection, user_id, item_id)
            connection.execute("DELETE FROM gallery_item_tags WHERE gallery_item_id=?", (item_id,))
            for key, display in sorted(normalized.items()):
                row = connection.execute(
                    "SELECT id FROM tags WHERE user_id=? AND normalized_name=?", (user_id, key)
                ).fetchone()
                tag_id = row["id"] if row else uuid4().hex
                if row is None:
                    connection.execute(
                        "INSERT INTO tags(id,user_id,name,normalized_name,created_at) VALUES(?,?,?,?,?)",
                        (tag_id, user_id, display, key, _iso(self.clock())),
                    )
                connection.execute(
                    "INSERT INTO gallery_item_tags(gallery_item_id,tag_id) VALUES(?,?)",
                    (item_id, tag_id),
                )

    def update_preferences(self, user_id: str, **values: Any) -> None:
        allowed = {
            "favorite_style", "favorite_background", "favorite_clothing",
            "favorite_format", "favorite_quality", "favorite_scenarios", "recent_scenarios",
        }
        if set(values) - allowed:
            raise InvalidInputError("Unknown preference field")
        with self.database.transaction() as connection:
            self._require_user(connection, user_id)
            current = connection.execute(
                "SELECT * FROM user_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
            data = dict(current) if current else {
                "favorite_style": None, "favorite_background": None, "favorite_clothing": None,
                "favorite_format": None, "favorite_quality": None,
                "favorite_scenarios_json": "[]", "recent_scenarios_json": "[]",
            }
            for key, value in values.items():
                column = f"{key}_json" if key in {"favorite_scenarios", "recent_scenarios"} else key
                data[column] = json.dumps(value, ensure_ascii=False) if column.endswith("_json") else value
            connection.execute(
                """INSERT INTO user_preferences(
                       user_id,favorite_style,favorite_background,favorite_clothing,
                       favorite_format,favorite_quality,favorite_scenarios_json,
                       recent_scenarios_json,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       favorite_style=excluded.favorite_style,
                       favorite_background=excluded.favorite_background,
                       favorite_clothing=excluded.favorite_clothing,
                       favorite_format=excluded.favorite_format,
                       favorite_quality=excluded.favorite_quality,
                       favorite_scenarios_json=excluded.favorite_scenarios_json,
                       recent_scenarios_json=excluded.recent_scenarios_json,
                       updated_at=excluded.updated_at""",
                (
                    user_id, data["favorite_style"], data["favorite_background"],
                    data["favorite_clothing"], data["favorite_format"], data["favorite_quality"],
                    data["favorite_scenarios_json"], data["recent_scenarios_json"],
                    _iso(self.clock()),
                ),
            )

    def get_preferences(self, user_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            self._require_user(connection, user_id)
            row = connection.execute(
                "SELECT * FROM user_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            return {
                "favorite_style": None,
                "favorite_background": None,
                "favorite_clothing": None,
                "favorite_format": None,
                "favorite_quality": None,
                "favorite_scenarios": [],
                "recent_scenarios": [],
            }
        result = dict(row)
        result["favorite_scenarios"] = json.loads(result.pop("favorite_scenarios_json"))
        result["recent_scenarios"] = json.loads(result.pop("recent_scenarios_json"))
        result.pop("user_id", None)
        result.pop("updated_at", None)
        return result

    def search(
        self,
        user_id: str,
        query: Optional[str] = None,
        scenario_id: Optional[str] = None,
        folder_id: Optional[str] = None,
        favorite: Optional[bool] = None,
        created_from: Optional[str] = None,
        created_to: Optional[str] = None,
        deleted: bool = False,
        limit: int = 50,
    ) -> list[GalleryItem]:
        if limit < 1 or limit > 100:
            raise InvalidInputError("Gallery search limit must be between 1 and 100")
        clauses = ["g.user_id=?", "g.deleted=?"]
        parameters: list[Any] = [user_id, int(deleted)]
        if query:
            clauses.append("(g.title LIKE ? OR EXISTS(SELECT 1 FROM gallery_item_tags git JOIN tags t ON t.id=git.tag_id WHERE git.gallery_item_id=g.id AND t.name LIKE ?))")
            parameters.extend([f"%{query.strip()}%", f"%{query.strip()}%"])
        for column, value in (("scenario_id", scenario_id), ("folder_id", folder_id)):
            if value is not None:
                clauses.append(f"g.{column}=?")
                parameters.append(value)
        if favorite is not None:
            if favorite:
                clauses.append("(g.favorite=1 OR EXISTS(SELECT 1 FROM gallery_versions v WHERE v.gallery_item_id=g.id AND v.favorite=1))")
            else:
                clauses.append("(g.favorite=0 AND NOT EXISTS(SELECT 1 FROM gallery_versions v WHERE v.gallery_item_id=g.id AND v.favorite=1))")
        if created_from:
            clauses.append("g.created_at>=?")
            parameters.append(created_from)
        if created_to:
            clauses.append("g.created_at<=?")
            parameters.append(created_to)
        parameters.append(limit)
        with self.database.read() as connection:
            rows = connection.execute(
                f"SELECT DISTINCT g.* FROM gallery_items g WHERE {' AND '.join(clauses)} ORDER BY g.updated_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._item(row) for row in rows]

    def open_item(self, user_id: str, item_id: str) -> tuple[GalleryItem, Optional[GalleryVersion]]:
        now = _iso(self.clock())
        with self.database.transaction() as connection:
            self._require_item(connection, user_id, item_id)
            connection.execute(
                "UPDATE gallery_items SET last_opened_at=? WHERE id=?", (now, item_id)
            )
            item = connection.execute("SELECT * FROM gallery_items WHERE id=?", (item_id,)).fetchone()
            version = None
            if item["current_best_version_id"]:
                version = connection.execute(
                    "SELECT * FROM gallery_versions WHERE id=?", (item["current_best_version_id"],)
                ).fetchone()
        return self._item(item), self._version(version) if version else None

    def continue_work(self, user_id: str) -> Optional[tuple[GalleryItem, Optional[GalleryVersion]]]:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT id FROM gallery_items WHERE user_id=? AND deleted=0
                   ORDER BY COALESCE(last_opened_at,updated_at) DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return self.open_item(user_id, row["id"]) if row else None

    def soft_delete(self, user_id: str, item_id: str) -> None:
        now = self.clock()
        with self.database.transaction() as connection:
            self._require_item(connection, user_id, item_id)
            connection.execute(
                """UPDATE gallery_items SET deleted=1,deleted_at=?,purge_after=?,updated_at=?
                   WHERE id=?""",
                (
                    _iso(now), _iso(now + timedelta(days=self.settings.trash_retention_days)),
                    _iso(now), item_id,
                ),
            )

    def restore(self, user_id: str, item_id: str) -> None:
        with self.database.transaction() as connection:
            self._require_item(connection, user_id, item_id, include_deleted=True)
            connection.execute(
                """UPDATE gallery_items SET deleted=0,deleted_at=NULL,purge_after=NULL,updated_at=?
                   WHERE id=?""",
                (_iso(self.clock()), item_id),
            )

    def due_for_cleanup(self, now: Optional[datetime] = None) -> list[str]:
        moment = _iso(now or self.clock())
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT id FROM gallery_items WHERE purged_at IS NULL AND
                   ((deleted=1 AND purge_after<=?) OR retention_until<=?)""",
                (moment, moment),
            ).fetchall()
        return [row["id"] for row in rows]

    def purge_item(self, item_id: str) -> None:
        if (
            self.provider_context_service is not None
            and self.settings.openai_context_delete_on_gallery_delete
        ):
            # Remote cleanup is best-effort and leaves a retryable tombstone.
            # User data purge must never be blocked by a provider outage.
            self.provider_context_service.delete_for_gallery(item_id)
        with self.database.transaction() as connection:
            item = connection.execute("SELECT * FROM gallery_items WHERE id=?", (item_id,)).fetchone()
            if not item:
                return
            attempts = connection.execute(
                "SELECT attempt_id FROM gallery_versions WHERE gallery_item_id=? AND attempt_id IS NOT NULL",
                (item_id,),
            ).fetchall()
            self.storage.delete_private_tree(Path(item["storage_root_path"]))
            for attempt in attempts:
                connection.execute(
                    """UPDATE generation_attempts SET source_path='',secondary_source_path=NULL,
                       original_result_path=NULL,
                       demo_result_path=NULL WHERE id=?""",
                    (attempt["attempt_id"],),
                )
            connection.execute(
                """UPDATE demo_sessions SET source_file_path='',secondary_source_file_path=NULL,
                   status='deleted'
                   WHERE gallery_item_id=?""",
                (item_id,),
            )
            connection.execute("DELETE FROM gallery_items WHERE id=?", (item_id,))
            connection.execute(
                """DELETE FROM tags WHERE user_id=? AND NOT EXISTS(
                       SELECT 1 FROM gallery_item_tags git WHERE git.tag_id=tags.id
                   )""",
                (item["user_id"],),
            )

    def purge_due(self, execute: bool = False) -> list[str]:
        due = self.due_for_cleanup()
        if execute:
            for item_id in due:
                self.purge_item(item_id)
        return due

    @staticmethod
    def _require_user(connection: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise InvalidInputError("Unknown user")
        return row

    @staticmethod
    def _ensure_gallery(
        connection: sqlite3.Connection, user_id: str, now: datetime
    ) -> str:
        row = connection.execute(
            "SELECT id FROM galleries WHERE user_id=?", (user_id,)
        ).fetchone()
        if row:
            return row["id"]
        gallery_id = uuid4().hex
        connection.execute(
            "INSERT INTO galleries(id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
            (gallery_id, user_id, _iso(now), _iso(now)),
        )
        return gallery_id

    @staticmethod
    def _require_collection(connection: sqlite3.Connection, user_id: str, collection_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM collections WHERE id=? AND user_id=? AND deleted=0",
            (collection_id, user_id),
        ).fetchone()
        if not row:
            raise InvalidInputError("Unknown collection")
        return row

    @staticmethod
    def _require_item(
        connection: sqlite3.Connection, user_id: str, item_id: str, include_deleted: bool = False
    ) -> sqlite3.Row:
        sql = "SELECT * FROM gallery_items WHERE id=? AND user_id=?"
        parameters: list[Any] = [item_id, user_id]
        if not include_deleted:
            sql += " AND deleted=0"
        row = connection.execute(sql, parameters).fetchone()
        if not row:
            raise InvalidInputError("Unknown gallery work")
        return row

    @staticmethod
    def _require_version(connection: sqlite3.Connection, user_id: str, version_id: str) -> sqlite3.Row:
        row = connection.execute(
            """SELECT v.* FROM gallery_versions v JOIN gallery_items g ON g.id=v.gallery_item_id
               WHERE v.id=? AND g.user_id=?""",
            (version_id, user_id),
        ).fetchone()
        if not row:
            raise InvalidInputError("Unknown gallery version")
        return row
