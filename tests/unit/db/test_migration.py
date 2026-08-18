from __future__ import annotations

import os
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_control_plane_revision_follows_immutable_baseline() -> None:
    scripts = ScriptDirectory.from_config(Config("db/alembic.ini"))
    head = scripts.get_current_head()
    revision = scripts.get_revision(head)

    assert head == "20260818_0003"
    assert revision is not None
    assert revision.down_revision == "20260818_0002"


def test_migration_upgrade_and_downgrade_compile_for_postgresql() -> None:
    environment = {**os.environ, "PULSE_DATABASE_URL": "postgresql+psycopg://unused/db"}
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "db/alembic.ini", "upgrade", "head", "--sql"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    downgrade = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "db/alembic.ini",
            "downgrade",
            "20260818_0003:base",
            "--sql",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "CREATE TABLE demo_runs" in upgrade.stdout
    assert "CREATE TABLE response_retries" in upgrade.stdout
    assert "ALTER TABLE traffic_snapshots ADD COLUMN demo_run_id UUID" in upgrade.stdout
    assert "DROP TABLE demo_runs" in downgrade.stdout
    assert "DROP TABLE response_retries" in downgrade.stdout
