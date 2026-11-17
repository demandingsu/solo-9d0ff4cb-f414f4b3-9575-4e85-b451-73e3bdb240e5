"""Tests for the reload-manifest feature (account-day partition reload list)."""
import json
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("WATER_DATA_DIR", tmp)
    monkeypatch.setenv("WATER_DB_PATH", os.path.join(tmp, "test.db"))
    import importlib
    import app.config as config
    import app.database as database
    import app.main as main
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def _files(c, kind, content, name="x.csv"):
    return c.post(
        f"/api/uploads/{kind}",
        files={"file": (name, content.encode("utf-8"), "text/csv")},
    )


def _gen(c):
    resp = c.post("/api/generations")
    assert resp.status_code == 200, resp.text
    return resp.json()["detail"]["generation_id"]


def _manifest(c):
    resp = c.post("/api/reload-manifests")
    assert resp.status_code == 200, resp.text
    return resp.json()["detail"]


def _changes(c, mid):
    data = c.get(f"/api/reload-manifests/{mid}/changes?limit=10000").json()
    return {ch["account"] + "|" + ch["partition_day"]: ch for ch in data["changes"]}


def _manifests(c):
    return c.get("/api/reload-manifests").json()["manifests"]


REPL = (
    "account,old_meter,new_meter,switched_at\n"
    "A1,OLD1,NEW1,2026-01-10T00:00:00Z\n"
)


def test_no_generation_manifest_fails_409_and_nothing_published(client):
    resp = client.post("/api/reload-manifests")
    assert resp.status_code == 409
    assert _manifests(client) == []


def test_manifest_without_changes_is_empty(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    g1 = _gen(client)
    detail = _manifest(client)
    assert detail["baseline_generation_id"] == g1
    assert detail["change_count"] == 0
    assert _changes(client, detail["manifest_id"]) == {}
    # The empty manifest is still the active, current list.
    assert _manifests(client)[0]["status"] == "active"


def test_late_reading_split_covers_both_sides_of_old_interval(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n"
           "A1,OLD1,2026-01-03T00:00:00Z,130\n")
    g1 = _gen(client)

    # A late reading lands inside the (Jan 2 -> Jan 3) interval, splitting it.
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-02T12:00:00Z,120\n")
    detail = _manifest(client)
    ch = _changes(client, detail["manifest_id"])

    # Jan 2: the original pair (100->110) is replaced by two pieces on either
    # side of the new reading; Jan 3: its pair now starts from the new reading.
    assert "A1|2026-01-02" in ch and "A1|2026-01-03" in ch
    jan2 = ch["A1|2026-01-02"]
    assert "consumption_changed" in jan2["change_types"]
    assert jan2["baseline_consumption_m3"] == 10.0     # 110 - 100
    assert jan2["current_consumption_m3"] == 20.0      # (110-100) + (120-110)
    assert "source_readings_changed" in jan2["change_types"]
    jan3 = ch["A1|2026-01-03"]
    assert jan3["baseline_consumption_m3"] == 20.0     # 130 - 110
    assert jan3["current_consumption_m3"] == 10.0      # 130 - 120
    # Before/after evidence is attached to every changed account-day.
    assert jan2["baseline_details"] and jan2["current_details"]
    assert jan3["baseline_details"] and jan3["current_details"]


def test_earliest_late_reading_adds_new_day_and_changes_existing(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    _gen(client)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2025-12-31T00:00:00Z,90\n")
    detail = _manifest(client)
    ch = _changes(client, detail["manifest_id"])

    added = ch["A1|2025-12-31"]
    assert added["change_types"] == "added"
    assert added["baseline_details"] == []
    assert added["current_details"][0]["review_reason"] == "unpaired_reading"
    # Jan 1 stays 10 m3 but now pairs with the new earliest reading.
    jan1 = ch["A1|2026-01-01"]
    assert "source_readings_changed" in jan1["change_types"]
    assert jan1["current_consumption_m3"] == 10.0


def test_identical_retransmission_creates_no_spurious_partition(client):
    _files(client, "replacements", REPL)
    payload = (
        "account,meter,read_at,reading_cum\n"
        "A1,OLD1,2026-01-01T00:00:00Z,100\n"
        "A1,OLD1,2026-01-02T00:00:00Z,110\n"
    )
    _files(client, "readings", payload)
    _gen(client)
    # Re-upload byte-identical readings: all duplicates, inputs effectively unchanged.
    resp = _files(client, "readings", payload)
    assert resp.json()["detail"]["inserted"] == 0
    detail = _manifest(client)
    assert detail["change_count"] == 0
    assert _changes(client, detail["manifest_id"]) == {}


def test_replacement_correction_flags_review_reason_change(client):
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A5,O5,N5,2026-01-10T00:00:00Z\n")
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A5,O5,2026-01-08T00:00:00Z,10\n"
           "A5,O5,2026-01-09T00:00:00Z,13\n")
    _gen(client)
    # Correction moves the switch earlier: the Jan 9 reading falls out of window.
    _files(client, "replacements",
           "account,old_meter,new_meter,switched_at\n"
           "A5,O5,N5,2026-01-08T12:00:00Z\n")
    detail = _manifest(client)
    ch = _changes(client, detail["manifest_id"])
    jan9 = ch["A5|2026-01-09"]
    assert "review_reason_changed" in jan9["change_types"]
    assert jan9["baseline_review_reasons"] == ""
    assert "outside_meter_service_window" in jan9["current_review_reasons"]
    # REVIEW rows are bucketed by the later reading's UTC day, same as the OK row they replace.
    assert jan9["current_details"][0]["partition_utc_day"] == "2026-01-09"


def test_manifest_is_bound_to_baseline_generation_and_batches(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n")
    g1 = _gen(client)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")  # second reading batch
    batches = client.get("/api/batches").json()["batches"]
    reading_ids = sorted(str(b["id"]) for b in batches
                         if b["batch_type"] == "reading" and b["status"] == "committed")
    replacement_ids = [b["id"] for b in batches
                       if b["batch_type"] == "replacement" and b["status"] == "committed"]
    detail = _manifest(client)
    assert detail["baseline_generation_id"] == g1
    assert detail["reading_batch_ids"] == reading_ids
    assert detail["replacement_batch_id"] == replacement_ids[-1]
    stored = _manifests(client)[0]
    assert stored["reading_batch_ids"] == ",".join(reading_ids)
    assert stored["baseline_generation_id"] == g1


def test_upload_and_generation_expire_active_manifest(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n")
    _gen(client)
    m1 = _manifest(client)["manifest_id"]
    assert next(m for m in _manifests(client) if m["id"] == m1)["status"] == "active"

    # A new upload must expire the current manifest.
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    assert next(m for m in _manifests(client) if m["id"] == m1)["status"] == "expired"

    m2 = _manifest(client)["manifest_id"]
    assert next(m for m in _manifests(client) if m["id"] == m2)["status"] == "active"

    # A new generation changes the baseline and must also expire the manifest.
    _gen(client)
    assert next(m for m in _manifests(client) if m["id"] == m2)["status"] == "expired"
    assert all(m["status"] == "expired"
               for m in _manifests(client) if m["id"] != m2)


def test_only_one_active_manifest_and_expired_remains_downloadable(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n")
    _gen(client)
    m1 = _manifest(client)["manifest_id"]
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    m2 = _manifest(client)["manifest_id"]
    actives = [m for m in _manifests(client) if m["status"] == "active"]
    assert len(actives) == 1 and actives[0]["id"] == m2
    dl = client.get(f"/api/reload-manifests/{m1}/download")
    assert dl.status_code == 200
    assert dl.headers["X-Manifest-Status"] == "expired"
    assert "partition_utc_day" in dl.text.splitlines()[0]


def test_manifest_build_failure_keeps_old_list_marked_expired(client):
    import app.main as main
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n")
    _gen(client)
    m1 = _manifest(client)["manifest_id"]

    original = main.build_reload_manifest

    def boom(conn):
        raise main.ManifestError("模拟清单生成失败")

    main.build_reload_manifest = boom
    resp = client.post("/api/reload-manifests")
    main.build_reload_manifest = original
    assert resp.status_code == 409

    stored = next(m for m in _manifests(client) if m["id"] == m1)
    assert stored["status"] == "expired"
    assert "失败" in stored["status_detail"]
    # Still available for traceability.
    assert client.get(f"/api/reload-manifests/{m1}/download").status_code == 200
    # No new manifest row was published by the failed attempt.
    assert len(_manifests(client)) == 1


def test_interleaved_manifest_builds_and_uploads_never_publish_stale_active(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    g1 = _gen(client)

    errors = []

    def build_many():
        try:
            for _ in range(8):
                r = client.post("/api/reload-manifests")
                if r.status_code != 200:
                    errors.append(r.status_code)
        except Exception as exc:  # pragma: no cover - failure surface
            errors.append(repr(exc))

    def upload_many():
        try:
            for day in range(3, 11):
                _files(client, "readings",
                       "account,meter,read_at,reading_cum\n"
                       f"A1,OLD1,2026-01-{day:02d}T00:00:00Z,{100 + day * 10}\n")
        except Exception as exc:  # pragma: no cover - failure surface
            errors.append(repr(exc))

    t1 = threading.Thread(target=build_many)
    t2 = threading.Thread(target=upload_many)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors

    manifests = _manifests(client)
    actives = [m for m in manifests if m["status"] == "active"]
    assert len(actives) <= 1
    if actives:
        active = actives[0]
        # An active manifest must describe exactly the inputs committed at that
        # moment: its batch list equals the current committed reading batches.
        batches = client.get("/api/batches").json()["batches"]
        committed_reading = [
            str(x) for x in sorted(
                b["id"] for b in batches
                if b["batch_type"] == "reading" and b["status"] == "committed"
            )
        ]
        assert active["reading_batch_ids"].split(",") == committed_reading
        assert active["baseline_generation_id"] == g1


def test_diff_account_days_direct_unit():
    from app.manifest import diff_account_days

    def row(account, day, status, consumption, reason, sources, end=None, start=None):
        return {
            "account": account, "meter": "M", "status": status,
            "review_reason": reason,
            "interval_start": start, "interval_end": end or f"{day}T00:00:00Z",
            "start_reading": None, "end_reading": None,
            "consumption_m3": consumption, "attributed_day": day,
            "source_ids": sources,
        }

    base = [
        row("A", "2026-01-01", "OK", 10.0, None, [1, 2]),
        row("A", "2026-01-02", "OK", 5.0, None, [2, 3]),
        row("B", "2026-01-01", "REVIEW", None, "unpaired_reading", [7]),
    ]
    current = [
        row("A", "2026-01-01", "OK", 10.0, None, [1, 9]),      # source change only
        row("A", "2026-01-02", "OK", 8.0, None, [9, 3]),       # consumption + source
        row("B", "2026-01-01", "REVIEW", None, "reversed_reading", [7]),  # reason change
        row("C", "2026-01-03", "OK", 4.0, None, [11, 12]),     # brand new bucket
    ]
    changes = diff_account_days(base, current)
    keys = {c["account"] + "|" + c["partition_day"]: c for c in changes}
    assert set(keys) == {"A|2026-01-01", "A|2026-01-02", "B|2026-01-01", "C|2026-01-03"}
    assert keys["A|2026-01-01"]["change_types"] == ["source_readings_changed"]
    assert "consumption_changed" in keys["A|2026-01-02"]["change_types"]
    assert keys["B|2026-01-01"]["change_types"] == ["review_reason_changed"]
    assert keys["C|2026-01-03"]["change_types"] == ["added"]

    # Fully identical inputs -> no changes at all (no spurious partitions).
    assert diff_account_days(base, [dict(r) for r in base]) == []


def test_manifest_changes_endpoint_returns_json_evidence(client):
    _files(client, "replacements", REPL)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-01T00:00:00Z,100\n"
           "A1,OLD1,2026-01-02T00:00:00Z,110\n")
    _gen(client)
    _files(client, "readings",
           "account,meter,read_at,reading_cum\n"
           "A1,OLD1,2026-01-03T00:00:00Z,125\n")
    mid = _manifest(client)["manifest_id"]
    payload = client.get(f"/api/reload-manifests/{mid}/changes").json()
    assert payload["manifest_id"] == mid
    change = next(c for c in payload["changes"] if c["partition_day"] == "2026-01-03")
    assert change["change_types"] == "added"
    # Evidence is structured JSON, not opaque strings.
    assert isinstance(change["current_details"], list)
    assert change["current_details"][0]["consumption_m3"] == 15.0
    assert json.loads  # module import sanity; endpoint already parsed JSON above
