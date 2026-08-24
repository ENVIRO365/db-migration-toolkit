"""Tests for dbmigrate.config — profile loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dbmigrate.config import (
    AutomationConfig,
    ComparisonConfig,
    DatabaseConfig,
    MigrationConfig,
    PerformanceConfig,
    ProfileConfig,
    load_profile,
    list_profiles,
)
from dbmigrate.models import AutomationMode, ComparisonStrategy, MigrationMode


def _write_profile(profiles_dir: Path, name: str, data: dict) -> Path:
    """Helper: write a profile.yaml under profiles_dir/name/."""
    profile_dir = profiles_dir / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / "profile.yaml"
    profile_path.write_text(yaml.dump(data))
    return profile_path


@pytest.fixture
def valid_profile_data() -> dict:
    return {
        "name": "wealth-adapter",
        "source": {
            "type": "db2",
            "schema": "WEALTHADAPTER",
            "dsn_env": "DB2_DSN",
        },
        "target": {
            "type": "postgresql",
            "schema": "public",
            "dsn_env": "PG_DSN",
        },
        "migration": {
            "mode": "sync",
            "batch_size": 2000,
            "fetch_size": 2000,
            "workers": 2,
            "commit_every": 2000,
        },
        "automation": {
            "mode": "supervised",
        },
    }


class TestLoadProfile:
    def test_load_valid_profile(self, tmp_path, valid_profile_data):
        _write_profile(tmp_path, "wealth-adapter", valid_profile_data)
        cfg = load_profile("wealth-adapter", profiles_dir=tmp_path)
        assert isinstance(cfg, ProfileConfig)
        assert cfg.name == "wealth-adapter"
        assert cfg.source.type == "db2"
        assert cfg.target.schema_name == "public"
        assert cfg.migration.batch_size == 2000
        assert cfg.migration.mode == MigrationMode.SYNC

    def test_load_missing_profile_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Profile not found"):
            load_profile("nonexistent", profiles_dir=tmp_path)

    def test_load_profile_with_defaults(self, tmp_path):
        minimal = {
            "name": "minimal",
            "source": {"type": "db2", "schema": "S", "dsn_env": "X"},
            "target": {"type": "postgresql", "schema": "public", "dsn_env": "Y"},
        }
        _write_profile(tmp_path, "minimal", minimal)
        cfg = load_profile("minimal", profiles_dir=tmp_path)
        # Defaults should apply
        assert cfg.migration.batch_size == 5000
        assert cfg.migration.mode == MigrationMode.SYNC
        assert cfg.automation.mode == AutomationMode.SUPERVISED
        assert cfg.comparison.strategy == ComparisonStrategy.AUTO


class TestListProfiles:
    def test_list_empty(self, tmp_path):
        assert list_profiles(profiles_dir=tmp_path) == []

    def test_list_excludes_template(self, tmp_path):
        _write_profile(tmp_path, "_template", {"name": "template", "source": {"type": "x", "schema": "x", "dsn_env": "x"}, "target": {"type": "x", "schema": "x", "dsn_env": "x"}})
        _write_profile(tmp_path, "real", {"name": "real", "source": {"type": "x", "schema": "x", "dsn_env": "x"}, "target": {"type": "x", "schema": "x", "dsn_env": "x"}})
        profiles = list_profiles(profiles_dir=tmp_path)
        assert profiles == ["real"]

    def test_list_sorted(self, tmp_path):
        for name in ["zebra", "alpha", "mid"]:
            _write_profile(tmp_path, name, {"name": name, "source": {"type": "x", "schema": "x", "dsn_env": "x"}, "target": {"type": "x", "schema": "x", "dsn_env": "x"}})
        profiles = list_profiles(profiles_dir=tmp_path)
        assert profiles == ["alpha", "mid", "zebra"]

    def test_list_ignores_dirs_without_yaml(self, tmp_path):
        (tmp_path / "no-yaml-dir").mkdir()
        assert list_profiles(profiles_dir=tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        assert list_profiles(profiles_dir=tmp_path / "nope") == []


class TestProfileConfigValidation:
    def test_database_config_alias(self):
        dc = DatabaseConfig(type="db2", schema="MY_SCHEMA", dsn_env="DSN")
        assert dc.schema_name == "MY_SCHEMA"

    def test_migration_config_defaults(self):
        mc = MigrationConfig()
        assert mc.mode == MigrationMode.SYNC
        assert mc.batch_size == 5000
        assert mc.workers == 4

    def test_automation_config_defaults(self):
        ac = AutomationConfig()
        assert ac.mode == AutomationMode.SUPERVISED
        assert ac.auto_confirm_below_rows == 0


class TestMigrationModeEnumParsing:
    def test_sync(self):
        assert MigrationMode("sync") == MigrationMode.SYNC

    def test_rollback(self):
        assert MigrationMode("rollback") == MigrationMode.ROLLBACK

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            MigrationMode("invalid")


class TestAutomationModeEnumParsing:
    def test_supervised(self):
        assert AutomationMode("supervised") == AutomationMode.SUPERVISED

    def test_auto_non_prod(self):
        assert AutomationMode("auto_non_prod") == AutomationMode.AUTO_NON_PROD

    def test_auto_approved(self):
        assert AutomationMode("auto_approved") == AutomationMode.AUTO_APPROVED
