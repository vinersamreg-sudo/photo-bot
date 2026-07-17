import json
import tempfile
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.config import Settings
from app.database import Database
from scripts.export_owner_e2e_audit import build_report


class OwnerE2EAuditTests(TestCase):
    def test_report_preserves_evidence_without_identifiers_or_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "data" / "users" / "private-owner" / "source.png"
            source.parent.mkdir(parents=True)
            Image.new("RGB", (16, 12), "green").save(source)
            settings = Settings(
                openai_api_key="secret-openai-key",
                openai_image_model="gpt-image-2",
                app_env="ci",
                base_dir=base,
                max_poll_observe_only=True,
                max_owner_user_ids=("private-platform-owner-id",),
            )
            database = Database(settings.database_path)
            parent_plan = {
                "mode": "correction",
                "primary_action": "change_clothes",
                "scene": {"outfit": {"operation": "replace", "style": "hiking"}},
            }
            plan = {
                "mode": "correction",
                "primary_action": "change_clothes",
                "inherited_constraints": ["Keep the rocky mountains."],
                "scene": {
                    "outfit": {
                        "operation": "replace",
                        "style": "hiking",
                        "color": "dark green",
                    }
                },
            }
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO users(id,platform,platform_user_id,created_at) VALUES('u','max',?,?)",
                    (settings.max_owner_user_ids[0], "2026-01-01T00:00:00+00:00"),
                )
                connection.execute(
                    """INSERT INTO demo_sessions(
                           id,user_id,source_file_path,source_sha256,status,started_at,expires_at,
                           successful_generations,max_generations,completed_at,converted_to_paid,
                           created_at,updated_at,gallery_item_id
                       ) VALUES('s','u',?,'sha','completed',?,?,1,1,?,0,?,?, 'i')""",
                    (str(source),) + ("2026-01-01T00:00:00+00:00",) * 5,
                )
                connection.execute(
                    "INSERT INTO galleries(id,user_id,created_at,updated_at) VALUES('g','u',?,?)",
                    ("2026-01-01T00:00:00+00:00",) * 2,
                )
                connection.execute(
                    """INSERT INTO gallery_items(
                           id,gallery_id,user_id,title,created_at,updated_at,original_source_path,
                           storage_root_path,current_best_version_id,favorite,retention_until
                       ) VALUES('i','g','u','work',?,?,?,?, 'v2',1,?)""",
                    (
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                        str(source),
                        str(source.parent),
                        "2027-01-01T00:00:00+00:00",
                    ),
                )
                connection.execute(
                    """INSERT INTO gallery_versions(
                           id,gallery_item_id,version_number,source_path,prompt,effective_prompt,
                           provider,model,created_at,status,edit_plan_json
                       ) VALUES('v1','i',1,?,'old','old','openai','gpt-image-2',?,'succeeded',?)""",
                    (str(source), "2026-01-01T00:00:00+00:00", json.dumps(parent_plan)),
                )
                connection.execute(
                    """INSERT INTO generation_attempts(
                           id,idempotency_key,session_id,user_id,prompt,status,started_at,completed_at,
                           provider,model,source_path,original_result_path,estimated_cost,
                           external_request_id,duration_ms,input_size_bytes,output_size_bytes,
                           correction,edit_plan_json,provider_prompt,source_version_id,
                           parent_version_id,prompt_builder_version,created_at
                       ) VALUES('a','k','s','u',?,'succeeded',?,?,'openai','gpt-image-2',?,?,10,
                                'req-safe',90000,1,2,1,?,'technical prompt','v1','v1','technical-en-v2',?)""",
                    (
                        "Поменяй только цвет куртки на тёмно-зелёный",
                        "2026-01-01T00:01:00+00:00",
                        "2026-01-01T00:02:30+00:00",
                        str(source),
                        str(source),
                        json.dumps(plan, ensure_ascii=False),
                        "2026-01-01T00:01:00+00:00",
                    ),
                )
                connection.execute(
                    """INSERT INTO gallery_versions(
                           id,gallery_item_id,attempt_id,version_number,parent_version_id,source_path,
                           prompt,effective_prompt,provider,model,original_path,created_at,status,
                           favorite,edit_plan_json,source_version_id
                       ) VALUES('v2','i','a',2,'v1',?,?,?,'openai','gpt-image-2',?,?,'succeeded',1,?,'v1')""",
                    (
                        str(source),
                        "new",
                        "new",
                        str(source),
                        "2026-01-01T00:02:30+00:00",
                        json.dumps(plan, ensure_ascii=False),
                    ),
                )
                connection.execute(
                    """INSERT INTO max_dialogs(
                           platform_user_id,user_id,state,created_at,updated_at
                       ) VALUES(?, 'u','main_menu',?,?)""",
                    (
                        settings.max_owner_user_ids[0],
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

            report = build_report(settings, limit=1)
            rendered = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(settings.max_owner_user_ids[0], rendered)
            self.assertNotIn(str(base), rendered)
            self.assertNotIn(settings.openai_api_key, rendered)
            self.assertEqual(report["request_count"], 1)
            self.assertEqual(
                report["attempts"][0]["changed_fields"]["outfit.color"], "dark green"
            )
            self.assertEqual(report["attempts"][0]["parent_version_number"], 1)
            self.assertEqual(report["attempts"][0]["gallery_version_number"], 2)
            self.assertEqual(report["final_checks"]["sqlite_quick_check"], "ok")
            self.assertEqual(report["final_checks"]["orphan_private_file_count"], 0)
