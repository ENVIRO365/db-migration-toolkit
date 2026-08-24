"""Tests for dbmigrate.comparison.dependency — FK dependency graph."""

from __future__ import annotations

import pytest

from dbmigrate.comparison.dependency import DependencyGraph
from dbmigrate.models import ForeignKeyMetadata, PrimaryKeyMetadata, TableMetadata


def _table(name: str, fks: list[ForeignKeyMetadata] | None = None) -> TableMetadata:
    """Helper to build a minimal TableMetadata with optional FKs."""
    return TableMetadata(
        name=name,
        schema="public",
        primary_key=PrimaryKeyMetadata(columns=["id"]),
        foreign_keys=fks or [],
    )


def _fk(child_col: str, parent_table: str) -> ForeignKeyMetadata:
    return ForeignKeyMetadata(
        constraint_name=f"fk_{child_col}_{parent_table}",
        columns=[child_col],
        referenced_table=parent_table,
        referenced_columns=["id"],
    )


class TestNoDependencies:
    def test_all_at_level_zero(self):
        tables = {
            "a": _table("a"),
            "b": _table("b"),
            "c": _table("c"),
        }
        graph = DependencyGraph.build(tables)
        assert graph.get_level("a") == 0
        assert graph.get_level("b") == 0
        assert graph.get_level("c") == 0

    def test_insert_order_single_level(self):
        tables = {"x": _table("x"), "y": _table("y")}
        graph = DependencyGraph.build(tables)
        order = graph.get_insert_order()
        assert len(order) == 1
        assert set(order[0]) == {"x", "y"}


class TestLinearChain:
    """A -> B -> C  (C depends on B, B depends on A)."""

    @pytest.fixture
    def graph(self):
        tables = {
            "a": _table("a"),
            "b": _table("b", fks=[_fk("a_id", "a")]),
            "c": _table("c", fks=[_fk("b_id", "b")]),
        }
        return DependencyGraph.build(tables)

    def test_levels(self, graph):
        assert graph.get_level("a") == 0
        assert graph.get_level("b") == 1
        assert graph.get_level("c") == 2

    def test_insert_order_parents_first(self, graph):
        order = graph.get_insert_order()
        assert len(order) == 3
        assert "a" in order[0]
        assert "b" in order[1]
        assert "c" in order[2]

    def test_delete_order_children_first(self, graph):
        order = graph.get_delete_order()
        assert "c" in order[0]
        assert "b" in order[1]
        assert "a" in order[2]


class TestDiamondDependencies:
    """
    D depends on B and C; B and C both depend on A.

        A
       / \\
      B   C
       \\ /
        D
    """

    @pytest.fixture
    def graph(self):
        tables = {
            "a": _table("a"),
            "b": _table("b", fks=[_fk("a_id", "a")]),
            "c": _table("c", fks=[_fk("a_id", "a")]),
            "d": _table("d", fks=[_fk("b_id", "b"), _fk("c_id", "c")]),
        }
        return DependencyGraph.build(tables)

    def test_levels(self, graph):
        assert graph.get_level("a") == 0
        assert graph.get_level("b") == 1
        assert graph.get_level("c") == 1
        assert graph.get_level("d") == 2

    def test_insert_order(self, graph):
        order = graph.get_insert_order()
        assert "a" in order[0]
        assert set(order[1]) == {"b", "c"}
        assert "d" in order[2]


class TestCycleDetection:
    """A -> B -> A creates a cycle."""

    def test_cycle_handled_gracefully(self):
        tables = {
            "a": _table("a", fks=[_fk("b_id", "b")]),
            "b": _table("b", fks=[_fk("a_id", "a")]),
        }
        # Should not raise; cycles are broken with a warning
        graph = DependencyGraph.build(tables)
        # Both tables should be assigned a level
        assert "a" in graph.levels
        assert "b" in graph.levels


class TestSelfReferencingFK:
    """Self-referencing FK should be skipped."""

    def test_self_ref_ignored(self):
        tables = {
            "tree": _table("tree", fks=[_fk("parent_id", "tree")]),
        }
        graph = DependencyGraph.build(tables)
        assert graph.get_level("tree") == 0


class TestGetLevelUnknownTable:
    def test_raises_key_error(self):
        tables = {"a": _table("a")}
        graph = DependencyGraph.build(tables)
        with pytest.raises(KeyError, match="not found"):
            graph.get_level("nonexistent")


class TestWealthAdapterFKStructure:
    """
    Simplified version of the Wealth Adapter FK structure:

    - config (no deps)
    - party (no deps)
    - party_address -> party
    - party_contact -> party
    - account -> party
    - transaction -> account
    """

    @pytest.fixture
    def graph(self):
        tables = {
            "config": _table("config"),
            "party": _table("party"),
            "party_address": _table("party_address", fks=[_fk("party_id", "party")]),
            "party_contact": _table("party_contact", fks=[_fk("party_id", "party")]),
            "account": _table("account", fks=[_fk("party_id", "party")]),
            "transaction": _table("transaction", fks=[_fk("account_id", "account")]),
        }
        return DependencyGraph.build(tables)

    def test_root_tables_at_level_0(self, graph):
        assert graph.get_level("config") == 0
        assert graph.get_level("party") == 0

    def test_direct_children_at_level_1(self, graph):
        assert graph.get_level("party_address") == 1
        assert graph.get_level("party_contact") == 1
        assert graph.get_level("account") == 1

    def test_grandchild_at_level_2(self, graph):
        assert graph.get_level("transaction") == 2

    def test_insert_order_total(self, graph):
        order = graph.get_insert_order()
        assert len(order) == 3  # 3 levels
        flat_order = [t for level in order for t in level]
        # transaction must come after account
        assert flat_order.index("transaction") > flat_order.index("account")
        # account must come after party
        assert flat_order.index("account") > flat_order.index("party")
