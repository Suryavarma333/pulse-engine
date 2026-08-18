from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from db import models  # noqa: F401
from db.base import Base

EXPECTED_TABLES = {
    "traffic_snapshots",
    "scheduled_events",
    "surge_predictions",
    "surge_prediction_points",
    "scaling_actions",
    "load_shedding_events",
}


def test_all_audit_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_schema_compiles_for_postgresql() -> None:
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))
