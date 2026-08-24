"""Shared test fixtures for the db-migration-toolkit test suite."""

from __future__ import annotations

import pytest

from dbmigrate.checkpoint import CheckpointStore
from dbmigrate.config import (
    AutomationConfig,
    ComparisonConfig,
    DatabaseConfig,
    MigrationConfig,
    PerformanceConfig,
    ProfileConfig,
)
from dbmigrate.models import (
    AutomationMode,
    ColumnMapping,
    ColumnMetadata,
    DatabaseMetadata,
    ForeignKeyMetadata,
    IdentityStrategy,
    MigrationMode,
    PrimaryKeyMetadata,
    TableMetadata,
)


@pytest.fixture
def sample_source_metadata() -> DatabaseMetadata:
    """DatabaseMetadata representing a source (DB2-like) schema."""
    return DatabaseMetadata(
        engine="db2",
        schema="SOURCE_SCHEMA",
        tables={
            "parent_table": TableMetadata(
                name="parent_table",
                schema="SOURCE_SCHEMA",
                columns=[
                    ColumnMetadata(name="id", data_type="INTEGER", is_nullable=False, is_identity=True,
                                   identity_generation=IdentityStrategy.ALWAYS, ordinal_position=1),
                    ColumnMetadata(name="name", data_type="VARCHAR", max_length=100, ordinal_position=2),
                    ColumnMetadata(name="status", data_type="SMALLINT", ordinal_position=3),
                ],
                primary_key=PrimaryKeyMetadata(columns=["id"], constraint_name="pk_parent"),
                foreign_keys=[],
                row_count=500,
                identity_column=ColumnMetadata(
                    name="id", data_type="INTEGER", is_identity=True,
                    identity_generation=IdentityStrategy.ALWAYS,
                ),
            ),
            "child_table": TableMetadata(
                name="child_table",
                schema="SOURCE_SCHEMA",
                columns=[
                    ColumnMetadata(name="id", data_type="INTEGER", is_nullable=False, is_identity=True,
                                   identity_generation=IdentityStrategy.BY_DEFAULT, ordinal_position=1),
                    ColumnMetadata(name="parent_id", data_type="INTEGER", is_nullable=False, ordinal_position=2),
                    ColumnMetadata(name="value", data_type="VARCHAR", max_length=255, ordinal_position=3),
                ],
                primary_key=PrimaryKeyMetadata(columns=["id"], constraint_name="pk_child"),
                foreign_keys=[
                    ForeignKeyMetadata(
                        constraint_name="fk_child_parent",
                        columns=["parent_id"],
                        referenced_table="parent_table",
                        referenced_columns=["id"],
                    ),
                ],
                row_count=2000,
                identity_column=ColumnMetadata(
                    name="id", data_type="INTEGER", is_identity=True,
                    identity_generation=IdentityStrategy.BY_DEFAULT,
                ),
            ),
            "standalone_table": TableMetadata(
                name="standalone_table",
                schema="SOURCE_SCHEMA",
                columns=[
                    ColumnMetadata(name="id", data_type="BIGINT", is_nullable=False, ordinal_position=1),
                    ColumnMetadata(name="description", data_type="CLOB", ordinal_position=2),
                ],
                primary_key=PrimaryKeyMetadata(columns=["id"], constraint_name="pk_standalone"),
                foreign_keys=[],
                row_count=100,
            ),
        },
    )


@pytest.fixture
def sample_target_metadata() -> DatabaseMetadata:
    """DatabaseMetadata representing a target (PostgreSQL-like) schema."""
    return DatabaseMetadata(
        engine="postgresql",
        schema="public",
        tables={
            "parent_table": TableMetadata(
                name="parent_table",
                schema="public",
                columns=[
                    ColumnMetadata(name="id", data_type="integer", is_nullable=False, is_identity=True,
                                   identity_generation=IdentityStrategy.ALWAYS, ordinal_position=1),
                    ColumnMetadata(name="name", data_type="character varying", max_length=100, ordinal_position=2),
                    ColumnMetadata(name="status", data_type="smallint", ordinal_position=3),
                ],
                primary_key=PrimaryKeyMetadata(columns=["id"], constraint_name="pk_parent"),
                foreign_keys=[],
                row_count=500,
                identity_column=ColumnMetadata(
                    name="id", data_type="integer", is_identity=True,
                    identity_generation=IdentityStrategy.ALWAYS,
                ),
            ),
            "child_table": TableMetadata(
                name="child_table",
                schema="public",
                columns=[
                    ColumnMetadata(name="id", data_type="integer", is_nullable=False, is_identity=True,
                                   identity_generation=IdentityStrategy.BY_DEFAULT, ordinal_position=1),
                    ColumnMetadata(name="parent_id", data_type="integer", is_nullable=False, ordinal_position=2),
                    ColumnMetadata(name="value", data_type="character varying", max_length=255, ordinal_position=3),
                ],
                primary_key=PrimaryKeyMetadata(columns=["id"], constraint_name="pk_child"),
                foreign_keys=[
                    ForeignKeyMetadata(
                        constraint_name="fk_child_parent",
                        columns=["parent_id"],
                        referenced_table="parent_table",
                        referenced_columns=["id"],
                    ),
                ],
                row_count=1800,
                identity_column=ColumnMetadata(
                    name="id", data_type="integer", is_identity=True,
                    identity_generation=IdentityStrategy.BY_DEFAULT,
                ),
            ),
            "standalone_table": TableMetadata(
                name="standalone_table",
                schema="public",
                columns=[
                    ColumnMetadata(name="id", data_type="bigint", is_nullable=False, ordinal_position=1),
                    ColumnMetadata(name="description", data_type="text", ordinal_position=2),
                ],
                primary_key=PrimaryKeyMetadata(columns=["id"], constraint_name="pk_standalone"),
                foreign_keys=[],
                row_count=100,
            ),
        },
    )


@pytest.fixture
def sample_profile_config() -> ProfileConfig:
    """A sample ProfileConfig for testing."""
    return ProfileConfig(
        name="test-profile",
        source=DatabaseConfig(type="db2", schema_name="SOURCE_SCHEMA", dsn_env="SOURCE_DSN"),
        target=DatabaseConfig(type="postgresql", schema_name="public", dsn_env="TARGET_DSN"),
        migration=MigrationConfig(mode=MigrationMode.SYNC, batch_size=1000),
        comparison=ComparisonConfig(),
        performance=PerformanceConfig(),
        automation=AutomationConfig(mode=AutomationMode.SUPERVISED),
    )


@pytest.fixture
def tmp_checkpoint_store(tmp_path) -> CheckpointStore:
    """A CheckpointStore backed by a temporary directory."""
    store = CheckpointStore(base_dir=str(tmp_path / "checkpoints"))
    return store
