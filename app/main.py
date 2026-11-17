"""FastAPI application: uploads, generation, review and download."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .compute import GenerationError, generate_generation, reason_label
from .database import get_connection, init_db
from .ingestion import BatchRejected, ingest_readings, ingest_replacements
from .manifest import (
    ManifestError,
    build_reload_manifest,
    change_labels,
    expire_active_manifests,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="供水账户用量明细服务", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"


class Message(BaseModel):
    ok: bool
    message: str
    detail: dict | None = None


def _text_of(file: UploadFile) -> str:
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400,
                                detail="文件必须为 UTF-8（或 GB18030）编码的 CSV") from exc


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/uploads/readings", response_model=Message)
def upload_readings(file: UploadFile = File(...)):
    text = _text_of(file)
    conn = get_connection()
    try:
        try:
            detail = ingest_readings(conn, file.filename or "readings.csv", text)
        except BatchRejected as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        return Message(ok=True,
                       message=f"读数上传成功：新增 {detail['inserted']} 条，"
                               f"同表同时同值重传跳过 {detail['duplicates_skipped']} 条",
                       detail=detail)
    finally:
        conn.close()


@app.post("/api/uploads/replacements", response_model=Message)
def upload_replacements(file: UploadFile = File(...)):
    text = _text_of(file)
    conn = get_connection()
    try:
        try:
            detail = ingest_replacements(conn, file.filename or "replacements.csv", text)
        except BatchRejected as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        return Message(ok=True,
                       message=f"换表整版上传成功：载入 {detail['inserted']} 户，旧版换表数据已被替换",
                       detail=detail)
    finally:
        conn.close()


@app.post("/api/generations", response_model=Message)
def create_generation():
    conn = get_connection()
    try:
        try:
            detail = generate_generation(conn)
        except GenerationError as exc:
            # Business-level failure: keep the previous result, mark it failed/expired.
            _mark_active_failed(conn, str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            _mark_active_failed(conn, f"生成失败: {exc}")
            raise HTTPException(status_code=500, detail=f"生成失败: {exc}") from exc
        return Message(
            ok=True,
            message=f"第 {detail['generation_id']} 代结果已发布为现役："
                    f"共 {detail['total_rows']} 行（正常 {detail['ok_rows']}，待核 {detail['review_rows']}）",
            detail=detail,
        )
    finally:
        conn.close()


def _mark_active_failed(conn: sqlite3.Connection, detail: str) -> None:
    """A failed generation publishes nothing new. The previous result stays
    downloadable but is marked expired (with the failure reason recorded),
    so no row claims to be the current live result. Any active reload manifest
    is likewise expired but retained for traceability."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """UPDATE generations
                  SET status='expired',
                      status_detail=?,
                      superseded_at=COALESCE(superseded_at, ?)
                WHERE status='active'""",
            (f"后续生成失败，旧结果保留：{detail}", _now()),
        )
        expire_active_manifests(
            conn, f"后续生成失败，旧核查清单保留并标过期：{detail}"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _mark_active_manifest_failed(conn: sqlite3.Connection, detail: str) -> None:
    """A failed manifest build publishes nothing new; the previous manifest is
    retained for traceability but marked expired, so no list claims to be the
    current one."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        expire_active_manifests(conn, f"核查清单生成失败，旧清单保留并标过期：{detail}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _now() -> str:
    from .ingestion import now_utc_iso
    return now_utc_iso()


@app.get("/api/generations")
def list_generations():
    conn = get_connection()
    try:
        gens = [dict(r) for r in conn.execute(
            "SELECT * FROM generations ORDER BY id DESC")]
        for g in gens:
            g["row_count"] = conn.execute(
                "SELECT COUNT(*) FROM result_rows WHERE generation_id=?",
                (g["id"],)).fetchone()[0]
            g["review_count"] = conn.execute(
                "SELECT COUNT(*) FROM result_rows WHERE generation_id=? AND status='REVIEW'",
                (g["id"],)).fetchone()[0]
        return {"generations": gens}
    finally:
        conn.close()


@app.get("/api/batches")
def list_batches():
    conn = get_connection()
    try:
        return {
            "batches": [
                dict(r) for r in conn.execute(
                    "SELECT * FROM upload_batches ORDER BY id DESC")
            ]
        }
    finally:
        conn.close()


CSV_COLUMNS = [
    "generation_id", "account", "meter",
    "interval_start_utc", "interval_end_utc",
    "start_reading_m3", "end_reading_m3", "consumption_m3",
    "attributed_utc_day", "status", "review_reason", "review_reason_detail",
    "source_reading_ids",
]


def _generation_or_404(conn: sqlite3.Connection, generation_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM generations WHERE id=?",
                       (generation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"不存在第 {generation_id} 代结果")
    return row


@app.get("/api/generations/latest/download")
def download_latest():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM generations ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="尚未生成任何结果")
        gen_id = row["id"]
    finally:
        conn.close()
    return download_generation(gen_id)


@app.get("/api/generations/{generation_id}/download")
def download_generation(generation_id: int):
    conn = get_connection()
    try:
        gen = _generation_or_404(conn, generation_id)
        rows = conn.execute(
            "SELECT * FROM result_rows WHERE generation_id=? ORDER BY id",
            (generation_id,)).fetchall()

        def iter_csv():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(CSV_COLUMNS)
            yield buffer.getvalue()
            for r in rows:
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([
                    generation_id, r["account"], r["meter"],
                    r["interval_start"] or "", r["interval_end"] or "",
                    "" if r["start_reading"] is None else r["start_reading"],
                    "" if r["end_reading"] is None else r["end_reading"],
                    "" if r["consumption_m3"] is None else r["consumption_m3"],
                    r["attributed_day"] or "",
                    r["status"],
                    r["review_reason"] or "",
                    reason_label(r["review_reason"]) or "",
                    r["source_reading_ids"],
                ])
                yield buffer.getvalue()

        headers = {
            "Content-Disposition":
                f'attachment; filename="usage_generation_{generation_id}.csv"',
            "X-Generation-Status": gen["status"],
        }
        return StreamingResponse(
            iter_csv(), media_type="text/csv; charset=utf-8", headers=headers)
    finally:
        conn.close()


@app.get("/api/generations/{generation_id}/rows")
def generation_rows(generation_id: int, limit: int = 1000):
    conn = get_connection()
    try:
        _generation_or_404(conn, generation_id)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM result_rows WHERE generation_id=? ORDER BY id LIMIT ?",
            (generation_id, limit))]
        for r in rows:
            r["review_reason_detail"] = reason_label(r["review_reason"])
        return {"generation_id": generation_id, "rows": rows}
    finally:
        conn.close()


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Reload manifests: account-day partitions needing reload after late readings
# or replacement corrections, derived against the latest generation.
# ---------------------------------------------------------------------------

@app.post("/api/reload-manifests", response_model=Message)
def create_reload_manifest():
    conn = get_connection()
    try:
        try:
            detail = build_reload_manifest(conn)
        except ManifestError as exc:
            # Business-level failure: publish nothing new; keep the old list,
            # marked expired with the failure reason.
            _mark_active_manifest_failed(conn, str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            _mark_active_manifest_failed(conn, f"清单生成失败: {exc}")
            raise HTTPException(status_code=500,
                                detail=f"清单生成失败: {exc}") from exc
        return Message(
            ok=True,
            message=(
                f"重载核查清单第 {detail['manifest_id']} 版已发布为现役："
                f"基线为第 {detail['baseline_generation_id']} 代结果，"
                f"共 {detail['change_count']} 个账户日需要重载"
            ),
            detail=detail,
        )
    finally:
        conn.close()


def _manifest_or_404(conn: sqlite3.Connection, manifest_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM reload_manifests WHERE id=?", (manifest_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"不存在第 {manifest_id} 版核查清单")
    return row


@app.get("/api/reload-manifests")
def list_reload_manifests():
    conn = get_connection()
    try:
        manifests = [dict(r) for r in conn.execute(
            "SELECT * FROM reload_manifests ORDER BY id DESC")]
        for m in manifests:
            m["change_count"] = conn.execute(
                "SELECT COUNT(*) FROM manifest_changes WHERE manifest_id=?",
                (m["id"],)).fetchone()[0]
        return {"manifests": manifests}
    finally:
        conn.close()


@app.get("/api/reload-manifests/{manifest_id}/changes")
def manifest_changes(manifest_id: int, limit: int = 1000):
    conn = get_connection()
    try:
        _manifest_or_404(conn, manifest_id)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM manifest_changes WHERE manifest_id=? "
            "ORDER BY account, partition_day, id LIMIT ?",
            (manifest_id, limit))]
        for r in rows:
            r["change_type_labels"] = change_labels(r["change_types"])
            r["baseline_details"] = json.loads(r["baseline_details"])
            r["current_details"] = json.loads(r["current_details"])
            r["baseline_review_reason_labels"] = [
                reason_label(x) for x in r["baseline_review_reasons"].split(",") if x
            ]
            r["current_review_reason_labels"] = [
                reason_label(x) for x in r["current_review_reasons"].split(",") if x
            ]
        return {"manifest_id": manifest_id, "changes": rows}
    finally:
        conn.close()


MANIFEST_CSV_COLUMNS = [
    "manifest_id", "account", "partition_utc_day", "change_types",
    "change_type_labels", "baseline_consumption_m3", "current_consumption_m3",
    "baseline_review_reasons", "current_review_reasons",
    "baseline_source_reading_ids", "current_source_reading_ids",
]


@app.get("/api/reload-manifests/{manifest_id}/download")
def download_manifest(manifest_id: int):
    conn = get_connection()
    try:
        manifest = _manifest_or_404(conn, manifest_id)
        rows = conn.execute(
            "SELECT * FROM manifest_changes WHERE manifest_id=? "
            "ORDER BY account, partition_day, id",
            (manifest_id,)).fetchall()

        def iter_csv():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(MANIFEST_CSV_COLUMNS)
            yield buffer.getvalue()
            for r in rows:
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([
                    manifest_id,
                    r["account"],
                    r["partition_day"],
                    r["change_types"],
                    change_labels(r["change_types"]),
                    "" if r["baseline_consumption_m3"] is None
                    else r["baseline_consumption_m3"],
                    "" if r["current_consumption_m3"] is None
                    else r["current_consumption_m3"],
                    r["baseline_review_reasons"] or "",
                    r["current_review_reasons"] or "",
                    r["baseline_source_ids"],
                    r["current_source_ids"],
                ])
                yield buffer.getvalue()

        headers = {
            "Content-Disposition":
                f'attachment; filename="reload_manifest_{manifest_id}.csv"',
            "X-Manifest-Status": manifest["status"],
        }
        return StreamingResponse(
            iter_csv(), media_type="text/csv; charset=utf-8", headers=headers)
    finally:
        conn.close()
