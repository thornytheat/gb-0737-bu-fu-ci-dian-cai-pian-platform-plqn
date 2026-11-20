from time import perf_counter_ns

from app.archive import lookup, lookup_supplement, review_supplement, save_supplement
from app.manifest import hash_payload, timestamp
from app.modules.m8_supplement.service import parse_order_fields, plan_supplement, supplement_identity
from app.reasons import FabricError


def submit_supplement(archive, run_id, fields):
    order = parse_order_fields(fields)
    with archive.write() as db:
        document = lookup(db, run_id)
        if document is None:
            raise FabricError("RUN_UNKNOWN", 404)
        key = supplement_identity(run_id, document["manifest"]["kerf_mm"], order)
        current = lookup_supplement(db, key["id"])
        if current:
            return current["manifest"], True
        start = perf_counter_ns()
        snapshot = plan_supplement(document, order)
        manifest = {**key, "kind": "supplement", "created_at": timestamp(),
                    "elapsed_ms": (perf_counter_ns() - start) // 1_000_000,
                    "result_sha256": hash_payload(snapshot),
                    "piece_count": len(snapshot["placements"]),
                    "shortfall": order["quantity"] - len(snapshot["placements"]),
                    "rolls": sorted({piece["roll"] for piece in snapshot["placements"]}),
                    "totals": snapshot["totals"]}
        save_supplement(db, {"manifest": manifest, "snapshot": snapshot})
        review_supplement(db, key["id"], manifest["created_at"], "committed")
        return manifest, False


def replay_supplement(archive, supplement_id):
    with archive.write() as db:
        stored = lookup_supplement(db, supplement_id)
        if stored is None:
            raise FabricError("SUPPLEMENT_UNKNOWN", 404)
        manifest = stored["manifest"]
        parent = lookup(db, manifest["parent"])
        if parent is None:
            raise FabricError("RUN_UNKNOWN", 404)
        snapshot = plan_supplement(parent, manifest["order"])
        digest = manifest["result_sha256"]
        if hash_payload(snapshot) != digest or hash_payload(stored["snapshot"]) != digest:
            raise FabricError("REPLAY_DIFF", 409)
        review_supplement(db, supplement_id, timestamp(), "replayed")
        return manifest
