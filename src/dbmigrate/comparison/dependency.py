"""FK dependency graph and topological sort.

Builds a dependency graph from FK metadata and provides
both insert-order (parents first) and delete-order (children first)
topological sorts.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field

from dbmigrate.models import TableMetadata

logger = logging.getLogger(__name__)


@dataclass
class DependencyGraph:
    """Directed acyclic graph of table FK dependencies.

    Each edge ``A -> B`` means table *A* has a foreign key referencing
    table *B*, so *B* must be populated before *A* (insert order).

    Attributes
    ----------
    edges:
        ``{child_table: {parent_tables...}}`` — tables that *child* depends on.
    levels:
        ``{table_name: int}`` — topological level (0 = no dependencies).
    ordered_levels:
        List of lists, where index is the level and value is the list
        of tables at that level (insert order: parents first).
    """

    edges: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    reverse_edges: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    levels: dict[str, int] = field(default_factory=dict)
    ordered_levels: list[list[str]] = field(default_factory=list)
    _all_tables: set[str] = field(default_factory=set)

    @classmethod
    def build(cls, tables: dict[str, TableMetadata]) -> DependencyGraph:
        """Construct a dependency graph from table metadata.

        Parameters
        ----------
        tables:
            Mapping of ``table_name -> TableMetadata``, typically from
            :attr:`DatabaseMetadata.tables`.

        Returns
        -------
        DependencyGraph
            Fully resolved graph with topological levels assigned.
        """
        graph = cls()
        graph._all_tables = set(tables.keys())

        # Build adjacency lists from FK constraints
        for table_name, meta in tables.items():
            table_key = table_name.lower()
            # Ensure every table appears even if it has no FKs
            if table_key not in graph.edges:
                graph.edges[table_key] = set()

            for fk in meta.foreign_keys:
                parent = fk.referenced_table.lower()
                if parent == table_key:
                    # Self-referencing FK — skip to avoid trivial cycles
                    logger.debug(
                        "Skipping self-referencing FK '%s' on table '%s'",
                        fk.constraint_name,
                        table_key,
                    )
                    continue
                if parent not in graph._all_tables:
                    logger.warning(
                        "FK '%s' on '%s' references unknown table '%s' — skipping",
                        fk.constraint_name,
                        table_key,
                        parent,
                    )
                    continue
                graph.edges[table_key].add(parent)
                graph.reverse_edges[parent].add(table_key)

        graph._compute_levels()
        return graph

    def get_insert_order(self) -> list[list[str]]:
        """Return tables grouped by dependency level, parents first.

        Level 0 tables have no FK dependencies and can be loaded first.
        Level 1 tables depend only on level 0 tables, and so on.

        Returns
        -------
        list[list[str]]
            Each inner list contains tables that can be loaded in
            parallel within that level.
        """
        return [list(level) for level in self.ordered_levels]

    def get_delete_order(self) -> list[list[str]]:
        """Return tables grouped by dependency level, children first.

        This is the reverse of insert order — child tables (highest
        level) are deleted first to satisfy FK constraints.

        Returns
        -------
        list[list[str]]
            Each inner list contains tables that can be deleted in
            parallel within that level.
        """
        return list(reversed(self.get_insert_order()))

    def get_level(self, table_name: str) -> int:
        """Return the dependency level of a table.

        Parameters
        ----------
        table_name:
            Table name (case-insensitive).

        Returns
        -------
        int
            Dependency level (0 = no dependencies).

        Raises
        ------
        KeyError
            If *table_name* is not in the graph.
        """
        key = table_name.lower()
        if key not in self.levels:
            raise KeyError(f"Table '{table_name}' not found in dependency graph")
        return self.levels[key]

    # -- internal ----------------------------------------------------------

    def _compute_levels(self) -> None:
        """Assign topological levels using Kahn's algorithm.

        Handles cycles by logging a warning and breaking them
        arbitrarily (remaining nodes are assigned to the next level).
        """
        # In-degree: number of dependencies each table has
        in_degree: dict[str, int] = {}
        for table in self._all_tables:
            key = table.lower()
            in_degree[key] = len(self.edges.get(key, set()))

        # Seed: tables with no dependencies
        queue: deque[str] = deque()
        for table, deg in in_degree.items():
            if deg == 0:
                queue.append(table)

        level = 0
        processed = 0
        self.ordered_levels = []

        while queue:
            current_level: list[str] = sorted(queue)
            queue.clear()
            self.ordered_levels.append(current_level)

            for table in current_level:
                self.levels[table] = level
                processed += 1

                # Reduce in-degree of dependants
                for child in self.reverse_edges.get(table, set()):
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        queue.append(child)

            level += 1

        # Cycle detection: any remaining tables with in_degree > 0
        remaining = [t for t, d in in_degree.items() if d > 0]
        if remaining:
            logger.warning(
                "Dependency cycle detected involving %d table(s): %s. "
                "Breaking cycle by assigning them to level %d.",
                len(remaining),
                ", ".join(sorted(remaining)),
                level,
            )
            cycle_level = sorted(remaining)
            self.ordered_levels.append(cycle_level)
            for table in cycle_level:
                self.levels[table] = level

        total = sum(len(lv) for lv in self.ordered_levels)
        logger.info(
            "Dependency graph: %d tables across %d levels",
            total,
            len(self.ordered_levels),
        )
