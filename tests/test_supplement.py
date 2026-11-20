import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest


def order(**overrides):
    payload = {"订单号": "S001", "布种": "斜纹棉", "最小幅宽": "1400", "幅宽单位": "mm",
               "净长": "180", "净长单位": "mm", "单端缝份": "10", "缝份单位": "mm",
               "数量": "1", "优先级": "急"}
    return payload | overrides


@pytest.fixture
def run_id(client):
    return client.post("/api/runs", json={"sample": "dirty", "kerf_mm": 5}).json()["manifest"]["id"]


def supplement(client, run_id, **overrides):
    return client.post(f"/api/runs/{run_id}/supplements", json=order(**overrides))


def test_supplement_carves_archived_remnants_and_reconciles(client, run_id):
    written = supplement(client, run_id)
    assert written.status_code == 201 and written.json()["reused"] is False
    manifest = written.json()["manifest"]
    assert (manifest["kind"], manifest["parent"], manifest["kerf_mm"]) == ("supplement", run_id, 5)
    assert (manifest["piece_count"], manifest["shortfall"], manifest["rolls"]) == (1, 0, ["R-001"])
    assert manifest["order"] == {"id": "S001", "fabric": "斜纹棉", "width": 1400, "net": 180,
                                 "seam": 10, "length": 200, "quantity": 1, "priority": "急"}
    snapshot = client.get(f"/api/supplements/{manifest['id']}").json()["snapshot"]
    piece, = snapshot["placements"]
    assert [(piece["roll"], piece["start"], piece["end"], piece["kerf_start"], piece["kerf_end"])] == [
        ("R-001", 1960, 2160, 2160, 2165)]
    assert piece["origin"] == {"run": run_id, "roll": "R-001", "span": [1960, 2189]}
    # 剩余段证据：只来自已裁卷，留存卷 R-004 与封存卷 R-003 不提供余料
    assert [(s["run"], s["roll"], s["start"], s["end"]) for s in snapshot["segments"]] == [
        (run_id, "R-001", 860, 990), (run_id, "R-001", 1960, 2189), (run_id, "R-002", 1796, 1800)]
    assert all(s["source"].startswith("dirty-rolls.csv:") for s in snapshot["segments"])
    books = {roll["roll"]: roll for roll in snapshot["rolls"]}
    first = books["R-001"]
    assert first["base"] == {"piece": 1700, "blocked": 121, "kerf": 20, "unused": 359}
    assert first["supplement"] == {"piece": 200, "kerf": 5} and first["remaining"] == 154
    assert first["lengths"] == {"piece": 1900, "blocked": 121, "kerf": 25, "unused": 154}
    assert first["delta"] == 0 and sum(first["lengths"].values()) == first["length"]
    carved = [span for span in first["spans"] if span.get("batch") == manifest["id"]]
    assert [(span["type"], span["start"], span["end"]) for span in carved] == [("piece", 1960, 2160), ("kerf", 2160, 2165)]
    assert books["R-004"]["remaining"] == 1200 and books["R-004"]["supplement"] == {"piece": 0, "kerf": 0}
    totals = snapshot["totals"]
    assert totals["base"] == {"piece": 4090, "blocked": 2932, "kerf": 40, "unused": 1563}
    assert totals["supplement"] == {"piece": 200, "kerf": 5} and totals["remaining"] == 1358
    assert totals["base"]["unused"] == totals["supplement"]["piece"] + totals["supplement"]["kerf"] + totals["remaining"]
    assert totals["delta"] == 0
    assert totals["length"] == totals["piece"] + totals["blocked"] + totals["kerf"] + totals["unused"]


def test_same_supplement_is_reused_not_duplicated(client, run_id):
    first = supplement(client, run_id)
    assert first.status_code == 201
    manifest = first.json()["manifest"]
    before = client.get(f"/api/supplements/{manifest['id']}/export").content
    again = supplement(client, run_id)
    assert again.status_code == 200 and again.json() == {"manifest": manifest, "reused": True}
    equivalent = supplement(client, run_id, 净长="0.18", 净长单位="m", **{"单端缝份": "1", "缝份单位": "cm"})
    assert equivalent.status_code == 200 and equivalent.json()["manifest"]["id"] == manifest["id"]
    listing = client.get(f"/api/runs/{run_id}/supplements").json()["supplements"]
    assert [item["id"] for item in listing] == [manifest["id"]]
    assert client.get(f"/api/supplements/{manifest['id']}/export").content == before


def test_distinct_supplements_coexist_on_one_run(client, run_id):
    first = supplement(client, run_id).json()["manifest"]
    second = supplement(client, run_id, 订单号="S002", 净长="100").json()["manifest"]
    assert first["id"] != second["id"]
    listing = client.get(f"/api/runs/{run_id}/supplements").json()["supplements"]
    assert {item["id"] for item in listing} == {first["id"], second["id"]}
    # 两版补裁单都从原归档回读剩余段，互不影响
    for manifest in (first, second):
        snapshot = client.get(f"/api/supplements/{manifest['id']}").json()["snapshot"]
        assert snapshot["totals"]["base"]["unused"] == 1563 and snapshot["totals"]["delta"] == 0


def test_failed_supplement_preserves_archive_and_prior_versions(client, run_id):
    manifest = supplement(client, run_id).json()["manifest"]
    run_before = client.get(f"/api/export/{run_id}").content
    supp_before = client.get(f"/api/supplements/{manifest['id']}/export").content
    bad = supplement(client, run_id, 数量="0")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "BAD_VALUE"
    assert client.get(f"/api/export/{run_id}").content == run_before
    assert client.get(f"/api/supplements/{manifest['id']}/export").content == supp_before
    assert [item["id"] for item in client.get(f"/api/runs/{run_id}/supplements").json()["supplements"]] == [manifest["id"]]


@pytest.mark.parametrize("fields,code", [
    ({"订单号": ""}, "MISSING"),
    ({"优先级": "特"}, "BAD_VALUE"),
    ({"数量": "13"}, "BAD_VALUE"),
    ({"净长单位": "寸"}, "BAD_UNIT"),
    ({"净长": "0.5"}, "SUB_MM"),
    ({"备注": "加急"}, "FORM_INVALID"),
    ({"数量": ["1"]}, "FORM_INVALID"),
])
def test_supplement_fields_follow_original_intake_contract(client, run_id, fields, code):
    response = supplement(client, run_id, **fields)
    assert response.status_code == 422 and response.json()["error"]["code"] == code
    assert client.get(f"/api/runs/{run_id}/supplements").json()["supplements"] == []


def test_supplement_reads_remnants_from_archive_after_source_removed(client, settings, run_id):
    manifest = supplement(client, run_id).json()["manifest"]
    before = client.get(f"/api/supplements/{manifest['id']}").json()
    for name in ("dirty-rolls.csv", "dirty-defects.csv", "dirty-orders.csv"):
        (settings.samples / name).unlink()
    rebuilt = supplement(client, run_id, 订单号="S002", 净长="100")
    assert rebuilt.status_code == 201
    snapshot = rebuilt.json()["manifest"]
    assert snapshot["totals"]["base"]["unused"] == 1563
    assert client.get(f"/api/supplements/{manifest['id']}").json() == before
    verified = client.post(f"/api/supplements/{manifest['id']}/rerun")
    assert verified.status_code == 200 and verified.json()["verified"] is True


def test_supplement_rerun_verifies_without_adding_version(client, settings, run_id):
    manifest = supplement(client, run_id).json()["manifest"]
    before = client.get(f"/api/supplements/{manifest['id']}/export").content
    verified = client.post(f"/api/supplements/{manifest['id']}/rerun")
    assert verified.json() == {"manifest": manifest, "verified": True}
    assert [item["id"] for item in client.get(f"/api/runs/{run_id}/supplements").json()["supplements"]] == [manifest["id"]]
    assert client.get(f"/api/supplements/{manifest['id']}/export").content == before
    with closing(sqlite3.connect(settings.database)) as db:
        assert db.execute("SELECT action FROM supplement_reviews ORDER BY seq").fetchall() == [("committed",), ("replayed",)]


def test_unknown_run_and_unknown_supplement(client):
    missing_run = client.post("/api/runs/missing/supplements", json=order())
    assert (missing_run.status_code, missing_run.json()["error"]["code"]) == (404, "RUN_UNKNOWN")
    assert client.get("/api/runs/missing/supplements").status_code == 404
    for path in ("/api/supplements/missing", "/api/supplements/missing/export"):
        reply = client.get(path)
        assert (reply.status_code, reply.json()["error"]["code"]) == (404, "SUPPLEMENT_UNKNOWN")
    rerun = client.post("/api/supplements/missing/rerun")
    assert (rerun.status_code, rerun.json()["error"]["code"]) == (404, "SUPPLEMENT_UNKNOWN")


def test_supplement_inherits_parent_kerf(client):
    run_id = client.post("/api/runs", json={"sample": "dirty", "kerf_mm": 6}).json()["manifest"]["id"]
    manifest = supplement(client, run_id).json()["manifest"]
    assert manifest["kerf_mm"] == 6
    snapshot = client.get(f"/api/supplements/{manifest['id']}").json()["snapshot"]
    assert all(piece["kerf_end"] - piece["kerf_start"] == 6 for piece in snapshot["placements"])
    assert snapshot["totals"]["delta"] == 0


def test_unplaceable_supplement_records_shortfall_review(client, run_id):
    canvas = supplement(client, run_id, 订单号="S101", 布种="帆布", 最小幅宽="1300", 净长="600")
    assert canvas.status_code == 201
    manifest = canvas.json()["manifest"]
    assert (manifest["piece_count"], manifest["shortfall"]) == (0, 1)
    review, = client.get(f"/api/supplements/{manifest['id']}").json()["snapshot"]["orders"]
    assert (review["planned"], review["remaining"], review["review"]["code"]) == (0, 1, "SEGMENT_TOO_SHORT")
    foreign = supplement(client, run_id, 订单号="S102", 布种="无纺布")
    assert foreign.status_code == 201
    snapshot = client.get(f"/api/supplements/{foreign.json()['manifest']['id']}").json()["snapshot"]
    assert snapshot["orders"][0]["review"]["code"] == "NO_COMPATIBLE_ROLL"
    partial = supplement(client, run_id, 订单号="S103", 数量="2")
    assert partial.status_code == 201
    snapshot = client.get(f"/api/supplements/{partial.json()['manifest']['id']}").json()["snapshot"]
    assert [(item["planned"], item["remaining"], item["review"]["code"]) for item in snapshot["orders"]] == [
        (1, 1, "CAPACITY_USED")]


def test_concurrent_identical_supplements_store_one_version(client, run_id):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: supplement(client, run_id), range(4)))
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    assert len({response.json()["manifest"]["id"] for response in responses}) == 1
    assert len(client.get(f"/api/runs/{run_id}/supplements").json()["supplements"]) == 1


def test_supplement_export_is_self_contained(client, run_id):
    manifest = supplement(client, run_id).json()["manifest"]
    download = client.get(f"/api/supplements/{manifest['id']}/export")
    assert download.headers["content-disposition"] == f'attachment; filename="fabric-supp-{manifest["id"][:12]}.json"'
    document = json.loads(download.content)
    assert document == client.get(f"/api/supplements/{manifest['id']}").json()
    assert document["manifest"]["parent"] == run_id
    assert {segment["run"] for segment in document["snapshot"]["segments"]} == {run_id}
