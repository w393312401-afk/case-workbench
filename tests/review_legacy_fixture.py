"""Seed historical review records in disposable test stores, never through retired APIs."""
import copy

from direction_store import now, product_key


def seed_legacy_followup(store, video, **values):
    data = store.read()
    old = next((row for row in data["entries"] if store.key(row) == store.key(video)), None)
    event = {"state": "reviewed", "decision": "", "note": "历史人工复盘。", "outcome": "",
             "evidence": "", "next_review_on": "", "observation_reason": "", "defer_until": "",
             "reference_value": "", "at": now(), **values}
    entry = {"product": product_key(video["product"]), "video_id": video["video_id"],
             "window_end": video["window_end"], **event,
             "history": [*(old or {}).get("history", []), copy.deepcopy(event)]}
    if old:
        data["entries"][data["entries"].index(old)] = entry
    else:
        data["entries"].append(entry)
    store.write(data)
    return copy.deepcopy(entry)
