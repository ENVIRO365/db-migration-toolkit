"""Profile configuration loader and validator."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from dbmigrate.models import AutomationMode, ComparisonStrategy, MigrationMode


class DatabaseConfig(BaseModel):
    """Connection details for a single database endpoint."""

    type: str  # postgresql, db2, mysql, etc.
    schema_name: str = Field(alias="schema")
    dsn_env: str  # env var name containing DSN

    model_config = {"populate_by_name": True}


class MigrationConfig(BaseModel):
    """Tuning knobs for the migration engine."""

    mode: MigrationMode = MigrationMode.SYNC
    batch_size: int = 5000
    fetch_size: int = 5000
    workers: int = 4
    commit_every: int = 5000


class ComparisonConfig(BaseModel):
    """Settings for the source/target comparison phase."""

    strategy: ComparisonStrategy = ComparisonStrategy.AUTO


class PerformanceConfig(BaseModel):
    """Performance-related toggles."""

    streaming: bool = True
    parallel_tables: bool = True


class AutomationConfig(BaseModel):
    """Controls for human-in-the-loop confirmation."""

    mode: AutomationMode = AutomationMode.SUPERVISED
    auto_confirm_below_rows: int = 0


class KnownQuirk(BaseModel):
    """Documents a known data anomaly the operator has accepted."""

    table: str
    note: str


class ProfileConfig(BaseModel):
    """Top-level configuration for a migration profile."""

    name: str
    source: DatabaseConfig
    target: DatabaseConfig
    migration: MigrationConfig = MigrationConfig()
    comparison: ComparisonConfig = ComparisonConfig()
    performance: PerformanceConfig = PerformanceConfig()
    automation: AutomationConfig = AutomationConfig()
    known_quirks: list[KnownQuirk] = []

    # Tables to skip entirely
    skip_tables: list[str] = []
    # Tables where DELETE is explicitly allowed in rollback mode
    delete_allowed_tables: list[str] = []


def load_profile(
    profile_name: str,
    profiles_dir: Optional[Path] = None,
) -> ProfileConfig:
    """Load a named profile from the profiles directory.

    Parameters
    ----------
    profile_name:
        Directory name under *profiles_dir* containing ``profile.yaml``.
    profiles_dir:
        Override for the default ``<project_root>/profiles`` directory.

    Raises
    ------
    FileNotFoundError
        If the resolved ``profile.yaml`` does not exist.
    """
    if profiles_dir is None:
        profiles_dir = Path(__file__).parent.parent.parent / "profiles"

    profile_path = profiles_dir / profile_name / "profile.yaml"
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile not found: {profile_path}")

    with open(profile_path) as f:
        data = yaml.safe_load(f)

    return ProfileConfig(**data)


def list_profiles(profiles_dir: Optional[Path] = None) -> list[str]:
    """Return sorted list of available profile names.

    A directory qualifies as a profile when it contains a ``profile.yaml``
    file and is not named ``_template``.
    """
    if profiles_dir is None:
        profiles_dir = Path(__file__).parent.parent.parent / "profiles"

    if not profiles_dir.exists():
        return []

    return sorted(
        d.name
        for d in profiles_dir.iterdir()
        if d.is_dir() and (d / "profile.yaml").exists() and d.name != "_template"
    )
