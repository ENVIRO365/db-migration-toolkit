# Known Limitations

This document lists known limitations, unverified assumptions, and constraints of the DB Migration Toolkit. Each item includes its risk level and mitigation path.

## Unverified Assumptions

### 1. DB2 LOAD Utility Access

**Risk: HIGH**

The DB2 LOAD utility (`LOAD FROM ... INSERT INTO ...`) is the fallback strategy for tables with more than 10 million rows (e.g., `jmssystemadaptermessagestatus` at 39.1M rows). LOAD bypasses logging and runs 10-100x faster than batched INSERT.

**Assumption**: The `db2` CLI and LOAD utility are accessible from the execution environment (the machine running the toolkit).

**What could go wrong**: If the toolkit runs on a Linux server that does not have the DB2 client installed, or if the DB2 instance does not grant LOAD authority to the connection user, the LOAD fallback will fail silently and the toolkit will fall back to batched INSERT — which may take hours for 39M rows.

**Mitigation**: Before running a production rollback, execute:
```bash
db2 "CONNECT TO <database>"
db2 "LOAD FROM /dev/null OF DEL INSERT INTO WEALTHADAPTER.SYSTEMSTATUSLOG"
```
If the LOAD command is not found or permission is denied, plan for extended INSERT times or arrange LOAD authority with the DBA.

### 2. DB2 TIMESTAMP Fractional Precision

**Risk: MEDIUM**

PostgreSQL stores timestamps with microsecond precision (6 fractional digits). DB2 10.5+ supports microsecond precision, but older DB2 versions may truncate to 3 digits (millisecond).

**Assumption**: The target DB2 instance is version 10.5 or later and supports 6-digit fractional seconds.

**What could go wrong**: If DB2 truncates to 3 digits, post-migration checksums will fail because the source (PG) has 6 digits and the target (DB2) has 3. The data is not corrupted — just less precise.

**Mitigation**: Query DB2 version before migration:
```sql
SELECT SERVICE_LEVEL FROM TABLE(SYSPROC.ENV_GET_INST_INFO());
```
If version < 10.5, adjust the checksum algorithm to truncate PG timestamps to 3 fractional digits before comparison.

### 3. CLOB Encoding with Multi-Byte Characters

**Risk: LOW**

Both PG and DB2 use UTF-8, but DB2's VARCHAR columns define length in bytes (not characters) by default. A VARCHAR(255) column in DB2 holds 255 bytes, which is only 85 characters if every character is 3-byte CJK.

**Assumption**: The Wealth Adapter data is primarily ASCII/Latin-1 (South African English, Afrikaans) and does not contain multi-byte characters that would overflow DB2 byte-length limits.

**What could go wrong**: If any column value contains multi-byte characters and the DB2 column is VARCHAR(N) where N is tight, the INSERT will fail with `SQLSTATE 22001` (string data right truncation).

**Mitigation**: During schema comparison, check if any PG VARCHAR values exceed the DB2 column's byte length:
```sql
SELECT MAX(OCTET_LENGTH(column_name)) FROM wealthadapter.table_name;
```
Compare against the DB2 column's byte limit.

### 4. MCP Query Row Counts May Be Estimates

**Risk: LOW**

The PostgreSQL MCP server used during investigation returns row counts that may be based on `pg_class.reltuples` (a cardinality estimate updated by ANALYZE) rather than exact `SELECT COUNT(*)`. For tables with heavy write activity, the estimate can drift by 5-10%.

**Assumption**: The row counts in `docs/progress.md` are approximate. The toolkit performs exact `SELECT COUNT(*)` at runtime.

**What could go wrong**: The investigation document says a table has ~5.9M rows but the actual count is 6.1M. This does not affect correctness — the toolkit counts at runtime — but it may affect time estimates.

**Mitigation**: None needed. This is a documentation accuracy issue, not a runtime issue.

## Technical Limitations

### 5. ibm_db Python Driver Streaming

**Risk: MEDIUM**

The `ibm_db` / `ibm_db_dbi` Python driver for DB2 does not support true server-side cursors in the same way `psycopg2` does for PostgreSQL. Specifically:

- `fetchmany(size)` fetches from a client-side buffer, not a server-side cursor
- Large result sets are fully materialized in client memory before iteration begins
- There is no equivalent of `psycopg2.extras.execute_values()` for high-speed bulk insert

**What could go wrong**: When reading from a DB2 source (not applicable for Wealth Adapter rollback, where PG is the source), memory usage may spike for tables with millions of rows.

**Mitigation**: When DB2 is the source, use `FETCH FIRST N ROWS ONLY` with PK-based pagination instead of a single streaming cursor. When DB2 is the target, use multi-row INSERT (`INSERT INTO ... VALUES (...), (...), (...)`) or LOAD utility for throughput.

### 6. No Support for Partitioned Tables

**Risk: LOW (for Wealth Adapter)**

The toolkit does not detect or handle DB2 table partitioning (RANGE, HASH, or LIST partitioning). If a target table is partitioned, the INSERT will work (DB2 routes rows to the correct partition automatically), but the toolkit cannot:

- Detach/attach partitions for faster loading
- Target a specific partition for cleanup
- Report per-partition row counts

**What could go wrong**: Performance may be suboptimal for large partitioned tables because the toolkit treats them as single tables.

**Mitigation**: For the Wealth Adapter use case, no tables are partitioned. If a future profile requires partitioned table support, extend the `Database` ABC with `get_partitions()` and `load_partition()` methods.

### 7. No Support for Materialized Views

**Risk: LOW**

The toolkit does not detect, refresh, or migrate materialized views. If a materialized view depends on a migrated table, it will become stale after migration.

**What could go wrong**: A downstream report or query that reads from a materialized view returns stale data after rollback.

**Mitigation**: Document materialized views that depend on migrated tables in the profile's `known_quirks`. After migration, manually refresh:
```sql
-- DB2
REFRESH TABLE schema.materialized_view;
-- PostgreSQL
REFRESH MATERIALIZED VIEW schema.materialized_view;
```

### 8. VARCHAR Byte-Length vs Character-Length Not Dynamically Validated

**Risk: MEDIUM**

During schema comparison, the toolkit compares column types and lengths between source and target. However, it does not currently validate whether actual data values fit within the target column's byte-length constraint.

For example, PG `VARCHAR(255)` means 255 characters. DB2 `VARCHAR(255)` means 255 bytes. If the data contains multi-byte UTF-8 characters, a value that fits in PG may not fit in DB2.

**What could go wrong**: INSERT fails at runtime with `SQLSTATE 22001` on a specific row that has multi-byte content.

**Mitigation**: Add a pre-flight validation step that scans source columns for `MAX(OCTET_LENGTH(col))` and compares against the target column's byte limit. Flag violations before migration begins.

### 9. No Cross-Schema Foreign Key Support

**Risk: LOW (for Wealth Adapter)**

The toolkit resolves FK dependencies within a single schema to determine insert order. It does not resolve FKs that cross schema boundaries.

The `userdetail` triggers write to `WEALTH.CHGDATALOG`, which is in a different schema. This is handled by DB2's trigger engine, not by the toolkit. But if a future migration involves tables with cross-schema FKs (e.g., a column in schema A references a PK in schema B), the toolkit would not detect the dependency.

**Mitigation**: Document cross-schema dependencies in the profile's `known_quirks`. The toolkit will not enforce insert order across schemas — the operator must ensure the referenced table is populated first.

### 10. Single-Run, Single-Profile

**Risk: LOW**

The toolkit processes one profile per CLI invocation. It cannot run multiple profiles in parallel or chain profiles in a pipeline within a single command.

**Mitigation**: Use shell scripting or a task runner (e.g., `make`, `just`) to orchestrate multiple profile runs:
```bash
dbmigrate migrate wealth-adapter-rollback --execute
dbmigrate migrate wealth-core-rollback --execute
```

### 11. No Bi-Directional Sync

**Risk: LOW**

The toolkit moves data in one direction: source → target. It does not support bi-directional sync, conflict resolution, or merge strategies. If both source and target have been independently modified, the operator must manually classify operations in the profile.

**Mitigation**: For bi-directional scenarios, run two separate profiles (A→B and B→A) with explicit table-level operation classification. Use `delete_allowed_tables` and `known_quirks` to handle conflicts.

### 12. No Schema Migration (DDL)

**Risk: NONE (by design)**

The toolkit migrates data (DML) only. It does not create tables, alter columns, add indexes, or modify constraints. The target schema must already exist and match the expected structure.

**Mitigation**: Use Flyway, Liquibase, or manual DDL scripts to prepare the target schema before running the toolkit.
