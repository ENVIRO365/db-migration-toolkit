"""Tests for dbmigrate.policy — AutomationPolicy and environment detection."""

from __future__ import annotations

import pytest

from dbmigrate.config import AutomationConfig
from dbmigrate.models import (
    AutomationMode,
    MigrationManifest,
    MigrationMode,
    MigrationOperation,
    MigrationTablePlan,
    TableDelta,
)
from dbmigrate.policy import AutomationPolicy, resolve_environment


# ---------------------------------------------------------------------------
# resolve_environment
# ---------------------------------------------------------------------------


class TestResolveEnvironment:
    @pytest.mark.parametrize("dsn,expected", [
        ("postgresql://db-prod.example.com:5432/mydb", "production"),
        ("postgresql://production-db.internal:5432/wealth", "production"),
        ("postgresql://prd-server.co.za:5432/app", "production"),
        ("postgresql://live.server.com:5432/app", "production"),
        ("postgresql://prod-db.co.za:5432/app", "production"),
    ])
    def test_production(self, dsn, expected):
        assert resolve_environment(dsn) == expected

    @pytest.mark.parametrize("dsn,expected", [
        ("postgresql://localhost:5432/testdb", "development"),
        ("postgresql://dev-server.co.za:5432/wealth_dev", "development"),
        ("postgresql://127.0.0.1:5432/mydb", "development"),
        ("postgresql://test-server.co.za:5432/app", "development"),
    ])
    def test_development(self, dsn, expected):
        assert resolve_environment(dsn) == expected

    @pytest.mark.parametrize("dsn,expected", [
        ("postgresql://pre.investments.momentum.co.za:5432/app", "pre-production"),
        ("postgresql://staging-db.example.com:5432/mydb", "pre-production"),
        ("postgresql://uat-server.co.za:5432/app", "pre-production"),
    ])
    def test_pre_production(self, dsn, expected):
        assert resolve_environment(dsn) == expected

    def test_unknown(self):
        assert resolve_environment("host=mysterious.server.co.za;database=app") == "unknown"


# ---------------------------------------------------------------------------
# AutomationPolicy.is_production_target
# ---------------------------------------------------------------------------


class TestIsProductionTarget:
    def test_production_hostname(self):
        config = AutomationConfig(mode=AutomationMode.SUPERVISED)
        policy = AutomationPolicy(config, "host=prod-db.co.za;database=app")
        assert policy.is_production_target() is True

    def test_dev_hostname(self):
        config = AutomationConfig(mode=AutomationMode.SUPERVISED)
        policy = AutomationPolicy(config, "postgresql://dev-db.co.za:5432/app")
        assert policy.is_production_target() is False

    def test_unknown_treated_as_prod(self):
        config = AutomationConfig(mode=AutomationMode.SUPERVISED)
        policy = AutomationPolicy(config, "host=mysterious.co.za;database=app")
        assert policy.is_production_target() is True

    def test_custom_dsn_override(self):
        config = AutomationConfig(mode=AutomationMode.SUPERVISED)
        policy = AutomationPolicy(config, "postgresql://dev.local:5432/x")
        # Override with a production DSN
        assert policy.is_production_target("host=prod.co.za;database=y") is True


# ---------------------------------------------------------------------------
# AutomationPolicy.requires_confirmation
# ---------------------------------------------------------------------------


def _manifest(total_rows: int = 100, has_deletes: bool = False) -> MigrationManifest:
    tables = [
        MigrationTablePlan(table_name="t1", operation=MigrationOperation.INSERT, row_count=total_rows),
    ]
    if has_deletes:
        tables.append(
            MigrationTablePlan(
                table_name="t2", operation=MigrationOperation.DELETE, row_count=10,
                delta=TableDelta(table_name="t2", delete_pks=list(range(10))),
            ),
        )
    return MigrationManifest(
        migration_id="m-001", profile_name="test",
        mode=MigrationMode.SYNC, tables=tables, total_rows=total_rows,
    )


class TestSupervisedMode:
    def test_always_requires_confirmation(self):
        config = AutomationConfig(mode=AutomationMode.SUPERVISED)
        policy = AutomationPolicy(config, "postgresql://dev.local:5432/x")
        assert policy.requires_confirmation(_manifest()) is True


class TestAutoNonProdMode:
    def test_requires_for_prod_target(self):
        config = AutomationConfig(mode=AutomationMode.AUTO_NON_PROD)
        policy = AutomationPolicy(config, "host=prod-db.co.za;database=app")
        assert policy.requires_confirmation(_manifest()) is True

    def test_no_confirmation_for_dev_target(self):
        config = AutomationConfig(mode=AutomationMode.AUTO_NON_PROD)
        policy = AutomationPolicy(config, "postgresql://dev.local:5432/x")
        assert policy.requires_confirmation(_manifest()) is False

    def test_threshold_exceeded(self):
        config = AutomationConfig(mode=AutomationMode.AUTO_NON_PROD, auto_confirm_below_rows=50)
        policy = AutomationPolicy(config, "postgresql://dev.local:5432/x")
        assert policy.requires_confirmation(_manifest(total_rows=100)) is True

    def test_threshold_not_exceeded(self):
        config = AutomationConfig(mode=AutomationMode.AUTO_NON_PROD, auto_confirm_below_rows=200)
        policy = AutomationPolicy(config, "postgresql://dev.local:5432/x")
        assert policy.requires_confirmation(_manifest(total_rows=100)) is False


class TestAutoApprovedMode:
    def test_no_confirmation(self):
        config = AutomationConfig(mode=AutomationMode.AUTO_APPROVED)
        policy = AutomationPolicy(config, "host=prod-db.co.za;database=app")
        assert policy.requires_confirmation(_manifest()) is False


class TestDeleteConfirmation:
    def test_always_required_when_deletes_present(self):
        config = AutomationConfig(mode=AutomationMode.AUTO_APPROVED)
        policy = AutomationPolicy(config, "postgresql://dev.local:5432/x")
        assert policy.requires_delete_confirmation(_manifest(has_deletes=True)) is True

    def test_not_required_when_no_deletes(self):
        config = AutomationConfig(mode=AutomationMode.AUTO_APPROVED)
        policy = AutomationPolicy(config, "postgresql://dev.local:5432/x")
        assert policy.requires_delete_confirmation(_manifest(has_deletes=False)) is False
