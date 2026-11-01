"""Bounded, meaning-free eyes shared by generated agents and research roles.

Coordinates always refer to source pixels; crops/derived arrays are ephemeral.
No path, executable, network, or interpretation is accepted by this interface.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import threading
import time

from PIL import Image, ImageChops, ImageFilter, ImageStat

MAX_PIXELS = 262144
MAX_VALUES = 2048
MAX_STEPS = 12


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Visual integer outside budget")
    return value


def box_for(value, size):
    box = value if value is not None else [0, 0, *size]
    if not isinstance(box,list) or len(box)!=4 or any(type(v)!=int for v in box):
        raise ValueError(f'Invalid visual region; source_size={list(size)}; box must be four integer source coordinates')
    x,y,r,b=box
    if not (0<=x<r<=size[0] and 0<=y<b<=size[1]):
        raise ValueError(f'Invalid visual region; source_size={list(size)}; require 0<=left<right<=width and 0<=top<bottom<=height; omit box for full source')
    return box


def transform(image, request):
    box = box_for(request.get("box"), image.size)
    size = request.get("size", [32, 32])
    if size == "native":
        size = [box[2] - box[0], box[3] - box[1]]
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError("Invalid visual size")
    for v in size:
        integer(v, 1, MAX_PIXELS)
    if size[0] * size[1] > MAX_PIXELS:
        raise ValueError(f"Visual pixel budget exceeded: size={size}, max_pixels={MAX_PIXELS}. Reduce size or native crop area; limit only bounds returned values")
    mode = request.get("mode", "L")
    if mode not in {"L", "RGB"}:
        raise ValueError("Visual mode must be L or RGB")
    # EXTENT avoids allocating an unbounded intermediate native crop.
    result = image.convert(mode).transform(tuple(size), Image.Transform.EXTENT, tuple(box), Image.Resampling.BILINEAR)
    operations = request.get("operations", [])
    if not isinstance(operations, list) or len(operations) > MAX_STEPS:
        raise ValueError("Visual operation budget exceeded")
    for step in operations:
        if not isinstance(step, dict):
            raise ValueError("Invalid visual operation")
        op = step.get("op")
        if op == "grayscale":
            result = result.convert("L")
        elif op == "threshold":
            threshold = integer(step.get("value", 128), 0, 255)
            result = result.convert("L").point(lambda v: 255 if v >= threshold else 0)
        elif op == "blur":
            radius = integer(step.get("radius", 1), 0, 8)
            result = result.filter(ImageFilter.GaussianBlur(radius))
        elif op == "edge":
            result = result.filter(ImageFilter.FIND_EDGES)
        elif op == "invert":
            result = ImageChops.invert(result)
        else:
            raise ValueError("Visual operation not granted")
    return result, box



class VisualRequestError(ValueError):
    def __init__(self,message,constraints):
        super().__init__(message)
        self.constraints=constraints


def checked_transform(image,request,reference):
    try:return transform(image,request)
    except ValueError as error:
        constraints={'source_size':list(image.size),'source':{k:v for k,v in reference.items() if k in ('event','observation','phase')},
            'requested_size':request.get('size',[32,32]),'max_pixels':MAX_PIXELS,'max_values':MAX_VALUES,
            'box_rule':'0 <= left < right <= source width; 0 <= top < bottom <= source height; omit box for full source',
            'size_rule':'width * height <= max_pixels; native uses the crop dimensions',
            'limit_rule':'limit only bounds returned values; reducing limit does not reduce transformed pixel area'}
        resolved=request.get('size',[32,32])
        if resolved=='native':
            try:
                box=box_for(request.get('box'),image.size);resolved=[box[2]-box[0],box[3]-box[1]]
            except ValueError:resolved=None
        if isinstance(resolved,list) and len(resolved)==2 and all(type(v)==int and 1<=v<=MAX_PIXELS for v in resolved):
            constraints.update(requested_pixels=resolved[0]*resolved[1],
                max_height_at_requested_width=MAX_PIXELS//resolved[0],max_width_at_requested_height=MAX_PIXELS//resolved[1])
        raise VisualRequestError(str(error),constraints) from error

def components(image, threshold=128):
    """Four-connected foreground geometry, without object names."""
    data = bytearray(v >= threshold for v in image.convert("L").tobytes())
    w, h = image.size
    found = []
    total = 0
    for index in range(len(data)):
        if not data[index]:
            continue
        data[index] = 0
        pending = [index]
        left = right = index % w
        top = bottom = index // w
        count = 0
        while pending:
            current = pending.pop()
            x, y = current % w, current // w
            count += 1
            left, right, top, bottom = min(left, x), max(right, x), min(top, y), max(bottom, y)
            for other in ((current-1 if x else -1), (current+1 if x+1 < w else -1), current-w, current+w):
                if 0 <= other < len(data) and data[other]:
                    data[other] = 0
                    pending.append(other)
        total += 1
        if len(found) < 256:
            found.append({"box": [left, top, right+1, bottom+1], "area": count})
    return {"components": found, "component_count": total, "truncated": total > len(found)}


def refresh_pixel_delivery(result):
    """Describe only bytes present in this view, including context-trimmed pages."""
    if 'pixels' not in result and 'pixel_coverage' not in result:return
    size=result.get('size',[]);mode=result.get('mode')
    if not isinstance(size,list) or len(size)!=2 or any(type(v)!=int or v<1 for v in size) or mode not in ('L','RGB'):return
    channels=3 if mode=='RGB' else 1;stride=size[0]*channels
    total=result.get('total_values',size[0]*size[1]*channels);offset=result.get('offset',0)
    present=isinstance(result.get('pixels'),list);count=len(result['pixels']) if present else 0
    def position(index):return [(index%stride)//channels,index//stride,index%channels]
    result['pixel_coverage']={'payload_present':present,'returned_values':count,'channels':channels,'row_stride_values':stride,
        'first_sample':position(offset) if count else None,'last_sample':position(offset+count-1) if count else None,
        'complete_transformed_image':offset==0 and count==total,'minimum_full_image_requests':(total+MAX_VALUES-1)//MAX_VALUES}


def describe(image, request):
    result = {"size": list(image.size), "mode": image.mode}
    output = request.get("output", "pixels")
    if output == "pixels":
        values = image.tobytes()
        offset = integer(request.get("offset", 0), 0, len(values))
        limit = integer(request.get("limit", MAX_VALUES), 1, MAX_VALUES)
        result.update(pixels=list(values[offset:offset+limit]), offset=offset, total_values=len(values),
                      next_offset=offset+limit if offset+limit < len(values) else None)
        if image.mode == "L" and len(values) <= MAX_VALUES and offset == 0:
            result["gray"] = result["pixels"]  # Original Agent ABI compatibility.
        refresh_pixel_delivery(result)
    elif output == "pixel":
        point = request.get("point")
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("Pixel requires output coordinates")
        x = integer(point[0], 0, image.width-1)
        y = integer(point[1], 0, image.height-1)
        result.update(point=point, value=image.getpixel((x, y)))
    elif output == "histogram":
        result["histogram"] = image.histogram()
    elif output == "statistics":
        stats = ImageStat.Stat(image)
        result.update(mean=stats.mean, stddev=stats.stddev, extrema=stats.extrema)
    elif output == "components":
        result.update(components(image, integer(request.get("threshold", 128), 0, 255)))
    elif output != "image":
        raise ValueError("Unknown visual output")
    return result


def evidence_ids(reference):
    if not isinstance(reference, dict):
        return set()
    if type(reference.get("event")) is int:
        return {reference["event"]}
    return evidence_ids(reference.get("left")) | evidence_ids(reference.get("right"))


def resolved_request(request, reference):
    """Bind current/fresh comparisons to immutable capture IDs for retries."""
    result = dict(request)
    if "left" in reference:
        result = resolved_request(result, reference["left"])
        result["compare"] = resolved_request(result["compare"], reference["right"])
        return result
    for key in ("event", "observation", "fresh"):
        result.pop(key, None)
    for key in ("event", "observation", "phase"):
        if key in reference:
            result[key] = reference[key]
    return result


def evaluation_summary(result):
    """Full sample data remains in the DB; capability responses remain bounded."""
    if "metrics" not in result:
        return result
    metrics = result["metrics"]
    compact = {k: v for k, v in metrics.items() if k not in {"baseline", "candidate"}}
    for name in ("baseline", "candidate"):
        compact[name] = {k: v for k, v in metrics.get(name, {}).items() if k != "samples"}
    return {**result, "metrics": compact}


class Eyes:
    def __init__(self, store, activity=None, current=None, fresh=None):
        self.store, self.activity = store, activity
        self.current, self.fresh = current, fresh
        self.lock = threading.RLock()

    def source(self, request, current=None, allowed=None, pin=None):
        event = request.get("event")
        phase = request.get("phase", "before")
        if phase not in {"before", "after", "diff"}:
            raise ValueError("Invalid visual phase")
        if sum(k in request for k in ("event", "observation")) > 1 or (request.get("fresh") and (event is not None or "observation" in request)):
            raise ValueError("Select one visual evidence source")
        if event is not None:
            integer(event, 1, 2**63-1)
            if allowed is not None and event not in allowed:
                raise ValueError("Evidence not yet available")
            with self.store.lock:
                if pin:
                    self.store.pin([event], pin)
                before = self.store.observed_image(event, "before")
                image = before if phase == "before" else self.store.observed_image(event, "after")
                if phase == "diff":
                    if before.size != image.size:
                        raise ValueError("Evidence dimensions differ")
                    image = ImageChops.difference(before.convert("RGB"), image.convert("RGB"))
                row = self.store.db.execute("SELECT before_hash,after_hash FROM events WHERE id=?", (event,)).fetchone()
            return image, {"event": event, "phase": phase, "hashes": list(row)}
        observation = request.get("observation")
        if observation is not None:
            integer(observation, 1, 2**63-1)
            with self.store.lock:
                row = self.store.db.execute("SELECT image_hash,captured FROM observations WHERE id=?", (observation,)).fetchone()
                if not row or not row[0]:
                    raise ValueError("Observation unavailable")
                if phase != "before":
                    raise ValueError("An observation has no Before/After pair")
                if pin:
                    self.store.db.execute("INSERT OR IGNORE INTO visual_observation_pins VALUES(?,?)", (observation, pin))
                    self.store.db.commit()
                with Image.open(self.store.root/"evidence"/(row[0]+".png")) as image:
                    image = image.copy()
            return image, {"observation": observation, "hash": row[0], "captured": row[1]}
        if phase != "before":
            raise ValueError("Current observation has no future outcome")
        callback = self.fresh if request.get("fresh") else self.current
        image = current.copy() if current is not None and not request.get("fresh") else callback() if callback else None
        if image is None:
            raise ValueError("Current frame unavailable")
        # Capture is stored only on demand; every current response can be revisited by ID.
        with self.store.lock:
            digest = self.store.observation(image, "visual_access")
            row = self.store.db.execute("SELECT id,captured FROM observations WHERE image_hash=? ORDER BY id DESC LIMIT 1", (digest,)).fetchone()
        if not digest:
            raise ValueError("Observation could not be retained")
        return image, {"observation": row[0], "hash": digest, "captured": row[1]}

    def query(self, request, *, current=None, allowed=None, pin=None, native=False):
        started = time.perf_counter()
        if not isinstance(request, dict):
            raise ValueError("Invalid visual request")
        image, reference = self.source(request, current, allowed, pin)
        if pin and "observation" in reference:
            with self.store.lock, self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO visual_observation_pins VALUES(?,?)", (reference["observation"], pin))
        original_size = image.size
        image, box = checked_transform(image, request,reference)
        compare = request.get("compare")
        if compare is not None:
            if not isinstance(compare, dict):
                raise ValueError("Invalid visual comparison")
            other, other_reference = self.source(compare, current, allowed, pin)
            if pin and "observation" in other_reference:
                with self.store.lock, self.store.db:
                    self.store.db.execute("INSERT OR IGNORE INTO visual_observation_pins VALUES(?,?)", (other_reference["observation"], pin))
            other, _ = checked_transform(other, {**request, "box": compare.get("box", box)},other_reference)
            if image.mode != other.mode or image.size != other.size:
                raise ValueError("Comparison shape differs")
            delta = ImageChops.difference(image, other)
            similarity = 1 - sum(ImageStat.Stat(delta).mean)/(255*len(image.getbands()))
            image = delta
            reference = {"left": reference, "right": other_reference, "similarity": similarity}
        result = {**describe(image, request), "evidence": reference, "width": original_size[0],
                  "height": original_size[1], "box": box,
                  "coordinates": {"origin": box[:2], "scale": [(box[2]-box[0])/image.width, (box[3]-box[1])/image.height]},
                  "observed": time.time()}
        attachment = None
        if native and request.get("output") == "image":
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            attachment = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        elif request.get("output") == "image":
            result.update(describe(image, {**request, "output": "pixels"}))
            result["delivery"] = "pixels; native image input unavailable"
        result["cost_ms"] = round((time.perf_counter()-started)*1000, 3)
        if self.activity:
            # Publish exact provenance so historical regions are never overlaid on
            # an unrelated live frame. The GUI labels all attention requests.
            source_label = f"経験 {request['event']} {request.get('phase','before')}" if "event" in request else (
                f"観測 {reference.get('observation', '?')}" if "observation" in request else "現在画面")
            leaf = reference.get("left", reference)
            self.activity.update(attention=tuple(box), attention_source=source_label, attention_size=tuple(original_size),
                                 attention_reference=(leaf.get("event", 0), leaf.get("phase", "before"), leaf.get("observation", 0)))
            def label(ref):
                return f"experience={ref['event']}:{ref.get('phase','before')}" if "event" in ref else f"observation={ref.get('observation','?')}"
            identity = label(reference["left"])+","+label(reference["right"]) if compare else label(reference)
            self.activity.record("VISION" if compare else "OBS", f"{identity} crop={box} {image.width}x{image.height} {image.mode}")
        return result, attachment


class Analyzers:
    """Immutable declarative pipelines, scoped to an individual, with held-out adoption.

    Pipelines compose only the bounded Host operations above. No eval/import/native
    execution is possible. WAT can continue to implement arbitrary finite algorithms.
    """
    def __init__(self, eyes):
        self.eyes, self.store = eyes, eyes.store
        with self.store.lock, self.store.db:
            self.store.db.execute("""CREATE TABLE IF NOT EXISTS visual_analyzers(
                id TEXT PRIMARY KEY, definition TEXT, status TEXT, cutoff INTEGER,
                metrics TEXT, created REAL, uses INTEGER DEFAULT 0)""")

    def create(self, definition):
        if not isinstance(definition, dict):
            raise ValueError("Invalid analyzer")
        raw = json.dumps(definition, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(raw.encode()) > 8192 or not isinstance(definition.get("description"), str) or not 1 <= len(definition["description"]) <= 240:
            raise ValueError("Analyzer requires a short self description")
        query = definition.get("query")
        if not isinstance(query, dict) or any(k not in {"box", "size", "mode", "operations", "output", "threshold"} for k in query):
            raise ValueError("Analyzer must be a pure single-frame pipeline")
        if query.get("output") not in {"statistics", "histogram", "components"}:
            raise ValueError("Analyzer requires bounded feature output")
        # Validate with the requested source dimensions, without executing arbitrary code.
        if query.get("size") == "native":
            raise ValueError("Reusable analyzer needs a fixed output size")
        box = query.get("box", [0, 0, 32, 32])
        box_for(box, (16384, 16384))
        image, _ = transform(Image.new("L", (32, 32)), {**query, "box": [0, 0, 32, 32]})
        describe(image, query)
        identifier = hashlib.sha256(raw.encode()).hexdigest()
        rows = self.store.rows(1)
        with self.store.lock, self.store.db:
            self.store.reserve(len(raw.encode())+8192)
            self.store.db.execute("INSERT OR IGNORE INTO visual_analyzers(id,definition,status,cutoff,metrics,created) VALUES(?,?,?,?,?,?)",
                                  (identifier, raw, "candidate", rows[-1]["id"] if rows else 0, "{}", time.time()))
        self.trace("create", identifier)
        return identifier

    def get(self, identifier):
        if not isinstance(identifier, str) or len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("Invalid analyzer ID")
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM visual_analyzers WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Unknown analyzer")
        return {**dict(row), "definition": json.loads(row["definition"]), "metrics": json.loads(row["metrics"])}

    def list(self, offset=0, limit=64):
        integer(offset, 0, 1000000)
        integer(limit, 1, 64)
        with self.store.lock:
            ids = [r[0] for r in self.store.db.execute("SELECT id FROM visual_analyzers ORDER BY created DESC LIMIT ? OFFSET ?", (limit, offset))]
        return [self.get(i) for i in ids]

    def run(self, identifier, request, *, experimental=False, **kwargs):
        record = self.get(identifier)
        if record["status"] != "promoted" and not experimental:
            raise ValueError("Analyzer has not passed independent evaluation")
        definition = record["definition"]["query"]
        # Callers select evidence only; changing a pipeline requires a new immutable ID.
        source = {k: v for k, v in request.items() if k in {"event", "phase", "observation", "fresh"}}
        result, _ = self.eyes.query({**definition, **source}, **kwargs)
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE visual_analyzers SET uses=uses+1 WHERE id=?", (identifier,))
        return {**result, "analyzer": identifier, "status": record["status"]}

    @staticmethod
    def signature(result):
        # Coarse, meaning-free feature buckets for the independent probe classifier.
        if "mean" in result:
            return tuple(int(v)//32 for v in result["mean"])
        if "histogram" in result:
            values = result["histogram"]
            return tuple(sum(values[i:i+32])//max(1, result["size"][0]*result["size"][1]//8) for i in range(0, len(values), 32))
        return (min(32, result["component_count"]),)

    def evaluate(self, identifier, train_ids, test_ids):
        from .evaluation import assessment, sample, summarize
        if (not isinstance(train_ids, list) or not isinstance(test_ids, list) or
                not 8 <= len(train_ids) <= 64 or not 8 <= len(test_ids) <= 64 or
                any(type(v) is not int for v in train_ids+test_ids) or
                len(set(train_ids+test_ids)) != len(train_ids+test_ids)):
            raise ValueError("Evaluation requires disjoint 8..64 training and new test experiences")
        record = self.get(identifier)
        previous_test = record["metrics"].get("test", [])
        if record["status"] == "retired" or max(train_ids) > record["cutoff"] or min(test_ids) <= max([record["cutoff"], *previous_test]):
            raise ValueError("Held-out evidence must be newer than analyzer creation")
        started = time.perf_counter()
        self.trace("test", identifier, f"n={len(train_ids)+len(test_ids)}")
        reason = "visual-analyzer-"+identifier
        # Keep the source evidence readable throughout evaluation and for later review.
        with self.store.lock:
            placeholders = ",".join("?" for _ in train_ids+test_ids)
            rows = {r["id"]: dict(r) for r in self.store.db.execute(f"SELECT * FROM events WHERE state='complete' AND id IN ({placeholders})", train_ids+test_ids)}
            if len(rows) != len(train_ids)+len(test_ids):
                raise ValueError("Evaluation evidence unavailable")
            self.store.pin(train_ids+test_ids, reason)
        counts = {}; total = changed = 0
        deadline = started+30
        reproducible = True
        for event in sorted(train_ids):
            if getattr(self.eyes, "cancel", None) is not None and self.eyes.cancel.is_set():
                raise InterruptedError("Analyzer evaluation cancelled")
            if time.perf_counter() > deadline:
                raise ValueError("Analyzer evaluation exceeded 30 seconds")
            result = self.run(identifier, {"event": event}, experimental=True)
            key = (self.signature(result), rows[event]["action"])
            n, y = counts.get(key, (0, 0)); outcome = int(rows[event]["change"] > .01)
            counts[key] = (n+1, y+outcome)
            total += 1; changed += outcome
        base = []; candidate = []; unknown = 0
        for event in sorted(test_ids):
            if getattr(self.eyes, "cancel", None) is not None and self.eyes.cancel.is_set():
                raise InterruptedError("Analyzer evaluation cancelled")
            if time.perf_counter() > deadline:
                raise ValueError("Analyzer evaluation exceeded 30 seconds")
            result = self.run(identifier, {"event": event}, experimental=True)
            repeat = self.run(identifier, {"event": event}, experimental=True)
            reproducible &= self.signature(result) == self.signature(repeat)
            key = (self.signature(result), rows[event]["action"])
            n, y = counts.get(key, (0, 0))
            probability = y/n if n else changed/total
            unknown += not bool(n)
            outcome = int(rows[event]["change"] > .01)
            base.append(sample(event, round(changed/total*1000), outcome, rows[event]["action"]))
            candidate.append(sample(event, round(probability*1000) if n else -1, outcome, rows[event]["action"]))
            # Do not train on held-out outcomes; the analyzer cannot learn the test set.
        cost = (time.perf_counter()-started)/len(test_ids)
        baseline, measured = summarize(base, 0), summarize(candidate, cost)
        # The Agent evaluator's relative timing rule compares two complete WAT
        # programs. Here usefulness is a feature probe, so assess its predictions
        # separately and enforce the actual measured analyzer budget below.
        passed, explanation = assessment(baseline, {**measured, "seconds": 0})
        passed = passed and reproducible and cost <= 1 and unknown < len(test_ids)
        metrics = {"baseline": baseline, "candidate": measured, "reproducible": reproducible,
                   "unknown": unknown, "cost_seconds": cost, "reason": explanation,
                   "train": train_ids, "test": test_ids, "scope": "held-out change prediction probe; no game meaning validated"}
        status = "promoted" if passed else "rejected"
        with self.store.lock, self.store.db:
            self.store.reserve(len(json.dumps(metrics).encode())+4096)
            self.store.db.execute("UPDATE visual_analyzers SET status=?,metrics=? WHERE id=?", (status, json.dumps(metrics), identifier))
        self.trace("promote" if passed else "reject", identifier)
        return {"id": identifier, "status": status, "metrics": metrics}

    def retire(self, identifier):
        self.get(identifier)
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE visual_analyzers SET status='retired' WHERE id=?", (identifier,))
        self.trace("retire", identifier)
        return {"id": identifier, "status": "retired"}

    def graph(self):
        from .inspection import analyzer_nodes
        return analyzer_nodes(self.list())

    def trace(self, action, identifier, detail=""):
        if self.eyes.activity:
            self.eyes.activity.record("ANALYZER", f"{action} id={identifier[:16]} {detail}")


VISUAL_INSTRUCTIONS = """画面そのものが主要Evidenceです。色、形状、模様、領域、動き、記号、数字、文字列、UI、画像等の構造があり得ます。意味を事前に仮定せず、解析方法は自分で考えてください。画像からの解釈も未検証の仮説です。
情報不足ならJSONのvisual_requests配列（最大4件）で追加観測し、結果を見て再要求できます。各要求はop=observe、event=経験ID、phase=before/after/diff（既定before）。eventを省略すると現在画面、fresh=trueは次の新しい取得を待ちます。返るobservation IDで同じ画面を再要求できます。
box=[左,上,右,下]は原画面座標、size=[幅,高さ]（32/64/128等）またはnative、mode=L/RGB。最大262144画素。output=pixels/pixel/statistics/histogram/components/image。pixelsは行優先でRGBならR,G,Bの各値、Lなら1値を並べた配列で、offset/limitは画素数ではなく値の数（最大2048）。pixel_coverageのfirst/last_sampleは変換後の[x,y,色チャネル]で、complete_transformed_image=falseならbox全体を見たことにはならない。minimum_full_image_requestsと残り要求数を見て、必要なら小さい概観・統計・別の観測方法を自分で選ぶ。pixelは変換後point=[x,y]。返るcoordinatesで原画面へ戻せます。compare={event,phase,box}で差分と類似度。operations=[{op:grayscale/threshold/blur/edge/invert,...}]で意味を持たない処理を組み合わせられます。Vision入力確認済みの場合だけoutput=imageを画像として渡し、それ以外は同じ画像の画素を返します。
op=analyzer_create,definition={description,query:{box,size,mode,operations,output:statistics/histogram/components}}で安全な解析器を生成・保存し、op=analyzer_run,id,event,experimental=trueで試験。op=analyzer_evaluate,id,train:[経験ID],test:[経験ID]は各8..64件、trainは作成以前、testは作成後の独立経験。再現性・予測寄与・未知率・誤反応・コストを評価し合格後だけ通常利用可。op=analyzer_list,cursor（返るnext_cursorでページ取得）、op=analyzer_get,idで定義を取得、op=analyzer_retire,idで再利用・比較・廃止。op=evidence_keep,observation,reasonで重要な追加画面を保存保護。変更は新定義から新IDを作成。WATとこれらの演算や作業領域を組み合わせられます。
op=library_request,name,purpose,reasonでHostへ取得を要求可能。信頼元・ライセンス・ハッシュ・署名・Runtime検証済みの資源だけが利用対象で、AgentへネットワークやNative実行権限はありません。
"""


class VisualAccess:
    """Single broker used by both research JSON and the Agent request ABI."""
    def __init__(self, eyes):
        self.eyes = eyes
        self.analyzers = Analyzers(eyes)

    def handle(self, request, *, pin=None, native=False, current=None, allowed=None):
        if not isinstance(request, dict):
            raise ValueError("Visual request must be an object")
        op = request.get("op", "observe")
        if op == "observe":
            return self.eyes.query(request, pin=pin, native=native, current=current, allowed=allowed)
        if op == "analyzer_create":
            return {"id": self.analyzers.create(request["definition"]), "status": "candidate"}, None
        if op == "analyzer_list":
            # Keep Agent responses under the capability ABI budget; full metrics are
            # in the individual's DB, not repeatedly sent with every inventory.
            cursor = integer(request.get("cursor", 0), 0, 1000000)
            rows = self.analyzers.list(cursor, 9)
            return {"analyzers": [{"id": r["id"], "status": r["status"], "description": r["definition"]["description"], "cutoff": r["cutoff"]} for r in rows[:8]],
                    "next_cursor": cursor+8 if len(rows) > 8 else None}, None
        if op == "analyzer_get":
            row = self.analyzers.get(request["id"])
            return {k: row[k] for k in ("id", "status", "definition", "cutoff")}, None
        if op == "analyzer_run":
            return self.analyzers.run(request["id"], request, experimental=request.get("experimental") is True,
                                      pin=pin, current=current, allowed=allowed), None
        if op == "analyzer_evaluate":
            return evaluation_summary(self.analyzers.evaluate(request["id"], request["train"], request["test"])), None
        if op == "analyzer_retire":
            return self.analyzers.retire(request["id"]), None
        if op == "evidence_keep":
            observation = integer(request["observation"], 1, 2**63-1)
            self.eyes.source({"observation": observation})
            reason = request.get("reason")
            if not isinstance(reason, str) or not 1 <= len(reason) <= 240:
                raise ValueError("Evidence retention requires a short reason")
            with self.eyes.store.lock, self.eyes.store.db:
                self.eyes.store.reserve(4096)
                self.eyes.store.db.execute("INSERT OR IGNORE INTO visual_observation_pins VALUES(?,?)", (observation, "kept:"+reason))
            return {"observation": observation, "retained": True}, None
        if op == "library_request":
            fields = {}
            for key in ("name", "purpose", "reason"):
                value = request.get(key)
                if not isinstance(value, str) or not 1 <= len(value) <= 500:
                    raise ValueError("Library request requires name, purpose, reason")
                fields[key] = value
            # A request is a durable acquisition proposal. Only the Host can
            # verify a signed catalog and acquire a declarative library.
            with self.eyes.store.lock, self.eyes.store.db:
                self.eyes.store.reserve(8192)
                self.eyes.store.db.execute("INSERT INTO visual_library_requests(request,state,created) VALUES(?,?,?)",
                                          (json.dumps(fields, ensure_ascii=False), "awaiting_verified_catalog", time.time()))
            if self.eyes.activity:
                self.eyes.activity.record("LIBRARY", "request "+fields["name"])
            from .visual_libraries import acquire
            result = acquire(self.eyes.store, fields, self.analyzers)
            with self.eyes.store.lock, self.eyes.store.db:
                self.eyes.store.db.execute("UPDATE visual_library_requests SET state=? WHERE id=(SELECT max(id) FROM visual_library_requests)", (result["state"],))
            return result, None
        raise ValueError("Unknown visual capability")


VISUAL_OPS = {"observe", "analyzer_create", "analyzer_list", "analyzer_run", "analyzer_evaluate",
              "analyzer_retire", "analyzer_get", "library_request", "evidence_keep"}


class ReplayEyes(Eyes):
    """Replay can inspect current Before and previously revealed evidence only."""
    def __init__(self, evidence, rows):
        self.evidence = evidence
        self.rows = {r["id"]: r for r in rows}
        self.revealed = set()
        self.current_id = None
        self.frame = None
        self.activity = None

    def source(self, request, current=None, allowed=None, pin=None):
        if "observation" in request or request.get("fresh"):
            raise ValueError("Capture IDs and fresh I/O are unavailable in Replay")
        event = request.get("event")
        phase = request.get("phase", "before")
        if phase not in {"before", "after", "diff"}:
            raise ValueError("Invalid Replay phase")
        if event is None:
            if phase != "before" or self.frame is None:
                raise ValueError("Future image is not available")
            return self.frame.copy(), {"event": self.current_id, "phase": "before", "replay": True}
        integer(event, 1, 2**63-1)
        if event not in self.revealed and not (event == self.current_id and phase == "before"):
            raise ValueError("Future evidence is not available")
        row = self.rows[event]
        def load(key):
            digest = row[key]
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Invalid evidence digest")
            with Image.open(self.evidence/(digest+".png")) as image:
                return image.convert("RGB")
        before = load("before_hash")
        image = before if phase == "before" else load("after_hash")
        if phase == "diff":
            if before.size != image.size:
                raise ValueError("Evidence dimensions differ")
            image = ImageChops.difference(before, image)
        return image, {"event": event, "phase": phase, "replay": True}

    def dispatch(self, request):
        if request.get("op") != "analyzer_run":
            raise ValueError("Side effects are unavailable in Replay")
        # Only immutable, already evaluated definitions can affect replayed actions.
        import sqlite3
        identifier = request.get("id")
        if not isinstance(identifier, str) or len(identifier) != 64:
            raise ValueError("Invalid analyzer ID")
        db_path = self.evidence.parent/"host"/"experience.sqlite3"
        with sqlite3.connect(db_path.as_uri()+"?mode=ro", uri=True) as db:
            try:
                row = db.execute("SELECT definition,status FROM visual_analyzers WHERE id=?", (identifier,)).fetchone()
            except sqlite3.OperationalError:
                row = None
        if row is None or row[1] != "promoted":
            raise ValueError("Analyzer is not available for Replay")
        definition = json.loads(row[0])["query"]
        source = {k: v for k, v in request.items() if k in {"event", "phase", "fresh", "observation"}}
        result, _ = self.query({**definition, **source})
        return {**result, "analyzer": identifier, "status": "promoted"}


class ReplayFiles:
    def deny(self, *args, **kwargs):
        raise ValueError("Workspace side effects are unavailable in Replay")
    read = write = delete = mkdir = list = deny
