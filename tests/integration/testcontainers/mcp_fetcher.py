"""Fetch data from source databases for test container population.

This module supports multiple data sources for populating local test
containers.  Sources are resolved in priority order from ``.env`` variables:

  1. ``DB2_WEALTH_TST_DSN`` — ibm_db DSN format (direct Db2 connection)
  2. ``WA_TARGET_DSN``      — URL format (user:pass@host:port/db → parsed to ibm_db)
  3. ``WA_SOURCE_DSN``      — PostgreSQL URL (psycopg2 connection)
  4. Embedded seed data     — Hardcoded minimal dataset (fallback / CI)

The ``--source`` flag in ``populate_test_dbs.py`` allows forcing a specific
source: ``db2``, ``pg``, ``embedded``, or ``auto`` (default, walks the list).

Target tables (WEALTHADAPTER schema, 26 tables):
  ┌──────────────────────────────────────┬───────┬──────────────────────────────────────────┐
  │ Table                                │ ~Rows │ Purpose                                  │
  ├──────────────────────────────────────┼───────┼──────────────────────────────────────────┤
  │ USERROLE                             │     9 │ Role reference data                      │
  │ WEALTHACCESSRIGHT                    │    72 │ Access right definitions                  │
  │ ROLE_ACCESSRIGHT                     │   166 │ Role → right mapping (composite PK)      │
  │ ADAPTERCONFIG                        │   394 │ Key-value configuration                   │
  │ EMAILGROUP                           │   257 │ Email distribution groups                 │
  │ EMAILADDRESS                         │       │ Email addresses within groups             │
  │ EMAILGROUP_EMAILADDRESS              │       │ Join table: emailgroup ↔ emailaddress    │
  │ RECIPIENT                            │   268 │ Message recipients                        │
  │ DIRECTORYLOCATION                    │     4 │ Directory path references                 │
  │ RECIPIENT_DIRLOCATIONS               │       │ Join table: recipient ↔ directorylocation│
  │ RECIPIENT_EMAILGROUP                 │       │ Join table: recipient ↔ emailgroup       │
  │ RECIPIENT_WEBSERVICES                │       │ Join table: recipient ↔ webservice       │
  │ WEBSERVICE                           │       │ External web service endpoints            │
  │ WEBSERVICESTATUSMESSAGE              │       │ Status messages for web services          │
  │ USERDETAIL                           │       │ User profile details                      │
  │ FACSMESSAGEENTITY                    │       │ FACS integration messages                 │
  │ FACSMESSAGEHISTORYITEMENTITY         │       │ FACS message history                      │
  │ GICSAWASYNCMESSAGE                   │       │ GICSA async correlation messages          │
  │ INCOMINGFILE                          │       │ Incoming file tracking                    │
  │ JMSSYSTEMADAPTERMESSAGE              │       │ JMS system adapter messages               │
  │ JMSSYSTEMADAPTERMESSAGESTATUS        │       │ JMS message status history                │
  │ OUTBOUNDEMAIL                        │       │ Outbound email queue (XML)                │
  │ REMOTE_INTERACTION_LOG               │       │ Remote service call audit log             │
  │ SMSMESSAGELOG                        │       │ SMS message audit log                     │
  │ SYSTEMSTATUSLOG                      │       │ System status tracking                    │
  │ VOC_MESSAGE_LOG                      │       │ VOC message audit log (XML)               │
  └──────────────────────────────────────┴───────┴──────────────────────────────────────────┘

All fetched data is returned as a normalized ``dict[str, list[dict]]``
keyed by lowercase table name, where each row is a plain dict.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ── Table definitions ────────────────────────────────────────────────────

SCHEMA = "WEALTHADAPTER"

# Ordered by FK dependency (independent tables first).
TABLE_QUERIES: dict[str, str] = {
    # ── Independent reference tables ─────────────────────────────────────
    "userrole": f"SELECT ID, ROLE FROM {SCHEMA}.USERROLE ORDER BY ID",
    "wealthaccessright": f"SELECT ID, NAME FROM {SCHEMA}.WEALTHACCESSRIGHT ORDER BY ID",
    "role_accessright": (
        f"SELECT ROLE_ID, ACCESSRIGHT_ID FROM {SCHEMA}.ROLE_ACCESSRIGHT "
        f"ORDER BY ROLE_ID, ACCESSRIGHT_ID"
    ),
    "adapterconfig": (
        f"SELECT ID, KEY, VALUE FROM {SCHEMA}.ADAPTERCONFIG "
        f"WHERE KEY NOT LIKE '%password%' "
        f"AND KEY NOT LIKE '%secret%' "
        f"AND KEY NOT LIKE '%pwd%' "
        f"ORDER BY ID"
    ),
    "userdetail": (
        f"SELECT ID, DATEPASSWORDCHANGED, EMAIL, PERSONNELNUMBER, SURNAME, "
        f"TELEPHONENUMBER, UID, USERNAME, DOMAIN_USER "
        f"FROM {SCHEMA}.USERDETAIL ORDER BY ID"
    ),
    "systemstatuslog": (
        f"SELECT ID, ACTION, HOSTNAME, STATUS, STATUSMESSAGE, SYSTEMNAME, TIMESTAMP "
        f"FROM {SCHEMA}.SYSTEMSTATUSLOG ORDER BY ID"
    ),
    # ── Email / recipient hierarchy ──────────────────────────────────────
    "emailgroup": f"SELECT ID, NAME FROM {SCHEMA}.EMAILGROUP ORDER BY ID",
    "emailaddress": (
        f"SELECT ID, DESCRIPTION, EMAILADDRESS, EMAILGROUP_FK "
        f"FROM {SCHEMA}.EMAILADDRESS ORDER BY ID"
    ),
    "emailgroup_emailaddress": (
        f"SELECT EMAILGROUP_ID, EMAILADDRESSES_ID "
        f"FROM {SCHEMA}.EMAILGROUP_EMAILADDRESS "
        f"ORDER BY EMAILGROUP_ID, EMAILADDRESSES_ID"
    ),
    "directorylocation": (
        f"SELECT ID, DIR, FILEMASK FROM {SCHEMA}.DIRECTORYLOCATION ORDER BY ID"
    ),
    "webservice": (
        f"SELECT ID, TYPE, USERNAME, COMPANYCODE "
        f"FROM {SCHEMA}.WEBSERVICE ORDER BY ID"
    ),
    "recipient": (
        f"SELECT ID, NAME, SYSID, REPLYTO FROM {SCHEMA}.RECIPIENT ORDER BY ID"
    ),
    "recipient_dirlocations": (
        f"SELECT RECIPIENTS_ID, DIRECTORYLOCATIONS_ID "
        f"FROM {SCHEMA}.RECIPIENT_DIRLOCATIONS "
        f"ORDER BY RECIPIENTS_ID, DIRECTORYLOCATIONS_ID"
    ),
    "recipient_emailgroup": (
        f"SELECT DISTINCT RECIPIENTS_ID, EMAILGROUPS_ID "
        f"FROM {SCHEMA}.RECIPIENT_EMAILGROUP "
        f"ORDER BY RECIPIENTS_ID, EMAILGROUPS_ID"
    ),
    "recipient_webservices": (
        f"SELECT RECIPIENTS_ID, WEBSERVICE_ID "
        f"FROM {SCHEMA}.RECIPIENT_WEBSERVICES "
        f"ORDER BY RECIPIENTS_ID, WEBSERVICE_ID"
    ),
    # ── Web service status ───────────────────────────────────────────────
    "webservicestatusmessage": (
        f"SELECT ID, REFNO, CREATEDDATE, LASTUPDATEDATE, FILENAME, "
        f"STATUSTYPE, STATUSMESSAGE, TYPE "
        f"FROM {SCHEMA}.WEBSERVICESTATUSMESSAGE ORDER BY ID"
    ),
    # ── FACS messaging ───────────────────────────────────────────────────
    "facsmessageentity": (
        f"SELECT ID, MESSAGEID, REPLYENDPOINT, REQUESTMESSAGE, RESPONSEMESSAGE, "
        f"STATUS, FACSKEY, TRANSFORMFROMTRANSACTIONCODE, TRANSFORMTOTRANSACTIONCODE "
        f"FROM {SCHEMA}.FACSMESSAGEENTITY ORDER BY ID"
    ),
    "facsmessagehistoryitementity": (
        f"SELECT ID, CREATED, DESCRIPTION, STATUS, FACSMESSAGE_ID "
        f"FROM {SCHEMA}.FACSMESSAGEHISTORYITEMENTITY ORDER BY ID"
    ),
    # ── GICSA async ──────────────────────────────────────────────────────
    "gicsawasyncmessage": (
        f"SELECT ID, ALSOSCAN, CALLBACKURL, CALLERREFERENCE, CALLERREFERENCETYPE, "
        f"DISPATCHTYPE, ERRORMESSAGE, GICSAWCORRELATIONID, LASTUPDATEDATE, "
        f"REQUESTMESSAGE, RESPONSEMESSAGE, RESPONSERECEIVEDDATE, STATUS, "
        f"STATUSMESSAGE, PARENTCRDA, PARENTREC "
        f"FROM {SCHEMA}.GICSAWASYNCMESSAGE ORDER BY ID"
    ),
    # ── Incoming files ───────────────────────────────────────────────────
    "incomingfile": (
        f"SELECT ID, FILE_TYPE, FILE_DATE, FILE_NAME, DOCUMENT_ID, FILE_HASH, STATUS "
        f"FROM {SCHEMA}.INCOMINGFILE ORDER BY ID"
    ),
    # ── JMS messaging ────────────────────────────────────────────────────
    "jmssystemadaptermessage": (
        f"SELECT ID, CALLERREFERENCETYPE, MESSAGEID, REMOTEREFERENCE, "
        f"REPLYENDPOINT, REQUESTMESSAGE, RESPONSEMESSAGE, STATUS, TYPE "
        f"FROM {SCHEMA}.JMSSYSTEMADAPTERMESSAGE ORDER BY ID"
    ),
    "jmssystemadaptermessagestatus": (
        f"SELECT ID, CREATED, DESCRIPTION, STATUS, MESSAGE_ID "
        f"FROM {SCHEMA}.JMSSYSTEMADAPTERMESSAGESTATUS ORDER BY ID"
    ),
    # ── Outbound email (XML column) ──────────────────────────────────────
    "outboundemail": (
        f"SELECT ID, CREATED_AT, DISPATCHED_AT, "
        f"XMLSERIALIZE(XMLCONTENT AS VARCHAR(32000)) AS XMLCONTENT "
        f"FROM {SCHEMA}.OUTBOUNDEMAIL ORDER BY ID"
    ),
    # ── Remote interaction log (CLOB columns) ────────────────────────────
    "remote_interaction_log": (
        f"SELECT ID, INTERACTION_MEDIUM, SERVICE_URI, SERVICE_METHOD, "
        f"USER_PRINCIPAL, STARTED_AT, COMPLETED_AT, DURATION_MILLISECONDS, "
        f"INPUT, OUTPUT, ERROR, TIMESLOT, BATCH_ID "
        f"FROM {SCHEMA}.REMOTE_INTERACTION_LOG ORDER BY ID"
    ),
    # ── SMS log ──────────────────────────────────────────────────────────
    "smsmessagelog": (
        f"SELECT ID, CHANNEL, MESSAGEID, IDNUMBER, MESSAGE, SENDERNUMBER, "
        f"RECIPIENTNUMBER, MESSAGERECEIVEDTIMESTAMP, MESSAGESENTTIMESTAMP "
        f"FROM {SCHEMA}.SMSMESSAGELOG ORDER BY ID"
    ),
    # ── VOC message log (CLOB + XML columns) ─────────────────────────────
    "voc_message_log": (
        f"SELECT ID, REQUEST_ID, PROCESSED_AT, VOC_EVENT, ACTIVITIES, "
        f"XMLSERIALIZE(VOC_MESSAGE AS VARCHAR(32000)) AS VOC_MESSAGE, "
        f"REASON_NOT_DISPATCHED, EXCEPTION "
        f"FROM {SCHEMA}.VOC_MESSAGE_LOG ORDER BY ID"
    ),
}

# ── PostgreSQL-compatible queries (lowercase schema + table names) ────────

PG_SCHEMA = "wealthadapter"

PG_TABLE_QUERIES: dict[str, str] = {
    # ── Independent reference tables ─────────────────────────────────────
    "userrole": f"SELECT id, role FROM {PG_SCHEMA}.userrole ORDER BY id",
    "wealthaccessright": f"SELECT id, name FROM {PG_SCHEMA}.wealthaccessright ORDER BY id",
    "role_accessright": (
        f"SELECT role_id, accessright_id FROM {PG_SCHEMA}.role_accessright "
        f"ORDER BY role_id, accessright_id"
    ),
    "adapterconfig": (
        f"SELECT id, key, value FROM {PG_SCHEMA}.adapterconfig "
        f"WHERE key NOT LIKE '%password%' "
        f"AND key NOT LIKE '%secret%' "
        f"AND key NOT LIKE '%pwd%' "
        f"ORDER BY id"
    ),
    "userdetail": (
        f"SELECT id, datepasswordchanged, email, personnelnumber, surname, "
        f"telephonenumber, uid, username, domain_user "
        f"FROM {PG_SCHEMA}.userdetail ORDER BY id"
    ),
    "systemstatuslog": (
        f"SELECT id, action, hostname, status, statusmessage, systemname, timestamp "
        f"FROM {PG_SCHEMA}.systemstatuslog ORDER BY id"
    ),
    # ── Email / recipient hierarchy ──────────────────────────────────────
    "emailgroup": f"SELECT id, name FROM {PG_SCHEMA}.emailgroup ORDER BY id",
    "emailaddress": (
        f"SELECT id, description, emailaddress, emailgroup_fk "
        f"FROM {PG_SCHEMA}.emailaddress ORDER BY id"
    ),
    "emailgroup_emailaddress": (
        f"SELECT emailgroup_id, emailaddresses_id "
        f"FROM {PG_SCHEMA}.emailgroup_emailaddress "
        f"ORDER BY emailgroup_id, emailaddresses_id"
    ),
    "directorylocation": (
        f"SELECT id, dir, filemask FROM {PG_SCHEMA}.directorylocation ORDER BY id"
    ),
    "webservice": (
        f"SELECT id, type, username, companycode "
        f"FROM {PG_SCHEMA}.webservice ORDER BY id"
    ),
    "recipient": (
        f"SELECT id, name, sysid, replyto FROM {PG_SCHEMA}.recipient ORDER BY id"
    ),
    "recipient_dirlocations": (
        f"SELECT recipients_id, directorylocations_id "
        f"FROM {PG_SCHEMA}.recipient_dirlocations "
        f"ORDER BY recipients_id, directorylocations_id"
    ),
    "recipient_emailgroup": (
        f"SELECT DISTINCT recipients_id, emailgroups_id "
        f"FROM {PG_SCHEMA}.recipient_emailgroup "
        f"ORDER BY recipients_id, emailgroups_id"
    ),
    "recipient_webservices": (
        f"SELECT recipients_id, webservice_id "
        f"FROM {PG_SCHEMA}.recipient_webservices "
        f"ORDER BY recipients_id, webservice_id"
    ),
    # ── Web service status ───────────────────────────────────────────────
    "webservicestatusmessage": (
        f"SELECT id, refno, createddate, lastupdatedate, filename, "
        f"statustype, statusmessage, type "
        f"FROM {PG_SCHEMA}.webservicestatusmessage ORDER BY id"
    ),
    # ── FACS messaging (exclude latest_history_date — PG-only column) ────
    "facsmessageentity": (
        f"SELECT id, messageid, replyendpoint, requestmessage, responsemessage, "
        f"status, facskey, transformfromtransactioncode, transformtotransactioncode "
        f"FROM {PG_SCHEMA}.facsmessageentity ORDER BY id"
    ),
    "facsmessagehistoryitementity": (
        f"SELECT id, created, description, status, facsmessage_id "
        f"FROM {PG_SCHEMA}.facsmessagehistoryitementity ORDER BY id"
    ),
    # ── GICSA async ──────────────────────────────────────────────────────
    "gicsawasyncmessage": (
        f"SELECT id, alsoscan, callbackurl, callerreference, callerreferencetype, "
        f"dispatchtype, errormessage, gicsawcorrelationid, lastupdatedate, "
        f"requestmessage, responsemessage, responsereceiveddate, status, "
        f"statusmessage, parentcrda, parentrec "
        f"FROM {PG_SCHEMA}.gicsawasyncmessage ORDER BY id"
    ),
    # ── Incoming files ───────────────────────────────────────────────────
    "incomingfile": (
        f"SELECT id, file_type, file_date, file_name, document_id, file_hash, status "
        f"FROM {PG_SCHEMA}.incomingfile ORDER BY id"
    ),
    # ── JMS messaging (exclude latest_status_date — PG-only column) ──────
    "jmssystemadaptermessage": (
        f"SELECT id, callerreferencetype, messageid, remotereference, "
        f"replyendpoint, requestmessage, responsemessage, status, type "
        f"FROM {PG_SCHEMA}.jmssystemadaptermessage ORDER BY id"
    ),
    "jmssystemadaptermessagestatus": (
        f"SELECT id, created, description, status, message_id "
        f"FROM {PG_SCHEMA}.jmssystemadaptermessagestatus ORDER BY id"
    ),
    # ── Outbound email (XML → text cast) ─────────────────────────────────
    "outboundemail": (
        f"SELECT id, created_at, dispatched_at, "
        f"xmlcontent::text AS xmlcontent "
        f"FROM {PG_SCHEMA}.outboundemail ORDER BY id"
    ),
    # ── Remote interaction log (TEXT columns) ────────────────────────────
    "remote_interaction_log": (
        f"SELECT id, interaction_medium, service_uri, service_method, "
        f"user_principal, started_at, completed_at, duration_milliseconds, "
        f"input, output, error, timeslot, batch_id "
        f"FROM {PG_SCHEMA}.remote_interaction_log ORDER BY id"
    ),
    # ── SMS log ──────────────────────────────────────────────────────────
    "smsmessagelog": (
        f"SELECT id, channel, messageid, idnumber, message, sendernumber, "
        f"recipientnumber, messagereceivedtimestamp, messagesenttimestamp "
        f"FROM {PG_SCHEMA}.smsmessagelog ORDER BY id"
    ),
    # ── VOC message log (TEXT + XML → text cast) ─────────────────────────
    "voc_message_log": (
        f"SELECT id, request_id, processed_at, voc_event, activities, "
        f"voc_message::text AS voc_message, "
        f"reason_not_dispatched, exception "
        f"FROM {PG_SCHEMA}.voc_message_log ORDER BY id"
    ),
}


# ── DSN format converters ────────────────────────────────────────────────


def _parse_db2_url_to_dsn(url: str) -> str:
    """Convert a URL-style Db2 DSN to ibm_db format.

    Input format:  user:password@host:port/database
    Output format: DATABASE=...;HOSTNAME=...;PORT=...;PROTOCOL=TCPIP;UID=...;PWD=...
    """
    pattern = r"^(?:db2://)?([^:]+):([^@]+)@([^:]+):(\d+)/(.+)$"
    match = re.match(pattern, url.strip())
    if not match:
        raise ValueError(
            f"Cannot parse Db2 URL: {url!r}. "
            f"Expected format: user:password@host:port/database"
        )
    uid, pwd, hostname, port, database = match.groups()
    return (
        f"DATABASE={database};"
        f"HOSTNAME={hostname};"
        f"PORT={port};"
        f"PROTOCOL=TCPIP;"
        f"UID={uid};"
        f"PWD={pwd}"
    )


@dataclass
class FetchResult:
    """Holds fetched data with metadata."""

    table_name: str
    rows: list[dict[str, Any]]
    row_count: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ── Direct DB2 fetcher (fallback when not in MCP agent context) ──────────


def _fetch_via_ibm_db(dsn: str) -> dict[str, FetchResult]:
    """Fetch all tables using a direct ibm_db connection."""
    import ibm_db  # noqa: F811

    results: dict[str, FetchResult] = {}

    try:
        conn = ibm_db.connect(dsn, "", "")
        logger.info("Connected to Db2 via ibm_db for data fetch.")
    except Exception as exc:
        logger.error("Failed to connect to Db2: %s", exc)
        for table_name in TABLE_QUERIES:
            results[table_name] = FetchResult(
                table_name=table_name,
                rows=[],
                row_count=0,
                error=f"Connection failed: {exc}",
            )
        return results

    try:
        for table_name, query in TABLE_QUERIES.items():
            try:
                logger.info("Fetching %s …", table_name)
                stmt = ibm_db.exec_immediate(conn, query)
                rows: list[dict[str, Any]] = []
                row = ibm_db.fetch_assoc(stmt)
                while row:
                    # ibm_db returns uppercase keys; normalise to lowercase
                    rows.append({k.lower(): v for k, v in row.items()})
                    row = ibm_db.fetch_assoc(stmt)

                results[table_name] = FetchResult(
                    table_name=table_name,
                    rows=rows,
                    row_count=len(rows),
                )
                logger.info("  %s: %d rows fetched", table_name, len(rows))

            except Exception as exc:
                logger.error("  %s: fetch failed — %s", table_name, exc)
                results[table_name] = FetchResult(
                    table_name=table_name,
                    rows=[],
                    row_count=0,
                    error=str(exc),
                )
    finally:
        ibm_db.close(conn)

    return results


# ── Direct PostgreSQL fetcher ────────────────────────────────────────────


def _fetch_via_psycopg2(dsn: str) -> dict[str, FetchResult]:
    """Fetch all tables using a direct psycopg2 connection to a PostgreSQL source."""
    import psycopg2
    import psycopg2.extras

    results: dict[str, FetchResult] = {}

    try:
        conn = psycopg2.connect(dsn)
        logger.info("Connected to PostgreSQL via psycopg2 for data fetch.")
    except Exception as exc:
        logger.error("Failed to connect to PostgreSQL source: %s", exc)
        for table_name in PG_TABLE_QUERIES:
            results[table_name] = FetchResult(
                table_name=table_name,
                rows=[],
                row_count=0,
                error=f"Connection failed: {exc}",
            )
        return results

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for table_name, query in PG_TABLE_QUERIES.items():
            try:
                logger.info("Fetching %s …", table_name)
                cur.execute(query)
                rows: list[dict[str, Any]] = [dict(row) for row in cur.fetchall()]

                results[table_name] = FetchResult(
                    table_name=table_name,
                    rows=rows,
                    row_count=len(rows),
                )
                logger.info("  %s: %d rows fetched", table_name, len(rows))

            except Exception as exc:
                logger.error("  %s: fetch failed — %s", table_name, exc)
                conn.rollback()
                results[table_name] = FetchResult(
                    table_name=table_name,
                    rows=[],
                    row_count=0,
                    error=str(exc),
                )
        cur.close()
    finally:
        conn.close()

    return results


# ── Static/embedded data (for CI environments without DB2 access) ────────


def _fetch_embedded_seed_data() -> dict[str, FetchResult]:
    """Return a minimal seed dataset for offline testing.

    This data mirrors the real schemas and contains representative rows
    so that schema creation and insert logic can be validated even when
    the MCP server or Db2 is unreachable.
    """
    logger.info("Using embedded seed data (no MCP / Db2 connection available).")

    data: dict[str, list[dict[str, Any]]] = {
        "userrole": [
            {"id": 1, "role": "WealthLineManagerRole"},
            {"id": 2, "role": "WealthNotificationRole"},
            {"id": 3, "role": "WealthSystemUserRole"},
            {"id": 4, "role": "WealthBOMRole"},
            {"id": 5, "role": "WealthQARole"},
            {"id": 6, "role": "WealthAdminRole"},
            {"id": 7, "role": "WealthBORole"},
            {"id": 8, "role": "WealthNotificationAdminRole"},
            {"id": 9, "role": "WealthFundAdminRole"},
        ],
        "wealthaccessright": [
            {"id": i, "name": name}
            for i, name in enumerate(
                [
                    "home", "myInBox", "contractAdmin", "financialAdmin",
                    "fundAdministration", "systemAdministration", "information",
                    "logout", "alertAdministration", "task",
                ],
                start=1,
            )
        ],
        "role_accessright": [
            {"role_id": 1, "accessright_id": 4},
            {"role_id": 1, "accessright_id": 6},
            {"role_id": 2, "accessright_id": 2},
            {"role_id": 2, "accessright_id": 9},
            {"role_id": 3, "accessright_id": 1},
            {"role_id": 3, "accessright_id": 7},
            {"role_id": 6, "accessright_id": 1},
            {"role_id": 6, "accessright_id": 6},
        ],
        "adapterconfig": [
            {"id": 1, "key": "test.setting.enabled", "value": "true"},
            {"id": 2, "key": "test.setting.timeout", "value": "30000"},
            {"id": 3, "key": "test.setting.url", "value": "https://example.com/api"},
        ],
        "userdetail": [],
        "systemstatuslog": [],
        "emailgroup": [
            {"id": 1, "name": "AdvantagePricing"},
            {"id": 2, "name": "AdvantageCashFlow"},
            {"id": 3, "name": "FNNCashFlow"},
        ],
        "emailaddress": [],
        "emailgroup_emailaddress": [],
        "directorylocation": [
            {"id": 1, "dir": "/RMBUT-FNB", "filemask": None},
            {"id": 2, "dir": "/RMBUT-RMBAM", "filemask": None},
            {"id": 3, "dir": "/RMBUT-Advantage", "filemask": None},
            {"id": 4, "dir": "/RMB", "filemask": None},
        ],
        "webservice": [],
        "recipient": [
            {"id": 1, "name": "AdvantagePricing", "sysid": "AdvantagePricing", "replyto": None},
            {"id": 2, "name": "AdvantageCashFlow", "sysid": "AdvantageCashFlow", "replyto": None},
        ],
        "recipient_dirlocations": [],
        "recipient_emailgroup": [],
        "recipient_webservices": [],
        "webservicestatusmessage": [],
        "facsmessageentity": [],
        "facsmessagehistoryitementity": [],
        "gicsawasyncmessage": [],
        "incomingfile": [],
        "jmssystemadaptermessage": [],
        "jmssystemadaptermessagestatus": [],
        "outboundemail": [],
        "remote_interaction_log": [],
        "smsmessagelog": [],
        "voc_message_log": [],
    }

    return {
        name: FetchResult(table_name=name, rows=rows, row_count=len(rows))
        for name, rows in data.items()
    }


# ── Public API ───────────────────────────────────────────────────────────


def fetch_all_tables(
    *,
    use_embedded: bool = False,
    source: str = "auto",
    db2_dsn: str | None = None,
) -> dict[str, FetchResult]:
    """Fetch all target tables and return normalised row dicts.

    Resolution order (when source="auto"):
      1. If ``use_embedded=True`` → return embedded seed data (no I/O).
      2. If ``db2_dsn`` is provided → connect via ibm_db directly.
      3. Try env var ``DB2_WEALTH_TST_DSN`` (ibm_db format) → ibm_db.
      4. Try env var ``WA_TARGET_DSN`` (URL format) → parse → ibm_db.
      5. Try env var ``WA_SOURCE_DSN`` (PostgreSQL URL) → psycopg2.
      6. Fall back to embedded seed data.

    Forced source modes:
      - source="db2":      Only try Db2 sources (steps 2–4).
      - source="pg":       Only try PG source (step 5).
      - source="embedded": Same as use_embedded=True.

    Parameters
    ----------
    use_embedded:
        Force use of built-in seed data (for CI without Db2).
    source:
        Source selection: "auto", "db2", "pg", or "embedded".
    db2_dsn:
        Explicit ibm_db DSN string.  Overrides env var.

    Returns
    -------
    dict mapping lowercase table name → FetchResult.
    """
    if use_embedded or source == "embedded":
        return _fetch_embedded_seed_data()

    # Load .env so DSN variables are available without manual export.
    # Called here (not at module level) to avoid side effects on import.
    load_dotenv()

    # ── Db2 sources ──────────────────────────────────────────────────────
    if source in ("auto", "db2"):
        # Priority 1: explicit DSN argument
        dsn = db2_dsn or os.getenv("DB2_WEALTH_TST_DSN")
        if dsn:
            try:
                logger.info("Fetching from Db2 via DB2_WEALTH_TST_DSN / --db2-dsn …")
                return _fetch_via_ibm_db(dsn)
            except Exception as exc:
                logger.warning("ibm_db fetch failed (%s).", exc)
                if source == "db2":
                    logger.error("Forced source=db2 but connection failed.")
                    return _fetch_embedded_seed_data()

        # Priority 2: WA_TARGET_DSN (URL format → parsed to ibm_db DSN)
        wa_target = os.getenv("WA_TARGET_DSN")
        if wa_target:
            try:
                parsed_dsn = _parse_db2_url_to_dsn(wa_target)
                logger.info("Fetching from Db2 via WA_TARGET_DSN …")
                return _fetch_via_ibm_db(parsed_dsn)
            except Exception as exc:
                logger.warning("WA_TARGET_DSN fetch failed (%s).", exc)
                if source == "db2":
                    logger.error("Forced source=db2 but WA_TARGET_DSN failed.")
                    return _fetch_embedded_seed_data()

        if source == "db2":
            logger.error(
                "Forced source=db2 but no Db2 DSN available "
                "(set DB2_WEALTH_TST_DSN or WA_TARGET_DSN)."
            )
            return _fetch_embedded_seed_data()

    # ── PostgreSQL source ────────────────────────────────────────────────
    if source in ("auto", "pg"):
        wa_source = os.getenv("WA_SOURCE_DSN")
        if wa_source:
            try:
                logger.info("Fetching from PostgreSQL via WA_SOURCE_DSN …")
                return _fetch_via_psycopg2(wa_source)
            except Exception as exc:
                logger.warning("psycopg2 fetch failed (%s).", exc)
                if source == "pg":
                    logger.error("Forced source=pg but WA_SOURCE_DSN failed.")
                    return _fetch_embedded_seed_data()

        if source == "pg":
            logger.error(
                "Forced source=pg but WA_SOURCE_DSN not set in environment."
            )
            return _fetch_embedded_seed_data()

    # ── Fallback ─────────────────────────────────────────────────────────
    logger.warning(
        "No source DSN available (DB2_WEALTH_TST_DSN, WA_TARGET_DSN, "
        "WA_SOURCE_DSN); using embedded seed data."
    )
    return _fetch_embedded_seed_data()


def validate_fetch_results(results: dict[str, FetchResult]) -> bool:
    """Return True if all tables were fetched successfully."""
    all_ok = True
    for name, result in results.items():
        if not result.ok:
            logger.error("Table %s fetch failed: %s", name, result.error)
            all_ok = False
        elif result.row_count == 0:
            logger.warning("Table %s returned 0 rows.", name)
    return all_ok
