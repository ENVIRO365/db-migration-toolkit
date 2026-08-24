# Migration Runbook Template

This is a generic runbook template. Copy and fill in for each migration profile and environment.

---

## Runbook: `<PROFILE_NAME>` — `<ENVIRONMENT>`

**Date**: YYYY-MM-DD
**Operator**: <name>
**Profile**: `profiles/<profile>/profile.yaml`
**Source**: `<source_type>` — `<source_host>/<source_db>` — schema `<source_schema>`
**Target**: `<target_type>` — `<target_host>/<target_db>` — schema `<target_schema>`

---

## Pre-Flight Checklist

| # | Check | Status | Notes |
|---|-------|--------|-------|
| 1 | Profile YAML reviewed and committed | ☐ | |
| 2 | Source DSN env var set and tested | ☐ | `echo $<SOURCE_DSN_ENV>` |
| 3 | Target DSN env var set and tested | ☐ | `echo $<TARGET_DSN_ENV>` |
| 4 | Source database accessible | ☐ | `dbmigrate compare <profile> --source-only` |
| 5 | Target database accessible | ☐ | `dbmigrate compare <profile> --target-only` |
| 6 | Target schema exists with all tables | ☐ | DDL applied via Flyway/Liquibase |
| 7 | Disk space sufficient for checkpoints | ☐ | `df -h .` |
| 8 | Disk space sufficient for CSV export (if LOAD fallback) | ☐ | Estimate: <N> GB |
| 9 | Downstream consumers notified | ☐ | List: <consumers> |
| 10 | Backup of target database taken | ☐ | Backup ID: <id> |
| 11 | Known quirks reviewed | ☐ | See profile.yaml `known_quirks` |
| 12 | GENERATED ALWAYS tables identified | ☐ | Tables: <list> |
| 13 | Trigger behavior documented | ☐ | Tables: <list> |
| 14 | Maintenance window confirmed | ☐ | Window: HH:MM — HH:MM |

## Execution Steps

### Step 1: Dry Run

```bash
dbmigrate migrate <profile>
```

Review the output. Confirm:
- [ ] Table list matches expectations
- [ ] Operation classification (INSERT/UPDATE/DELETE/NO_ACTION) is correct
- [ ] Row counts are reasonable
- [ ] No unexpected DELETE operations
- [ ] Source-only columns correctly excluded

### Step 2: Execute Migration

```bash
dbmigrate migrate <profile> --execute
```

Monitor:
- [ ] Progress bar advancing
- [ ] No error messages in output
- [ ] Checkpoint files being written (`checkpoints/<profile>/`)

If prompted for confirmation (supervised mode), review each table before confirming.

### Step 3: Monitor Progress

In a separate terminal:

```bash
# Watch checkpoint updates
watch -n 5 'cat checkpoints/<profile>/latest.json | python3 -m json.tool'

# Watch logs
tail -f logs/dbmigrate.log
```

### Step 4: Handle Failures

If the migration fails mid-run:

1. **Do not panic.** Checkpoints are saved after every batch.
2. Note the error message and the table that failed.
3. Check the checkpoint file for the last successful batch.
4. Fix the underlying issue (network, permissions, data issue).
5. Resume:

```bash
dbmigrate checkpoint resume <profile> <run_id>
```

### Step 5: Post-Migration Validation

```bash
dbmigrate validate <profile>
```

Confirm:
- [ ] All tables show matching row counts
- [ ] Checksums match (if checksum strategy enabled)
- [ ] Identity sequences reset correctly
- [ ] Triggers re-enabled (if they were disabled)

### Step 6: Generate Report

```bash
dbmigrate report <profile> <run_id>
```

Save the report for audit:

```bash
dbmigrate report <profile> <run_id> > reports/<profile>-<date>.md
```

## Post-Migration Checklist

| # | Check | Status | Notes |
|---|-------|--------|-------|
| 1 | Row counts match source and target | ☐ | |
| 2 | Checksums match (if applicable) | ☐ | |
| 3 | Identity sequences reset | ☐ | |
| 4 | Triggers restored to original state | ☐ | |
| 5 | Application smoke test passed | ☐ | |
| 6 | Downstream consumers verified | ☐ | |
| 7 | Monitoring dashboards show no anomalies | ☐ | |
| 8 | Report generated and archived | ☐ | |
| 9 | Team notified of completion | ☐ | |

## Rollback Procedure

If the migration must be reversed:

1. **If target was empty before migration**: DELETE or TRUNCATE the migrated tables in reverse dependency order.
2. **If target had existing data**: Restore from the pre-migration backup (Step 10 in pre-flight).
3. **Reset sequences** to pre-migration values.
4. **Re-enable triggers** if they were disabled.
5. **Notify downstream consumers** of the rollback.

```bash
# Restore from backup (example for PostgreSQL target)
pg_restore --clean --if-exists -d <database> <backup_file>

# Restore from backup (example for DB2 target)
db2 RESTORE DATABASE <database> FROM <backup_path>
```

## Contacts

| Role | Name | Contact |
|------|------|---------|
| Migration operator | <name> | <email/phone> |
| Source DBA | <name> | <email/phone> |
| Target DBA | <name> | <email/phone> |
| Application owner | <name> | <email/phone> |
| On-call engineer | <name> | <email/phone> |

## Appendix: Timing Estimates

| Table | Rows | Estimated Time | Actual Time |
|-------|------|---------------|-------------|
| <table1> | <N> | <estimate> | |
| <table2> | <N> | <estimate> | |
| **Total** | **<N>** | **<estimate>** | |
