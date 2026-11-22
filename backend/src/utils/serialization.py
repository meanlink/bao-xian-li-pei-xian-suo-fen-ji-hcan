import json


def row_claim(row):
    if not row:
        return None
    item = dict(row)
    for key in ("required_documents", "received_documents", "missing_documents", "intake_received_documents"):
        if isinstance(item.get(key), str):
            item[key] = json.loads(item[key])
    if item.get("loss_amount") is not None:
        item["loss_amount"] = float(item["loss_amount"])
    if item.get("model_score") is not None:
        item["model_score"] = float(item["model_score"])
    if item.get("model_threshold") is not None:
        item["model_threshold"] = float(item["model_threshold"])
    return item
