"""Run only main-bot tests that are valid inside a production artifact."""

from __future__ import annotations

import sys
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[1]

# Production keeps Content Studio as a separately deployed runtime. Repository
# policy/lineage and developer-efficiency tests also belong in canonical CI,
# where Git metadata is available, rather than in the extracted main-bot tree.
MAIN_BOT_PRODUCTION_TEST_MODULES: tuple[str, ...] = (
    "tests.test_ai_brain_integration",
    "tests.test_backup_maintenance_operations",
    "tests.test_commerce_cli",
    "tests.test_commerce",
    "tests.test_config",
    "tests.test_demo_cli",
    "tests.test_demo_service",
    "tests.test_direct_prompt",
    "tests.test_edit_intent",
    "tests.test_gallery",
    "tests.test_gemini_image_provider",
    "tests.test_health",
    "tests.test_image_provider",
    "tests.test_image_service",
    "tests.test_main_ops",
    "tests.test_max_adapter",
    "tests.test_max_application",
    "tests.test_max_ca",
    "tests.test_max_conversation",
    "tests.test_max_runtime",
    "tests.test_max_transport",
    "tests.test_max_ui_shell",
    "tests.test_migration_safety",
    "tests.test_openai_client",
    "tests.test_owner_e2e_audit",
    "tests.test_payment_admin",
    "tests.test_payments",
    "tests.test_processing_modes",
    "tests.test_provider_context_integration",
    "tests.test_provider_context",
    "tests.test_provider_router",
    "tests.test_readiness_controls",
    "tests.test_referrals",
    "tests.test_responses_image_provider",
    "tests.test_robokassa_sandbox_e2e",
    "tests.test_robokassa_signature_bisect",
    "tests.test_watchdog",
    "tests.test_work_gallery",
)


def run_preflight(
    modules: Iterable[str] = MAIN_BOT_PRODUCTION_TEST_MODULES,
    *,
    artifact_root: Path = ROOT,
    stream: TextIO | None = None,
) -> unittest.result.TestResult:
    """Run the explicit main-bot suite without consulting repository metadata."""

    module_names = tuple(modules)
    if not module_names:
        raise ValueError("main-bot production preflight requires test modules")

    root = str(artifact_root.resolve())
    inserted = root not in sys.path
    if inserted:
        sys.path.insert(0, root)
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(module_names)
        runner = unittest.TextTestRunner(stream=stream, verbosity=1)
        return runner.run(suite)
    finally:
        if inserted:
            sys.path.remove(root)


def main() -> int:
    result = run_preflight()
    print(
        "MAIN_BOT_PRODUCTION_PREFLIGHT="
        + ("PASS" if result.wasSuccessful() else "FAIL")
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
