from collections import Counter, defaultdict

from app.config import VERSION
from app.manifest import hash_payload
from app.modules.m1_intake.contract import ORDERS
from app.modules.m1_intake.contract import order as parse_order
from app.modules.m4_fit.service import fit
from app.modules.m5_changeovers.service import changeovers
from app.reasons import FabricError

SOURCE_REF = "补单·工作台登记"


def parse_order_fields(fields):
    """补单按原工单采信口径校验：同样的缺值、单位与范围规则。"""
    if not isinstance(fields, dict) or set(fields) - set(ORDERS):
        raise FabricError("FORM_INVALID")
    data = {}
    for column in ORDERS:
        value = fields.get(column, "")
        if value is None:
            value = ""
        if isinstance(value, (bool, dict, list)):
            raise FabricError("FORM_INVALID")
        data[column] = str(value)
    return parse_order(data)


def supplement_identity(run_id, kerf, order):
    payload = {"parent": run_id, "rules": VERSION, "kerf_mm": kerf, "order": order}
    return {"id": hash_payload(payload), **payload}


def remnant_rolls(snapshot, run_id):
    """已裁卷剩余的可用段，从归档批次回读；留存卷与封存卷不动。"""
    cut = {item["roll"] for item in snapshot["changeovers"]["placements"]}
    zones = {roll["id"]: roll for roll in snapshot["zones"]["rolls"]}
    rolls, segments = [], []
    for book in snapshot["ledger"]["rolls"]:
        roll_id = book["roll"]
        if roll_id not in cut or book["held"]:
            continue
        spans = [(span["start"], span["end"]) for span in book["spans"] if span["type"] == "unused"]
        zone = zones[roll_id]
        for start, end in spans:
            segments.append({"run": run_id, "roll": roll_id, "start": start, "end": end,
                             "length": end - start, "source": book["source"]})
        rolls.append({"id": roll_id, "fabric": zone["fabric"], "width": zone["width"],
                      "length": sum(end - start for start, end in spans), "blocked_mm": 0,
                      "held": False, "available": [{"start": start, "end": end} for start, end in spans]})
    return rolls, segments


def reconcile(books, placements, kerf, batch):
    """把追加批次的裁片与刀口并入原批次勾稽：新刀口只落在归档剩余段内。"""
    by_roll = defaultdict(list)
    for piece in placements:
        if piece["kerf_start"] != piece["end"] or piece["kerf_end"] - piece["kerf_start"] != kerf:
            raise RuntimeError("补裁切口长度或起点异常")
        by_roll[piece["roll"]].append(piece)
    rolls = []
    for book in books:
        pending = sorted(by_roll.get(book["roll"], ()), key=lambda piece: piece["start"])
        spans, carved = [], []
        for span in book["spans"]:
            inside = [piece for piece in pending
                      if span["start"] <= piece["start"] and piece["kerf_end"] <= span["end"]]
            if span["type"] != "unused":
                if inside:
                    raise RuntimeError("补裁刀口压进已归档坐标")
                spans.append(dict(span))
                continue
            cursor = span["start"]
            for piece in inside:
                if piece["start"] < cursor:
                    raise RuntimeError("补裁裁片互相交叠")
                if cursor < piece["start"]:
                    spans.append({"start": cursor, "end": piece["start"], "type": "unused"})
                spans.append({"start": piece["start"], "end": piece["end"], "type": "piece",
                              "id": piece["id"], "order": piece["order"], "batch": batch})
                spans.append({"start": piece["kerf_start"], "end": piece["kerf_end"], "type": "kerf",
                              "id": piece["id"], "batch": batch})
                carved.append(piece)
                cursor = piece["kerf_end"]
            if cursor < span["end"]:
                spans.append({"start": cursor, "end": span["end"], "type": "unused"})
        if len(carved) != len(pending):
            raise RuntimeError("补裁裁片越出归档剩余段")
        base = book["lengths"]
        supp_piece = sum(piece["end"] - piece["start"] for piece in carved)
        supp_kerf = sum(piece["kerf_end"] - piece["kerf_start"] for piece in carved)
        remaining = base["unused"] - supp_piece - supp_kerf
        combined = {"piece": base["piece"] + supp_piece, "blocked": base["blocked"],
                    "kerf": base["kerf"] + supp_kerf, "unused": remaining}
        sums = Counter({"blocked": 0, "piece": 0, "kerf": 0, "unused": 0})
        for span in spans:
            sums[span["type"]] += span["end"] - span["start"]
        if remaining < 0 or dict(sums) != combined or sum(sums.values()) != book["length"]:
            raise RuntimeError("补裁勾稽不守恒")
        rolls.append({"roll": book["roll"], "length": book["length"], "held": book["held"],
                      "source": book["source"], "base": dict(base),
                      "supplement": {"piece": supp_piece, "kerf": supp_kerf}, "remaining": remaining,
                      "lengths": combined, "delta": book["length"] - sum(combined.values()), "spans": spans})
    totals = {key: sum(roll["lengths"][key] for roll in rolls) for key in ("piece", "blocked", "kerf", "unused")}
    totals["length"] = sum(roll["length"] for roll in rolls)
    totals["delta"] = totals["length"] - sum(totals[key] for key in ("piece", "blocked", "kerf", "unused"))
    totals["base"] = {key: sum(roll["base"][key] for roll in rolls) for key in ("piece", "blocked", "kerf", "unused")}
    totals["supplement"] = {key: sum(roll["supplement"][key] for roll in rolls) for key in ("piece", "kerf")}
    totals["remaining"] = sum(roll["remaining"] for roll in rolls)
    if totals["delta"] != 0 or totals["base"]["unused"] != totals["supplement"]["piece"] + totals["supplement"]["kerf"] + totals["remaining"]:
        raise RuntimeError("原批次与追加批次对平失败")
    return rolls, totals


def plan_supplement(document, order):
    """从归档批次回读已裁卷剩余段，按原口径给短单配长并对平。"""
    manifest = document["manifest"]
    run_id, kerf = manifest["id"], manifest["kerf_mm"]
    batch = supplement_identity(run_id, kerf, order)["id"]
    rolls, segments = remnant_rolls(document["snapshot"], run_id)
    fitted, options = fit(rolls, [{**order, "source": {"ref": SOURCE_REF}}], kerf)
    planning = changeovers(rolls, fitted, options)
    placements = []
    for piece in planning["placements"]:
        span = next(item for item in segments if item["roll"] == piece["roll"]
                    and item["start"] <= piece["start"] and piece["kerf_end"] <= item["end"])
        placements.append({**piece, "origin": {"run": run_id, "roll": piece["roll"],
                                               "span": [span["start"], span["end"]]}})
    reconciled, totals = reconcile(document["snapshot"]["ledger"]["rolls"], placements, kerf, batch)
    return {"parent": run_id, "kerf_mm": kerf, "order": order, "segments": segments,
            "placements": placements, "orders": planning["orders"], "rolls": reconciled, "totals": totals}
