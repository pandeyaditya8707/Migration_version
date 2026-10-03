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
        from app.main import target_store, dynamic_store, plan_manager, executor
        target_store.db_path = test_db_file
        target_store.init_database()

        dynamic_store.db_path = test_db_file
        dynamic_store._init_metadata_tables()

        plan_manager.store = target_store
        plan_manager._plans.clear()
        plan_manager._load_from_db()

        executor.store = target_store
    except Exception as e:
        raise RuntimeError(f"Failed to initialize hermetic test database: {e}") from e

    yield test_db_file
