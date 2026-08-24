# DB Migration Toolkit — Progress Tracker

## Gate Status

| Gate | Status | Evidence | Date |
|------|--------|----------|------|
| Gate 1 — Environment | PASSED | Python 3.12.3, PG MCP verified (cis-lh-adapter-dev), DB2 MCP verified (WEALTH), 66GB disk | 2026-08-24 |
| Gate 2 — Database Discovery | PASSED | Source: 28 PG tables (26 business + 2 infra), Target: 26 DB2 tables. Identity strategies match. FK relationships verified. | 2026-08-24 |
| Gate 3 — Rollback Design | IN PROGRESS | Architecture designed, adapter contract defined, dependency graph built | 2026-08-24 |
| Gate 4 — Proof of Concept | PENDING | | |
| Gate 5 — Performance | PENDING | | |
| Gate 6 — Full Implementation | PENDING | | |
| Gate 7 — Non-Production Validation | PENDING | | |

## Per-Table INSERT/UPDATE/DELETE/NO_ACTION Classification

(Per Section 9a — explicitly classified, not inferred)

| Table | Operation | Row Count | Rationale |
|-------|-----------|-----------|-----------|
| adapterconfig | INSERT | ~105 | DB2 has 423, PG has 528. Delta INSERT only |
| directorylocation | NO_ACTION | 0 | Both have 4 rows, identical |
| emailaddress | NO_ACTION | 0 | Both have 5985 rows, identical |
| emailgroup | NO_ACTION | 0 | Both have 257 rows, identical |
| emailgroup_emailaddress | NO_ACTION | 0 | Both have 1050 rows |
| facsmessageentity | INSERT | ~5.9M | DB2 empty, full import |
| facsmessagehistoryitementity | INSERT | ~12.7M | DB2 empty, full import |
| gicsawasyncmessage | INSERT | ~9.8M | DB2 empty, full import |
| incomingfile | INSERT | ~8.5K | DB2 empty, GENERATED ALWAYS |
| jmssystemadaptermessage | INSERT | ~3.5M | DB2 empty, full import |
| jmssystemadaptermessagestatus | INSERT | ~39.1M | DB2 empty, full import |
| outboundemail | INSERT | ~222K | DB2 empty, GENERATED ALWAYS |
| recipient | NO_ACTION | 0 | Both have 268 rows, identical |
| recipient_dirlocations | NO_ACTION | 0 | Both have 43 rows |
| recipient_emailgroup | NO_ACTION | 0 | Both have 263 rows |
| recipient_webservices | NO_ACTION | 0 | Both have 5 rows |
| remote_interaction_log | INSERT | 164 | DB2 empty, GENERATED ALWAYS |
| role_accessright | NO_ACTION | 0 | Both have 166 rows |
| smsmessagelog | INSERT | ~60K | DB2 empty, full import |
| systemstatuslog | INSERT | 18 | DB2 empty, accept data gap |
| userdetail | INSERT | 861 | DB2 empty, has triggers |
| userrole | NO_ACTION | 0 | Both have 9 rows |
| voc_message_log | NO_ACTION | 0 | Both have 0 rows |
| wealthaccessright | NO_ACTION | 0 | Both have 72 rows |
| webservice | NO_ACTION | 0 | Both have 5 rows |
| webservicestatusmessage | INSERT | ~9.5K | DB2 empty, full import |

**Summary**: 14 tables INSERT, 0 UPDATE, 0 DELETE, 12 NO_ACTION
**No DELETE operations** in this rollback — DB2 was the original, PG is a migration target. No rows need removal from DB2.

## Open Questions
- DB2 LOAD utility access: verify if db2 CLI is available from the execution environment
- DB2 TIMESTAMP fractional precision match
