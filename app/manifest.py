"""Reload-manifest computation.

After late readings arrive or a replacement sheet is corrected, the warehouse
operator needs to know which *account-day* UTC usage partitions must be
reloaded. A manifest takes the most recent generation as the baseline,
recomputes the interval results from the currently committed inputs, and
compares the two outcomes bucketed by account-day:

  * OK rows are bucketed by their attributed UTC day (the later reading's day);
  * REVIEW rows are bucketed by the later reading's UTC day as well, so a row
    whose status flips between OK and REVIEW stays in the same bucket.

Each changed bucket records the change type(s) and the before/after evidence
(detail rows, consumption totals, review reasons and source reading ids).
A late reading that splits an old interval touches the days on both sides, so
both buckets show up; an identical retransmission changes nothing and produces
no entry.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any, Optional

from .config import (
    CHANGE_ADDED,
    CHANGE_CONSUMPTION,
    CHANGE_DETAIL,
    CHANGE_REMOVED,
    CHANGE_REVIEW_REASON,
    CHANGE_SOURCE,
)
from .ingestion import now_utc_iso

CHANGE_LABELS = {
    CHANGE_ADDED: "账户日新增（基线无此分区）",
    CHANGE_REMOVED: "账户日消失（当前已无此分区）",
    CHANGE_CONSUMPTION: "用量变化",
    CHANGE_SOURCE: "来源读数变化",
    CHANGE_REVIEW_REASON: "待核原因变化",
    CHANGE_DETAIL: "明细变化",
}


class ManifestError(Exception):
    """Building the manifest failed; no new manifest is published."""


def change_labels(codes: str | None) -> str:
    if not codes:
        return ""
    return "；".join(CHANGE_LABELS.get(c, c) for c in codes.split(","))


def _parse_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(part) for part in raw.split(",") if part != ""]


def _num(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _partition_day(row: dict) -> str:
    """OK rows use the attributed UTC day; REVIEW rows use the later reading's day."""
    if row["status"] == "OK":
        return row["attributed_day"]
    return (row["interval_end"] or "")[:10]


def _signature(row: dict) -> tuple:
    """Hashable identity of one detail row; equal multisets mean an unchanged bucket."""
    return (
        row["meter"],
        row["status"],
        row["review_reason"] or "",
        row["interval_start"] or "",
        row["interval_end"] or "",
        _num(row["start_reading"]),
        _num(row["end_reading"]),
        _num(row["consumption_m3"]),
        tuple(row["source_ids"]),
    )


def _evidence(row: dict) -> dict:
    return {
        "meter": row["meter"],
        "status": row["status"],
        "review_reason": row["review_reason"],
        "interval_start_utc": row["interval_start"],
        "interval_end_utc": row["interval_end"],
        "start_reading_m3": _num(row["start_reading"]),
        "end_reading_m3": _num(row["end_reading"]),
        "consumption_m3": _num(row["consumption_m3"]),
        "attributed_utc_day": row["attributed_day"],
        "partition_utc_day": _partition_day(row),
        "source_reading_ids": list(row["source_ids"]),
    }


def _baseline_adapters(rows: list[sqlite3.Row]) -> list[dict]:
    return [
        {
            "account": r["account"],
            "meter": r["meter"],
            "interval_start": r["interval_start"],
            "interval_end": r["interval_end"],
            "start_reading": r["start_reading"],
            "end_reading": r["end_reading"],
            "consumption_m3": r["consumption_m3"],
            "attributed_day": r["attributed_day"],
            "status": r["status"],
            "review_reason": r["review_reason"],
            "source_ids": _parse_ids(r["source_reading_ids"]),
        }
        for r in rows
    ]


def _current_adapters(results: list) -> list[dict]:
    return [
        {
            "account": x.account,
            "meter": x.meter,
            "interval_start": x.interval_start,
            "interval_end": x.interval_end,
            "start_reading": x.start_reading,
            "end_reading": x.end_reading,
            "consumption_m3": x.consumption_m3,
            "attributed_day": x.attributed_day,
            "status": x.status,
            "review_reason": x.review_reason,
            "source_ids": list(x.source_reading_ids),
        }
        for x in results
    ]


def _bucketize(rows: list[dict]) -> dict[tuple[str, str], dict]:
    buckets: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["account"], _partition_day(row))
        bucket = buckets.setdefault(
            key,
            {
                "rows": [],
                "signatures": Counter(),
                "source_ids": set(),
                "reasons": set(),
                "consumption_parts": [],
            },
        )
        bucket["rows"].append(row)
        bucket["signatures"][_signature(row)] += 1
        bucket["source_ids"].update(row["source_ids"])
        if row["review_reason"]:
            bucket["reasons"].add(row["review_reason"])
        if row["consumption_m3"] is not None:
            bucket["consumption_parts"].append(float(row["consumption_m3"]))
    for bucket in buckets.values():
        bucket["consumption"] = (
            round(sum(bucket["consumption_parts"]), 6)
            if bucket["consumption_parts"]
            else None
        )
        bucket["source_ids"] = sorted(bucket["source_ids"])
        bucket["reasons"] = sorted(bucket["reasons"])
    return buckets


def _classify(before: dict, after: dict) -> list[str]:
    types: list[str] = []
    if before["consumption"] != after["consumption"]:
        types.append(CHANGE_CONSUMPTION)
    if before["source_ids"] != after["source_ids"]:
        types.append(CHANGE_SOURCE)
    if before["reasons"] != after["reasons"]:
        types.append(CHANGE_REVIEW_REASON)
    if not types:
        # Aggregates coincide but the detail multiset still differs (e.g. a
        # meter-level restructure that nets to the same total).
        types.append(CHANGE_DETAIL)
    return types


def diff_account_days(baseline_rows: list[dict],
                      current_rows: list[dict]) -> list[dict]:
    """Compare two result sets bucketed by (account, UTC day)."""
    before = _bucketize(baseline_rows)
    after = _bucketize(current_rows)
    changes: list[dict] = []
    for key in sorted(set(before) | set(after)):
        account, day = key
        old = before.get(key)
        new = after.get(key)
        if old is None:
            types = [CHANGE_ADDED]
        elif new is None:
            types = [CHANGE_REMOVED]
        elif old["signatures"] == new["signatures"]:
            # Same detail multiset: identical retransmissions land here and
            # never create a spurious partition entry.
            continue
        else:
            types = _classify(old, new)

        old_evidence = [_evidence(r) for r in old["rows"]] if old else []
        new_evidence = [_evidence(r) for r in new["rows"]] if new else []
        changes.append(
            {
                "account": account,
                "partition_day": day,
                "change_types": types,
                "baseline_consumption": old["consumption"] if old else None,
                "current_consumption": new["consumption"] if new else None,
                "baseline_reasons": old["reasons"] if old else [],
                "current_reasons": new["reasons"] if new else [],
                "baseline_source_ids": old["source_ids"] if old else [],
                "current_source_ids": new["source_ids"] if new else [],
                "baseline_details": old_evidence,
                "current_details": new_evidence,
            }
        )
    return changes


def expire_active_manifests(conn: sqlite3.Connection,
                            detail: str | None = None) -> None:
    """Mark every active manifest expired (new input, new generation, failure)."""
    conn.execute(
        """UPDATE reload_manifests
              SET status='expired',
                  status_detail=COALESCE(?, status_detail),
                  superseded_at=COALESCE(superseded_at, ?)
            WHERE status='active'""",
        (detail, now_utc_iso()),
    )


def build_reload_manifest(conn: sqlite3.Connection) -> dict:
    """Build a manifest against the latest generation from committed inputs.

    Runs under one serialised transaction (``BEGIN IMMEDIATE``) so an upload
    interleaved with the build can neither be half-included nor publish an
    expired "current" list. On failure the transaction is rolled back and no
    manifest row is created.
    """
    # Imported lazily to avoid a module-level import cycle (ingestion/compute
    # call back into this module at runtime).
    from .compute import compute_intervals

    conn.execute("BEGIN IMMEDIATE")
    try:
        baseline = conn.execute(
            "SELECT * FROM generations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if baseline is None:
            raise ManifestError("尚无任何结果代际，无法以最近一代为基线生成核查清单")

        reading_batch_ids = [
            str(r["id"])
            for r in conn.execute(
                "SELECT id FROM upload_batches "
                "WHERE batch_type='reading' AND status='committed' ORDER BY id"
            )
        ]
        repl_row = conn.execute(
            "SELECT id FROM upload_batches "
            "WHERE batch_type='replacement' AND status='committed' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        repl_batch_id = repl_row["id"] if repl_row else None

        baseline_rows = _baseline_adapters(
            conn.execute(
                "SELECT * FROM result_rows WHERE generation_id=? ORDER BY id",
                (baseline["id"],),
            ).fetchall()
        )
        # Recomputed inside the same lock: a consistent snapshot of committed inputs.
        current_rows = _current_adapters(compute_intervals(conn))
        changes = diff_account_days(baseline_rows, current_rows)

        cur = conn.execute(
            """INSERT INTO reload_manifests
                   (status, status_detail, baseline_generation_id,
                    reading_batch_ids, replacement_batch_id, change_count,
                    created_at, superseded_at)
               VALUES ('active', NULL, ?, ?, ?, ?, ?, NULL)""",
            (
                baseline["id"],
                ",".join(reading_batch_ids),
                repl_batch_id,
                len(changes),
                now_utc_iso(),
            ),
        )
        manifest_id = int(cur.lastrowid)
        conn.executemany(
            """INSERT INTO manifest_changes
                   (manifest_id, account, partition_day, change_types,
                    baseline_consumption_m3, current_consumption_m3,
                    baseline_review_reasons, current_review_reasons,
                    baseline_details, current_details,
                    baseline_source_ids, current_source_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    manifest_id,
                    c["account"],
                    c["partition_day"],
                    ",".join(c["change_types"]),
                    c["baseline_consumption"],
                    c["current_consumption"],
                    ",".join(c["baseline_reasons"]),
                    ",".join(c["current_reasons"]),
                    json.dumps(c["baseline_details"], ensure_ascii=False),
                    json.dumps(c["current_details"], ensure_ascii=False),
                    ",".join(str(i) for i in c["baseline_source_ids"]),
                    ",".join(str(i) for i in c["current_source_ids"]),
                )
                for c in changes
            ],
        )
        # Only one active manifest at a time; older ones are superseded only
        # after the new one is fully built.
        conn.execute(
            """UPDATE reload_manifests
                  SET status='expired', superseded_at=?
                WHERE status='active' AND id<>?""",
            (now_utc_iso(), manifest_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "manifest_id": manifest_id,
        "baseline_generation_id": baseline["id"],
        "change_count": len(changes),
        "reading_batch_ids": reading_batch_ids,
        "replacement_batch_id": repl_batch_id,
    }
