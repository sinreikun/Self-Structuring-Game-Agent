"""Job-scoped accounting of exact image attachments; no semantic inference."""
import hashlib
import json


def delivery_key(evidence,attachment):
    return json.dumps(evidence,sort_keys=True,separators=(',',':'))+':'+hashlib.sha256(attachment.encode()).hexdigest()


def record_delivery(payload,images,candidates,suppressed,step):
    ledger=payload.setdefault('image_deliveries',{})
    for key,attachment in candidates.items():
        if attachment in images:ledger.setdefault(key,step)
    if suppressed:
        payload.setdefault('image_suppressions',[]).extend(dict(item,step=step) for item in suppressed)
        payload['image_suppressed_count']=payload.get('image_suppressed_count',0)+len(suppressed)
        payload['image_saved_bytes']=payload.get('image_saved_bytes',0)+sum(item['bytes'] for item in suppressed)
