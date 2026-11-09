#!/usr/bin/env python3
"""Recognize one local challenge. stdout is a single JSON object."""
import argparse
from fractions import Fraction
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parent.parent
TAC = {"slider": "SLIDER", "rotate": "ROTATE", "concat": "CONCAT", "ordered_click": "WORD_IMAGE_CLICK"}
KINDS = ("digits", "letters", "alphanumeric", "chinese", "arithmetic",
         "ordered_fill", "ordered_click", "slider", "rotate", "concat")
PATTERNS = {
    "digits": r"[0-9]+", "letters": r"[A-Za-z]+",
    "alphanumeric": r"[A-Za-z0-9]+", "chinese": r"[\u3400-\u9fff]+",
}


class NeedsHuman(Exception):
    pass


def local_image(request, key, base):
    if key not in request:
        raise ValueError(f"missing {key}")
    path = base / request[key]
    with path.open("rb") as source:
        content = source.read(8 * 1024 * 1024 + 1)
    if not content or len(content) > 8 * 1024 * 1024:
        raise ValueError(f"{key} must be between 1 byte and 8 MiB")
    return content


def decode(content, grayscale=False):
    import cv2
    import numpy as np
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(content)) as header:
        if header.width * header.height > 4_000_000:
            raise ValueError("image exceeds 4 megapixels")
    flag = cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_COLOR
    img = cv2.imdecode(np.frombuffer(content, np.uint8), flag)
    if img is None:
        raise ValueError("image cannot be decoded")
    return img


@lru_cache
def engine(name):
    if name == "ddddocr":
        from ddddocr import DdddOcr
        return DdddOcr(show_ad=False)
    from rapidocr import RapidOCR
    return RapidOCR(params={
        "Global.use_det": False, "Global.use_cls": False, "Global.log_level": "warning",
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })


def read_text(image, name):
    if name == "ddddocr":
        import cv2
        _, encoded = cv2.imencode(".png", image)
        return engine(name).classification(encoded.tobytes()).strip(), None
    result = engine(name)(image, use_det=False, use_cls=False)
    if not result.txts:
        return "", None
    return result.txts[0].strip(), float(result.scores[0])


def normalize(text):
    return unicodedata.normalize("NFKC", text).strip()


def arithmetic(text, fmt):
    if fmt not in {"integer", "fraction"}:
        raise ValueError("division_format must be integer or fraction")
    text = normalize(text).translate(str.maketrans({"×": "*", "x": "*", "X": "*", "÷": "/", "−": "-"}))
    match = re.fullmatch(r"\s*(-?[0-9]{1,6})\s*([+*/-])\s*(-?[0-9]{1,6})\s*(?:=\s*\??|\?)?\s*", text)
    if not match:
        raise NeedsHuman("算式不完整或存在额外字符")
    a, op, b = match.groups()
    a, b = int(a), int(b)
    if op == "/" and not b:
        raise NeedsHuman("除数为零")
    if op == "/":
        result = Fraction(a, b)
        if result.denominator != 1 and fmt == "integer":
            raise NeedsHuman("除法结果非整数，需要明确答案格式")
    else:
        result = {"+": a+b, "-": a-b, "*": a*b}[op]
    return str(result)


def generic(kind, req, base):
    img = decode(local_image(req, "image", base))
    if kind in PATTERNS or kind == "arithmetic":
        name = "ddddocr" if kind == "digits" else "rapidocr"
        text, score = read_text(img, name)
        text = normalize(text)
        if kind == "arithmetic":
            return {"text": arithmetic(text, req.get("division_format", "integer")), "expression": text}, score, name
        length = req.get("length")
        if length is not None and (type(length) is not int or length < 1):
            raise ValueError("length must be a positive integer")
        if not re.fullmatch(PATTERNS[kind], text) or (length is not None and len(text) != length):
            raise NeedsHuman("识别文本不符合字符集或长度要求")
        return {"text": text}, score, name
    if kind in ("ordered_fill", "ordered_click"):
        boxes = req["boxes"]
        if not isinstance(boxes, list) or not 1 <= len(boxes) <= 24:
            raise ValueError("boxes must contain 1–24 bounding boxes")
        h, w = img.shape[:2]
        labels = []
        for box in boxes:
            if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
                raise ValueError("boxes must contain integer [x1,y1,x2,y2] coordinates")
            x1, y1, x2, y2 = box
            if not (0 <= x1 < x2 <= w and 0 <= y1 < y2 <= h):
                raise ValueError("box is outside the image")
            label, _ = read_text(img[y1:y2, x1:x2], "rapidocr")
            labels.append(normalize(label))
        if kind == "ordered_fill":
            order = req["order"]
            if not isinstance(order, list) or any(type(v) is not int for v in order) or sorted(order) != list(range(len(boxes))):
                raise ValueError("order must be a zero-based permutation of boxes")
            if not all(len(labels[i]) == 1 for i in order):
                raise NeedsHuman("存在未能唯一识别的字格")
            return {"text": "".join(labels[i] for i in order)}, None, "rapidocr"
        cue = req["cue"]
        if not isinstance(cue, str) or not cue or len(set(cue)) != len(cue):
            raise NeedsHuman("点选提示为空或含无法区分先后的重复目标")
        if any(labels.count(char) != 1 for char in cue):
            raise NeedsHuman("目标缺失或位置不唯一")
        points = [[(boxes[i][0]+boxes[i][2])/2, (boxes[i][1]+boxes[i][3])/2]
                  for i in map(labels.index, cue)]
        return {"points": points, "coordinate_space": "image_px"}, None, "rapidocr"
    if kind == "slider":
        import cv2
        import numpy as np
        clean = decode(local_image(req, "reference", base))
        if clean.shape != img.shape:
            raise ValueError("slider requires clean and gapped images with identical geometry")
        delta = cv2.cvtColor(cv2.absdiff(img, clean), cv2.COLOR_BGR2GRAY)
        mask = cv2.morphologyEx((delta > 30).astype(np.uint8), cv2.MORPH_OPEN, np.ones((3,3), np.uint8))
        count, _, stats, centers = cv2.connectedComponentsWithStats(mask)
        ranks = sorted(range(1, count), key=lambda i: stats[i, 4], reverse=True)
        if not ranks or stats[ranks[0], 4] < 16 or (len(ranks) > 1 and stats[ranks[1], 4] >= stats[ranks[0], 4]*0.65):
            raise NeedsHuman("没有唯一且明显的缺口")
        return {"gap_center": centers[ranks[0]].tolist(), "coordinate_space": "image_px"}, None, "image_difference"
    if kind == "rotate":
        return generic_rotation(img, req, base), None, "rotation_alignment"
    raise NeedsHuman("该题型需要站点适配配置")


def generic_rotation(img, req, base):
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if "reference" in req:
        reference = decode(local_image(req, "reference", base), grayscale=True)
        if reference.shape != gray.shape or h != w:
            raise ValueError("reference rotation requires same-size square images")
        yy, xx = np.indices((h, w))
        cx, cy = (w-1)/2, (h-1)/2
        crop = (xx-cx)**2 + (yy-cy)**2 <= (min(h,w)*0.43)**2
        target = reference[crop].astype(float)
        scores = [np.mean((cv2.warpAffine(gray, cv2.getRotationMatrix2D((cx,cy), -a, 1), (w,h))[crop]-target)**2)
                  for a in range(0,360,3)]
        step = 3
    else:
        cx, cy, radius = map(float, req["ring"])
        if not all(map(math.isfinite, (cx,cy,radius))) or radius < 8 or radius+3 >= min(cx,cy,w-1-cx,h-1-cy):
            raise ValueError("ring must be [center_x,center_y,radius] inside image")
        theta = np.arange(360) * np.pi/180
        def trace(r):
            x = (cx + r*np.cos(theta)).astype(np.float32)[None]
            y = (cy + r*np.sin(theta)).astype(np.float32)[None]
            return cv2.remap(gray,x,y,cv2.INTER_LINEAR).ravel().astype(float)
        inner, outer = trace(radius-2), trace(radius+2)
        scores = [np.mean((np.roll(inner,a)-outer)**2) for a in range(360)]
        step = 1
    best = int(np.argmin(scores))
    distinct = [s for i,s in enumerate(scores) if min(abs(i-best), len(scores)-abs(i-best))*step >= 12]
    if not distinct or min(distinct)-scores[best] < max(1, scores[best]*0.07):
        raise NeedsHuman("存在多个相近的旋转解")
    return {"clockwise_degrees": (best*step)%360}


def tac(kind, req, base):
    import tac_engine as frozen
    if kind not in TAC:
        raise ValueError("unsupported type for tac")
    local_reference = ROOT / "assets" / "tac-reference"
    if local_reference.is_dir():
        os.environ.setdefault("REFERENCE_DIR", str(local_reference))
    if kind == "ordered_click":
        frozen.V4_MODEL = str(ROOT / "assets" / "ch_v4_rec_server.onnx")
        frozen.V4_KEYS = str(ROOT / "assets" / "ppocr_keys_v1.txt")
        if not all(Path(name).is_file() for name in (frozen.V4_MODEL, frozen.V4_KEYS)):
            raise ValueError("TAC word-click model assets are missing; see skill reference")
        if frozen.load_engines()["rec"] is None:
            raise ValueError("TAC word-click model failed to initialize")
    bg = local_image(req, "image", base)
    tpl = local_image(req, "template", base) if kind != "concat" else b""
    decode(bg)
    if tpl:
        decode(tpl)
    row = req.get("row_hint")
    if row is not None and (type(row) is not int or row <= 0):
        raise ValueError("row_hint must be a positive integer")
    result = frozen.solve(bg, tpl, TAC[kind], {"randomY":row})
    if result["需人工处理"] or not result["动作"]:
        raise NeedsHuman(result["判定理由"])
    act = result["动作"]
    if kind == "ordered_click":
        candidate = {"points":act["点序"], "coordinate_space":"tac_widget_300x180"}
    else:
        distance = act["拖动距离"]
        if not math.isfinite(distance) or not 0 <= distance <= act["轨道上限"]:
            raise NeedsHuman("拖动距离超出控件范围")
        candidate = {"drag_px":distance, "coordinate_space":"tac_widget_300x180"}
    return candidate, result["置信度"], "tac_frozen", result["判定理由"]


def recognize(req, base):
    if not isinstance(req, dict):
        raise ValueError("request must be a JSON object")
    kind = req.get("type")
    profile = req.get("profile", "generic")
    out = {"schema_version":1,"type":kind,"profile":profile,"site_verified":False,"answer":None}
    if kind not in KINDS or profile not in ("generic", "tac"):
        return out | {"status":"error","reason":"unsupported type or profile"}
    try:
        if profile == "tac":
            answer, score, model, reason = tac(kind, req, base)
        else:
            answer, score, model = generic(kind, req, base)
            reason = None
        return out | {"status":"candidate","answer":answer,"model":model,"score":score,"reason":reason}
    except NeedsHuman as error:
        return out | {"status":"needs_human","reason":str(error)}
    except Exception as error:
        return out | {"status":"error","reason":f"{type(error).__name__}: {error}"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="JSON manifest path, or - for stdin")
    args = parser.parse_args()
    try:
        if args.request == "-":
            req, base = json.load(sys.stdin), Path.cwd()
        else:
            path = Path(args.request).resolve()
            req, base = json.loads(path.read_text(encoding="utf-8")), path.parent
        out = recognize(req, base)
    except Exception as error:
        out = {"schema_version":1,"status":"error","site_verified":False,"answer":None,"reason":str(error)}
    print(json.dumps(out, ensure_ascii=False, allow_nan=False))
    return 2 if out["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
