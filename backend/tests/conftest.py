from __future__ import annotations

import os
import tempfile

import pytest

# Establish hermetic database default prior to module loads
_temp_db_dir = tempfile.TemporaryDirectory()
_default_test_db = os.path.join(_temp_db_dir.name, "default_test.db")
if "DATABASE_PATH" not in os.environ:
    os.environ["DATABASE_PATH"] = _default_test_db


@pytest.fixture(autouse=True)
def isolate_database_per_test(tmp_path, monkeypatch):
    """Hermetic SQLite database fixture that provides an isolated, pristine database for every test.
    Ensures zero cross-test interference, avoids file locks, and prevents data pollution."""
    test_db_file = str(tmp_path / "isolated_test.db")
    monkeypatch.setenv("DATABASE_PATH", test_db_file)

    try:
        import app.main as main_mod

        main_mod.target_store.db_path = test_db_file
        main_mod.target_store.init_database()

        main_mod.dynamic_store.db_path = test_db_file
        main_mod.dynamic_store._init_metadata_tables()

        main_mod.plan_manager.store = main_mod.target_store
        main_mod.plan_manager._plans.clear()
        main_mod.plan_manager._load_from_db()

        main_mod.executor.store = main_mod.target_store

        # In-place reset Mode 1 inspection tools with pristine 1,000 records
        from app.engine.profiler import (
            load_sample_records,
            load_source_schema,
            load_target_schema,
        )

        main_mod.inspection_tools.records = load_sample_records()
        main_mod.inspection_tools.source_schema = load_source_schema()
        main_mod.inspection_tools.target_schema = load_target_schema()
        main_mod.agent.tools = main_mod.inspection_tools

        # In-place reset Mode 2 inspection tools and state
        fresh_v2 = main_mod.init_mode2_inspection_tools()
        main_mod.v2_inspection_tools.records = fresh_v2.records
        main_mod.v2_inspection_tools.source_schema = fresh_v2.source_schema
        main_mod.v2_inspection_tools.target_schema = fresh_v2.target_schema
        main_mod.ollama_agent.tools = main_mod.v2_inspection_tools

        main_mod.v2_state.clear()
        main_mod.v2_state.update(
            {
                "active_plan": None,
                "active_target_schema": None,
                "last_dry_run_summary": None,
                "last_execution_result": None,
                "last_quarantine_records": [],
            }
        )
    except Exception as e:
        raise RuntimeError(f"Failed to initialize hermetic test database and state: {e}") from e

    yield test_db_file
