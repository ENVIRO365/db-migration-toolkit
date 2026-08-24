# Wealth Adapter Rollback — Investigation Reference

The full investigation document is maintained at:
`/docs/rollback/rollback-investigation.md` (in the wealth-admin-app repository)

## Summary
- Source: PostgreSQL (cis-lh-adapter-dev, wealthadapter schema, ~72M rows)
- Target: DB2 (WEALTHADAPTER schema, 13 of 26 tables empty)
- Strategy: Delta INSERT for 5 tables with shared data; Full LOAD for 13 empty DB2 tables; NO_ACTION for 8 in-sync tables
- Identity: 4 GENERATED ALWAYS tables require IDENTITYOVERRIDE; 16 BY DEFAULT; 1 no identity
- Triggers: 3 on userdetail → WEALTH.CHGDATALOG (active in DB2, non-functional in PG)
- Skip: flyway_schema_history, shedlock, cdc_outbox
