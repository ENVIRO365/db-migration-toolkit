"""Typed metadata models for database migration operations.

All models are plain dataclasses — lightweight, serialisable, and free of
ORM or validation-framework coupling.  Enums use lowercase string values so
they round-trip cleanly through YAML / JSON manifests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class IdentityStrategy(Enum):
    """How a target identity column should be handled during writes."""

    BY_DEFAULT = "by_default"
    ALWAYS = "always"
    NONE = "none"


class MigrationOperation(Enum):
    """Row-level operation the migration engine will perform."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    NO_ACTION = "no_action"


class ComparisonStrategy(Enum):
    """Strategy used to detect deltas between source and target."""

    ROW_COUNT = "row_count"
    PRIMARY_KEY = "primary_key"
    CHECKSUM = "checksum"
    TIMESTAMP = "timestamp"
    AUTO = "auto"


class MigrationMode(Enum):
    """Direction of the data flow."""

    SYNC = "sync"
    ROLLBACK = "rollback"


class AutomationMode(Enum):
    """How much human confirmation the engine requires."""

    SUPERVISED = "supervised"
    AUTO_NON_PROD = "auto_non_prod"
    AUTO_APPROVED = "auto_approved"


class BatchStatus(Enum):
    """Lifecycle status of a single migration batch."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Schema metadata
# ---------------------------------------------------------------------------

@dataclass
class ColumnMetadata:
    """Metadata describing a single database column."""

    name: str
    data_type: str
    max_length: Optional[int] = None
    numeric_precision: Optional[int] = None
    numeric_scale: Optional[int] = None
    is_nullable: bool = True
    is_identity: bool = False
    identity_generation: Optional[IdentityStrategy] = None
    default_value: Optional[str] = None
    ordinal_position: int = 0


@dataclass
class PrimaryKeyMetadata:
    """Metadata describing a table's primary key constraint."""

    columns: list[str]
    constraint_name: Optional[str] = None
    is_composite: bool = False  # derived in __post_init__

    def __post_init__(self) -> None:
        self.is_composite = len(self.columns) > 1


@dataclass
class ForeignKeyMetadata:
    """Metadata describing a foreign key relationship."""

    constraint_name: str
    columns: list[str]
    referenced_table: str
    referenced_columns: list[str]
    referenced_schema: Optional[str] = None


@dataclass
class SequenceMetadata:
    """Metadata describing a database sequence."""

    name: str
    last_value: Optional[int] = None
    start_value: int = 1
    increment: int = 1
    cache_size: int = 1
    is_identity_sequence: bool = False
    associated_table: Optional[str] = None
    associated_column: Optional[str] = None


@dataclass
class TriggerMetadata:
    """Metadata describing a database trigger."""

    name: str
    table_name: str
    event: str  # INSERT, UPDATE, DELETE
    timing: str  # BEFORE, AFTER
    body: Optional[str] = None
    referenced_tables: list[str] = field(default_factory=list)


@dataclass
class TableMetadata:
    """Complete metadata for a single database table."""

    name: str
    schema: str
    columns: list[ColumnMetadata] = field(default_factory=list)
    primary_key: Optional[PrimaryKeyMetadata] = None
    foreign_keys: list[ForeignKeyMetadata] = field(default_factory=list)
    sequences: list[SequenceMetadata] = field(default_factory=list)
    triggers: list[TriggerMetadata] = field(default_factory=list)
    row_count: int = 0
    identity_column: Optional[ColumnMetadata] = None
    max_pk_value: Optional[int] = None


@dataclass
class DatabaseMetadata:
    """Aggregated metadata for an entire database schema."""

    engine: str
    schema: str
    tables: dict[str, TableMetadata] = field(default_factory=dict)
    standalone_sequences: list[SequenceMetadata] = field(default_factory=list)
    encoding: Optional[str] = None
    version: Optional[str] = None


# ---------------------------------------------------------------------------
# Migration planning
# ---------------------------------------------------------------------------

@dataclass
class ColumnMapping:
    """Mapping between a source and target column."""

    source_column: str
    target_column: str
    source_type: str
    target_type: str
    requires_cast: bool = False
    cast_expression: Optional[str] = None
    source_only: bool = False
    target_only: bool = False


@dataclass
class TableDelta:
    """Result of comparing a single table between source and target."""

    table_name: str
    insert_pks: list[Any] = field(default_factory=list)
    update_pks: list[Any] = field(default_factory=list)
    delete_pks: list[Any] = field(default_factory=list)
    unchanged_count: int = 0
    source_count: int = 0
    target_count: int = 0


@dataclass
class MigrationTablePlan:
    """Execution plan for migrating a single table."""

    table_name: str
    operation: MigrationOperation
    row_count: int = 0
    identity_strategy: IdentityStrategy = IdentityStrategy.NONE
    dependency_level: int = 0
    column_mappings: list[ColumnMapping] = field(default_factory=list)
    source_only_columns: list[str] = field(default_factory=list)
    target_only_columns: list[str] = field(default_factory=list)
    requires_bulk_load: bool = False
    delta: Optional[TableDelta] = None


@dataclass
class MigrationBatch:
    """A single batch within a table migration."""

    batch_id: str
    table_name: str
    operation: MigrationOperation
    start_pk: Optional[int] = None
    end_pk: Optional[int] = None
    row_count: int = 0
    status: BatchStatus = BatchStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    checksum: Optional[str] = None


@dataclass
class MigrationManifest:
    """Top-level manifest describing an entire migration run."""

    migration_id: str
    profile_name: str
    mode: MigrationMode
    tables: list[MigrationTablePlan] = field(default_factory=list)
    batches: list[MigrationBatch] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    total_rows: int = 0
    insert_tables: int = 0
    update_tables: int = 0
    delete_tables: int = 0
    no_action_tables: int = 0


# ---------------------------------------------------------------------------
# Results & validation
# ---------------------------------------------------------------------------

@dataclass
class MigrationResult:
    """Outcome of migrating a single table."""

    migration_id: str
    table_name: str
    operation: MigrationOperation
    rows_attempted: int = 0
    rows_succeeded: int = 0
    rows_failed: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None


@dataclass
class ValidationResult:
    """Outcome of a single validation check."""

    table_name: str
    check_name: str
    passed: bool
    expected: Optional[str] = None
    actual: Optional[str] = None
    message: Optional[str] = None
    severity: str = "error"  # error, warning, info
