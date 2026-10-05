"""
Oval Blur Studio — local video editor.

Pick a video, step through it frame by frame, draw ovals over anything you want
hidden, then export a copy of the video with those regions blurred.
"""
import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

BASE = Path(__file__).resolve().parent
WORKSPACE = BASE / "workspace"
WORKSPACE.mkdir(exist_ok=True)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv", ".ts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS


def is_image(path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024 * 1024  # 16 GB uploads


# --------------------------------------------------------------------------
# project storage
# --------------------------------------------------------------------------

def project_dir(pid: str) -> Path:
    if not pid or "/" in pid or "\\" in pid or ".." in pid:
        abort(400, "bad project id")
    d = WORKSPACE / pid
    if not d.is_dir():
        abort(404, "project not found")
    return d


def ensure_media(p: dict) -> dict:
    """Projects made before stills were supported carry no media field."""
    meta = p.setdefault("meta", {})
    if not meta.get("media"):
        meta["media"] = "image" if is_image(p.get("source", "")) else "video"
    return p


def load_project(pid: str) -> dict:
    d = project_dir(pid)
    with open(d / "project.json") as fh:
        return ensure_media(json.load(fh))


def save_project(pid: str, data: dict) -> None:
    d = project_dir(pid)
    tmp = d / "project.json.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    tmp.replace(d / "project.json")


def probe(path: Path) -> dict:
    """Measure the media. A still image is treated as a clip of one frame."""
    if is_image(path):
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("could not read that image — unsupported or corrupt file")
        h, w = img.shape[:2]
        return {"media": "image", "width": int(w), "height": int(h), "fps": 1.0,
                "frames": 1, "duration": 0.0, "has_audio": False,
                "codec": Path(path).suffix.lstrip(".").lower()}

    meta = {"media": "video", "width": 0, "height": 0, "fps": 0.0, "frames": 0,
            "duration": 0.0, "has_audio": False}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        info = json.loads(out.stdout or "{}")
        for s in info.get("streams", []):
            if s.get("codec_type") == "audio":
                meta["has_audio"] = True
            elif s.get("codec_type") == "video" and not meta["width"]:
                meta["width"] = int(s.get("width") or 0)
                meta["height"] = int(s.get("height") or 0)
                num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
                try:
                    if float(den or 1) != 0:
                        meta["fps"] = float(num) / float(den or 1)
                except ValueError:
                    pass
                if s.get("nb_frames"):
                    meta["frames"] = int(s["nb_frames"])
                meta["codec"] = s.get("codec_name", "")
        dur = (info.get("format") or {}).get("duration")
        if dur:
            meta["duration"] = float(dur)
    except Exception:
        pass

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        raise ValueError("could not open video — unsupported or corrupt file")
    if not meta["width"]:
        meta["width"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        meta["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if not meta["fps"] or meta["fps"] <= 0:
        meta["fps"] = float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
    cv_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()

    if cv_frames > 0:
        meta["frames"] = cv_frames
    if not meta["frames"] and meta["duration"]:
        meta["frames"] = int(round(meta["duration"] * meta["fps"]))
    if not meta["duration"] and meta["frames"]:
        meta["duration"] = meta["frames"] / meta["fps"]
    if meta["frames"] < 1:
        raise ValueError("video reports zero frames")
    return meta


# --------------------------------------------------------------------------
# frame reading
# --------------------------------------------------------------------------

class Reader:
    """A cv2 capture that remembers where it is, so stepping forward is cheap."""

    SEEK_AHEAD = 24  # grab-forward rather than seek when the target is this close

    def __init__(self, path: Path):
        self.path = str(path)
        self.cap = cv2.VideoCapture(self.path)
        self.pos = 0
        self.lock = threading.Lock()
        self.touched = time.time()

    def frame(self, n: int):
        with self.lock:
            self.touched = time.time()
            if not self.cap.isOpened():
                self.cap = cv2.VideoCapture(self.path)
                self.pos = 0
            if n < self.pos or n > self.pos + self.SEEK_AHEAD:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, n)
                self.pos = n
            while self.pos < n:
                if not self.cap.grab():
                    return None
                self.pos += 1
            ok, img = self.cap.read()
            if not ok:
                return None
            self.pos = n + 1
            return img

    def close(self):
        with self.lock:
            self.cap.release()


class StillReader:
    """Stands in for Reader when the source is a single image."""

    def __init__(self, path: Path):
        self.img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        self.touched = time.time()

    def frame(self, n: int):
        self.touched = time.time()
        return None if self.img is None else self.img.copy()

    def close(self):
        self.img = None


_readers: dict[str, Reader] = {}
_readers_lock = threading.Lock()


def get_reader(pid: str, path: Path) -> Reader:
    with _readers_lock:
        r = _readers.get(pid)
        if r is None:
            r = StillReader(path) if is_image(path) else Reader(path)
            _readers[pid] = r
        # drop readers nobody has touched in 10 minutes
        for key in [k for k, v in _readers.items() if k != pid and time.time() - v.touched > 600]:
            _readers.pop(key).close()
        return r


# --------------------------------------------------------------------------
# shape maths + blur
# --------------------------------------------------------------------------

def shape_at(shape: dict, frame: int):
    """Resolve a shape's geometry on a given frame, or None if it is not active."""
    if frame < int(shape.get("start", 0)) or frame > int(shape.get("end", 0)):
        return None
    keys = sorted(shape.get("keys") or [], key=lambda k: k["f"])
    if not keys:
        return None
    if len(keys) == 1 or frame <= keys[0]["f"]:
        k = keys[0]
        return (k["cx"], k["cy"], k["rx"], k["ry"], k.get("ang", 0.0))
    if frame >= keys[-1]["f"]:
        k = keys[-1]
        return (k["cx"], k["cy"], k["rx"], k["ry"], k.get("ang", 0.0))
    for a, b in zip(keys, keys[1:]):
        if a["f"] <= frame <= b["f"]:
            span = b["f"] - a["f"]
            t = 0.0 if span == 0 else (frame - a["f"]) / span
            ang_a, ang_b = a.get("ang", 0.0), b.get("ang", 0.0)
            # take the short way round when interpolating rotation
            d = ((ang_b - ang_a + 180) % 360) - 180
            return (
                a["cx"] + (b["cx"] - a["cx"]) * t,
                a["cy"] + (b["cy"] - a["cy"]) * t,
                a["rx"] + (b["rx"] - a["rx"]) * t,
                a["ry"] + (b["ry"] - a["ry"]) * t,
                ang_a + d * t,
            )
    return None


def _odd(v: int) -> int:
    v = int(max(3, v))
    return v if v % 2 == 1 else v + 1


def obscure(frame: np.ndarray, shape: dict, geom) -> None:
    """Blur/pixelate/black out one oval or rectangular region of `frame`, in place."""
    h, w = frame.shape[:2]
    cx, cy, rx, ry, ang = geom
    rx, ry = max(1.0, float(rx)), max(1.0, float(ry))
    kind = shape.get("kind", "oval")

    feather_pct = float(shape.get("feather", 12))
    feather = max(0.0, min(rx, ry) * feather_pct / 100.0)
    # a turned rectangle reaches to its corners, further than either half-axis
    reach = float(np.hypot(rx, ry)) if kind == "rect" else max(rx, ry)
    pad = int(np.ceil(reach + feather * 3 + 4))

    x0, y0 = max(0, int(cx) - pad), max(0, int(cy) - pad)
    x1, y1 = min(w, int(cx) + pad + 1), min(h, int(cy) + pad + 1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return

    roi = frame[y0:y1, x0:x1]
    rh, rw = roi.shape[:2]
    mode = shape.get("mode", "blur")
    strength = float(shape.get("strength", 45))
    # strength is a percentage of the oval's short axis, so it looks the same
    # whether the oval is tiny or huge
    unit = max(2.0, min(rx, ry) * 2.0)

    if mode == "black":
        cover = np.zeros_like(roi)
    elif mode == "pixelate":
        block = max(2, int(unit * strength / 100.0 * 0.45))
        sw, sh = max(1, rw // block), max(1, rh // block)
        small = cv2.resize(roi, (sw, sh), interpolation=cv2.INTER_AREA)
        cover = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_NEAREST)
    else:
        k = _odd(unit * strength / 100.0 * 0.9)
        k = min(k, _odd(min(rw, rh)))
        cover = cv2.GaussianBlur(roi, (k, k), 0)
        # a second pass makes the blur genuinely unreadable rather than smeared
        cover = cv2.GaussianBlur(cover, (k, k), 0)

    mask = np.zeros((rh, rw), np.uint8)
    if kind == "rect":
        box = cv2.boxPoints(((float(cx - x0), float(cy - y0)),
                             (float(rx * 2), float(ry * 2)), float(ang)))
        cv2.fillConvexPoly(mask, np.int32(np.round(box)), 255)
    else:
        cv2.ellipse(mask, (int(round(cx - x0)), int(round(cy - y0))),
                    (int(round(rx)), int(round(ry))), float(ang), 0, 360, 255, -1)
    if feather >= 1:
        fk = _odd(feather * 2)
        mask = cv2.GaussianBlur(mask, (fk, fk), 0)

    alpha = (mask.astype(np.float32) / 255.0)[:, :, None]
    roi[:] = (cover.astype(np.float32) * alpha + roi.astype(np.float32) * (1 - alpha)).astype(np.uint8)


def render(frame: np.ndarray, shapes: list, n: int) -> np.ndarray:
    for s in shapes:
        if not s.get("enabled", True):
            continue
        geom = shape_at(s, n)
        if geom:
            obscure(frame, s, geom)
    return frame


# --------------------------------------------------------------------------
# following an object through the clip
# --------------------------------------------------------------------------

class FlowTracker:
    """Median flow. A grid of points inside the box is pushed forward with
    Lucas-Kanade and then back again; the points that come home are the ones we
    trust, and their median shift and spread give the new box. Built on the
    plain OpenCV build, and it says so honestly when it loses the object."""

    LK = dict(winSize=(21, 21), maxLevel=3,
              criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
    GRID = 11
    MIN_POINTS = 8
    MAX_FB = 12.0          # px of forward-backward error before we call it lost

    def __init__(self):
        self.prev = None
        self.box = None

    def init(self, frame, box):
        self.prev = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self.box = tuple(float(v) for v in box)
        return True

    def _grid(self):
        x, y, w, h = self.box
        mx, my = w * 0.08, h * 0.08
        xs = np.linspace(x + mx, x + w - mx, self.GRID)
        ys = np.linspace(y + my, y + h - my, self.GRID)
        return np.array([[a, b] for b in ys for a in xs], np.float32).reshape(-1, 1, 2)

    def update(self, frame):
        if self.prev is None:
            return False, self.box
        cur = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        p0 = self._grid()
        p1, s1, _ = cv2.calcOpticalFlowPyrLK(self.prev, cur, p0, None, **self.LK)
        if p1 is None:
            return False, self.box
        back, s2, _ = cv2.calcOpticalFlowPyrLK(cur, self.prev, p1, None, **self.LK)
        if back is None:
            return False, self.box

        ok = (s1.ravel() == 1) & (s2.ravel() == 1)
        fb = np.linalg.norm((p0 - back).reshape(-1, 2), axis=1)
        if ok.sum() < self.MIN_POINTS:
            return False, self.box
        keep = ok & (fb <= max(1.0, float(np.median(fb[ok]))))
        if keep.sum() < self.MIN_POINTS:
            keep = ok
        if float(np.median(fb[keep])) > self.MAX_FB:
            return False, self.box

        a = p0.reshape(-1, 2)[keep]
        b = p1.reshape(-1, 2)[keep]
        dx = float(np.median(b[:, 0] - a[:, 0]))
        dy = float(np.median(b[:, 1] - a[:, 1]))

        scale = 1.0
        if len(a) >= 4:                      # every pair of points votes on scale
            ia, ib = np.triu_indices(len(a), 1)
            d0 = np.linalg.norm(a[ia] - a[ib], axis=1)
            d1 = np.linalg.norm(b[ia] - b[ib], axis=1)
            m = d0 > 1e-3
            if m.any():
                scale = float(np.median(d1[m] / d0[m]))
        scale = min(max(scale, 0.85), 1.18)  # no wild jumps between frames

        x, y, w, h = self.box
        cx, cy = x + w / 2 + dx, y + h / 2 + dy
        w, h = w * scale, h * scale
        self.box = (cx - w / 2, cy - h / 2, w, h)
        self.prev = cur
        return True, self.box


class BoxTracker:
    """Wraps an OpenCV tracker, which wants whole-pixel boxes."""

    def __init__(self, factory):
        self.t = factory()

    def init(self, frame, box):
        return self.t.init(frame, tuple(int(round(v)) for v in box))

    def update(self, frame):
        return self.t.update(frame)


def tracker_catalogue() -> list:
    """Which trackers this OpenCV build can actually offer."""
    out = [{"id": "flow", "label": "Motion",
            "note": "follows how the picture moves — quick, and good with "
                    "something that drifts or changes size"}]
    if hasattr(cv2, "TrackerCSRT_create"):
        out.insert(0, {"id": "csrt", "label": "Accurate",
                       "note": "slower, but holds on through clutter"})
    if hasattr(cv2, "TrackerKCF_create"):
        out.append({"id": "kcf", "label": "Quick", "note": "fast, struggles when the size changes"})
    if hasattr(cv2, "TrackerMIL_create"):
        out.append({"id": "mil", "label": "Appearance",
                    "note": "learns how the thing looks — steadier when it turns, "
                            "but it will not resize"})
    return out


def make_tracker(kind: str):
    if kind == "csrt" and hasattr(cv2, "TrackerCSRT_create"):
        return BoxTracker(cv2.TrackerCSRT_create)
    if kind == "kcf" and hasattr(cv2, "TrackerKCF_create"):
        return BoxTracker(cv2.TrackerKCF_create)
    if kind == "mil" and hasattr(cv2, "TrackerMIL_create"):
        return BoxTracker(cv2.TrackerMIL_create)
    return FlowTracker()


def half_extent(kind: str, rx: float, ry: float, ang: float):
    """Half width and height of the box that just contains the shape."""
    a = math.radians(ang)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    if kind == "rect":
        return rx * c + ry * s, rx * s + ry * c
    return math.hypot(rx * c, ry * s), math.hypot(rx * s, ry * c)


def simplify_keys(keys: list, tol: float = 0.75, run: int = 60) -> list:
    """Drop positions that a straight line between their neighbours already
    predicts to within `tol` pixels, so a long follow stays a small file."""
    if len(keys) <= 2:
        return list(keys)

    def fits(a: int, b: int) -> bool:
        ka, kb = keys[a], keys[b]
        span = kb["f"] - ka["f"]
        if span <= 0:
            return False
        for m in range(a + 1, b):
            km = keys[m]
            t = (km["f"] - ka["f"]) / span
            for fld in ("cx", "cy", "rx", "ry"):
                if abs(ka[fld] + (kb[fld] - ka[fld]) * t - km[fld]) > tol:
                    return False
        return True

    out = [keys[0]]
    a, b = 0, 2
    while b < len(keys):
        if b - a <= run and fits(a, b):
            b += 1
        else:
            out.append(keys[b - 1])
            a = b - 1
            b = a + 2
    out.append(keys[-1])
    return out


def run_track(job_id: str, project: dict, req: dict):
    job = JOBS[job_id]
    shape = req.get("shape") or {}
    kind = shape.get("kind", "oval")
    total = project["meta"]["frames"]
    f0 = max(0, min(int(req.get("from", 0)), total - 1))
    f1 = max(f0, min(int(req.get("to", total - 1)), total - 1))
    count = max(1, f1 - f0)

    geom = shape_at(shape, f0)
    if geom is None:
        ks = sorted(shape.get("keys") or [], key=lambda k: k["f"])
        if not ks:
            job.update(state="error", note="that shape has no position to start from")
            return
        k = min(ks, key=lambda k: abs(k["f"] - f0))
        geom = (k["cx"], k["cy"], k["rx"], k["ry"], k.get("ang", 0.0))
    cx, cy, rx, ry, ang = geom

    hx, hy = half_extent(kind, rx, ry, ang)
    if hx < 2 or hy < 2:
        job.update(state="error", note="that shape is too small to follow")
        return
    # keep the shape's size relative to its box, so a turned shape stays its size
    ratio_x, ratio_y = rx / hx, ry / hy

    W, H = project["meta"]["width"], project["meta"]["height"]
    reader = Reader(Path(project["source"]))
    try:
        first = reader.frame(f0)
        if first is None:
            job.update(state="error", note=f"could not read frame {f0}")
            return
        x0 = max(0.0, cx - hx)
        y0 = max(0.0, cy - hy)
        box = (x0, y0, min(hx * 2, W - x0), min(hy * 2, H - y0))
        if box[2] < 4 or box[3] < 4:
            job.update(state="error", note="that shape is too small to follow")
            return

        tracker = make_tracker(req.get("tracker", "flow"))
        try:
            tracker.init(first, box)
        except Exception as e:
            job.update(state="error", note=f"could not start following: {e}")
            return

        keys = [{"f": f0, "cx": cx, "cy": cy, "rx": rx, "ry": ry, "ang": ang}]
        job.update(state="running", progress=0.0, frame=0, total=count,
                   note="following", reason="")
        t0 = time.time()
        reason = "reached the end of the clip"

        for n in range(f0 + 1, f1 + 1):
            if job.get("cancel"):
                reason = "stopped"
                break
            img = reader.frame(n)
            if img is None:
                reason = f"could not read frame {n}"
                break
            try:
                ok, b = tracker.update(img)
            except Exception:
                ok, b = False, None
            if not ok or b is None:
                reason = f"lost it at frame {n}"
                break
            x, y, w, h = (float(v) for v in b)
            ncx, ncy = x + w / 2, y + h / 2
            if w < 4 or h < 4:
                reason = f"it got too small at frame {n}"
                break
            if not (-w <= ncx <= W + w and -h <= ncy <= H + h):
                reason = f"it left the picture at frame {n}"
                break
            keys.append({"f": n, "cx": ncx, "cy": ncy,
                         "rx": (w / 2) * ratio_x, "ry": (h / 2) * ratio_y, "ang": ang})

            done = n - f0
            if done % 4 == 0 or n == f1:
                el = time.time() - t0
                rate = done / el if el > 0 else 0
                job.update(frame=done, progress=done / count, fps=round(rate, 1),
                           eta=round((count - done) / rate) if rate > 0 else None)
    finally:
        reader.close()

    trimmed = simplify_keys(keys)
    job.update(state="done", progress=1.0, note=reason, reason=reason,
               keys=trimmed, raw=len(keys), kept=len(trimmed),
               first=keys[0]["f"], last=keys[-1]["f"])


@app.get("/api/trackers")
def list_trackers():
    return jsonify(tracker_catalogue())


@app.post("/api/project/<pid>/track")
def start_track(pid):
    p = load_project(pid)
    if p["meta"].get("media") == "image":
        abort(400, "there is nothing to follow in a single picture")
    body = request.json or {}
    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "project": pid, "state": "queued",
                        "progress": 0.0, "note": "starting"}
    threading.Thread(target=run_track, args=(job_id, p, body), daemon=True).start()
    return jsonify({"job": job_id})


@app.get("/api/track/<job_id>")
def track_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404, "no such job")
    return jsonify({k: v for k, v in job.items() if k != "cancel"})


@app.post("/api/track/<job_id>/cancel")
def track_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404, "no such job")
    job["cancel"] = True
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# routes — projects
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/projects")
def list_projects():
    items = []
    for d in WORKSPACE.iterdir():
        f = d / "project.json"
        if d.is_dir() and f.exists():
            try:
                p = ensure_media(json.loads(f.read_text()))
                items.append({
                    "id": p["id"], "name": p["name"], "meta": p["meta"],
                    "shapes": len(p.get("shapes", [])),
                    "opened": p.get("opened", 0),
                })
            except Exception:
                continue
    items.sort(key=lambda x: x["opened"], reverse=True)
    return jsonify(items)


def _create_project(src: Path, name: str, linked: bool) -> dict:
    pid = uuid.uuid4().hex[:12]
    d = WORKSPACE / pid
    d.mkdir(parents=True)
    try:
        meta = probe(src)
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        abort(400, str(e))
    st = src.stat()
    project = {
        "id": pid, "name": name, "source": str(src), "linked": linked,
        "meta": meta, "shapes": [], "opened": time.time(),
        "src_size": st.st_size, "src_mtime": st.st_mtime,
    }
    with open(d / "project.json", "w") as fh:
        json.dump(project, fh, indent=1)
    return project


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    ext = Path(f.filename).suffix.lower() or ".mp4"
    staging = WORKSPACE / f".staging-{uuid.uuid4().hex[:8]}{ext}"
    f.save(staging)
    try:
        project = _create_project(staging, Path(f.filename).name, linked=False)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    final = WORKSPACE / project["id"] / f"source{ext}"
    staging.replace(final)
    project["source"] = str(final)
    save_project(project["id"], project)
    return jsonify(project)


@app.post("/api/open-path")
def open_path():
    raw = (request.json or {}).get("path", "").strip()
    src = Path(os.path.expanduser(raw)).resolve()
    if not src.is_file():
        abort(400, f"no such file: {src}")
    if src.suffix.lower() not in MEDIA_EXTS:
        abort(400, f"not a video or image this can open: {src.suffix}")
    # reuse an existing project for the same file instead of making a duplicate
    for d in WORKSPACE.iterdir():
        f = d / "project.json"
        if d.is_dir() and f.exists():
            try:
                p = json.loads(f.read_text())
            except Exception:
                continue
            if p.get("linked") and p.get("source") == str(src):
                p["opened"] = time.time()
                st = src.stat()
                if (p.get("src_size") != st.st_size
                        or p.get("src_mtime") != st.st_mtime):
                    # the file changed under us, so the old measurements are stale
                    try:
                        p["meta"] = probe(src)
                    except Exception as e:
                        abort(400, str(e))
                    p["src_size"], p["src_mtime"] = st.st_size, st.st_mtime
                    (d / "thumb.jpg").unlink(missing_ok=True)
                    with _readers_lock:
                        r = _readers.pop(p["id"], None)
                    if r:
                        r.close()
                    last = p["meta"]["frames"] - 1
                    for sh in p.get("shapes", []):
                        sh["start"] = min(sh.get("start", 0), last)
                        sh["end"] = min(sh.get("end", last), last)
                save_project(p["id"], p)
                return jsonify(p)
    return jsonify(_create_project(src, src.name, linked=True))


@app.get("/api/browse")
def browse():
    """Minimal server-side file browser, so you can pick a video without uploading."""
    raw = request.args.get("path") or str(Path.home())
    d = Path(os.path.expanduser(raw)).resolve()
    if not d.is_dir():
        d = Path.home()
    dirs, files = [], []
    try:
        for e in sorted(d.iterdir(), key=lambda p: p.name.lower()):
            if e.name.startswith("."):
                continue
            if e.is_dir():
                dirs.append({"name": e.name, "path": str(e)})
            elif e.suffix.lower() in MEDIA_EXTS:
                files.append({"name": e.name, "path": str(e), "size": e.stat().st_size,
                              "image": e.suffix.lower() in IMAGE_EXTS})
    except PermissionError:
        abort(403, "permission denied")
    return jsonify({
        "path": str(d),
        "parent": str(d.parent) if d.parent != d else None,
        "dirs": dirs, "files": files,
    })


@app.get("/api/project/<pid>")
def get_project(pid):
    p = load_project(pid)
    p["opened"] = time.time()
    save_project(pid, p)
    return jsonify(p)


@app.post("/api/project/<pid>/shapes")
def put_shapes(pid):
    p = load_project(pid)
    p["shapes"] = (request.json or {}).get("shapes", [])
    save_project(pid, p)
    return jsonify({"ok": True, "count": len(p["shapes"])})


@app.delete("/api/project/<pid>")
def delete_project(pid):
    d = project_dir(pid)
    with _readers_lock:
        r = _readers.pop(pid, None)
    if r:
        r.close()
    shutil.rmtree(d, ignore_errors=True)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# routes — frames
# --------------------------------------------------------------------------

@app.get("/api/project/<pid>/thumb")
def get_thumb(pid):
    """Small poster frame, cached on disk, for the library list."""
    d = project_dir(pid)
    cache = d / "thumb.jpg"
    if not cache.exists():
        p = load_project(pid)
        img = get_reader(pid, Path(p["source"])).frame(min(12, p["meta"]["frames"] - 1))
        if img is None:
            abort(404, "no frame")
        h, w = img.shape[:2]
        scale = 320 / max(1, w)
        img = cv2.resize(img, (320, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(cache), img, [cv2.IMWRITE_JPEG_QUALITY, 78])
    return send_file(cache, mimetype="image/jpeg")


@app.get("/api/project/<pid>/frame/<int:n>")
def get_frame(pid, n):
    p = load_project(pid)
    n = max(0, min(n, p["meta"]["frames"] - 1))
    img = get_reader(pid, Path(p["source"])).frame(n)
    if img is None:
        abort(404, f"could not decode frame {n}")
    if request.args.get("blur") == "1":
        img = render(img.copy(), p.get("shapes", []), n)
    q = int(request.args.get("q", 82))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        abort(500, "encode failed")
    return Response(buf.tobytes(), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------
# routes — export
# --------------------------------------------------------------------------

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def run_export(job_id: str, project: dict, shapes: list, opts: dict):
    job = JOBS[job_id]
    meta = project["meta"]
    w, h, fps = meta["width"], meta["height"], meta["fps"]
    total = meta["frames"]
    start = max(0, int(opts.get("range_start", 0)))
    end = min(total - 1, int(opts.get("range_end", total - 1)))
    count = max(1, end - start + 1)

    out_dir = WORKSPACE / project["id"]
    if opts.get("beside") and project.get("linked"):
        beside = Path(project["source"]).parent
        if os.access(beside, os.W_OK):
            out_dir = beside
    stem = Path(opts.get("filename") or (Path(project["name"]).stem + "-blurred")).stem
    out_path = out_dir / f"{stem}.mp4"
    if out_path.resolve() == Path(project["source"]).resolve():
        out_path = out_path.with_name(stem + "-blurred.mp4")

    crf = str(int(opts.get("crf", 20)))
    preset = opts.get("preset", "medium")
    want_audio = bool(opts.get("audio", True)) and meta.get("has_audio")

    def build_cmd(audio_mode: str):
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-"]
        if audio_mode != "none":
            # Take exactly the slice of audio that matches the frame range. We
            # deliberately avoid -shortest: with a copied audio stream ffmpeg can
            # read it as shorter than it is and cut the video off early.
            cmd += ["-ss", f"{start / fps:.6f}", "-t", f"{count / fps:.6f}",
                    "-i", project["source"]]
        cmd += ["-map", "0:v:0"]
        if audio_mode != "none":
            cmd += ["-map", "1:a:0?",
                    "-c:a", "copy" if audio_mode == "copy" else "aac"]
        if w % 2 or h % 2:
            # libx264 with yuv420p needs even dimensions
            cmd += ["-vf", "crop=trunc(iw/2)*2:trunc(ih/2)*2"]
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", crf,
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path)]
        return cmd

    modes = (["copy", "aac", "none"] if want_audio else ["none"])
    last_err = ""

    for attempt, audio_mode in enumerate(modes):
        job.update(state="running", progress=0.0, frame=0, total=count,
                   note=f"encoding ({'no audio' if audio_mode == 'none' else audio_mode})")
        reader = Reader(Path(project["source"]))
        proc = subprocess.Popen(build_cmd(audio_mode), stdin=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        failed = False
        t0 = time.time()
        try:
            for n in range(start, end + 1):
                if job.get("cancel"):
                    raise KeyboardInterrupt
                img = reader.frame(n)
                if img is None:
                    break
                if img.shape[1] != w or img.shape[0] != h:
                    img = cv2.resize(img, (w, h))
                render(img, shapes, n)
                proc.stdin.write(np.ascontiguousarray(img).tobytes())
                done = n - start + 1
                if done % 5 == 0 or done == count:
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    job.update(frame=done, progress=done / count,
                               fps=round(rate, 1),
                               eta=round((count - done) / rate) if rate > 0 else None)
            proc.stdin.close()
        except KeyboardInterrupt:
            proc.kill()
            reader.close()
            job.update(state="cancelled", note="cancelled")
            return
        except BrokenPipeError:
            failed = True
        finally:
            reader.close()

        err = (proc.stderr.read() or b"").decode(errors="replace").strip()
        rc = proc.wait()
        if rc == 0 and not failed:
            job.update(state="done", progress=1.0, note="finished",
                       output=str(out_path), size=out_path.stat().st_size,
                       audio=audio_mode != "none")
            return
        last_err = err or f"ffmpeg exited with {rc}"
        if attempt < len(modes) - 1:
            job.update(note=f"retrying: {last_err[:120]}")

    job.update(state="error", note=last_err[:500] or "export failed")


def run_image_export(job_id: str, project: dict, shapes: list, opts: dict):
    """Cover the marked spots on a still and write it out as a new file."""
    job = JOBS[job_id]
    job.update(state="running", progress=0.0, frame=0, total=1, note="covering")
    src = Path(project["source"])
    raw = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
    if raw is None:
        job.update(state="error", note="could not read the image")
        return
    if raw.dtype != np.uint8:                     # 16-bit and float images
        raw = cv2.normalize(raw, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    # cover the BGR channels, then put back whatever else the file carried
    alpha = None
    if raw.ndim == 2:
        kind, img = "gray", cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
    elif raw.shape[2] == 4:
        kind, img, alpha = "bgra", raw[:, :, :3].copy(), raw[:, :, 3]
    else:
        kind, img = "bgr", raw[:, :, :3].copy()

    render(img, shapes, 0)

    if kind == "gray":
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    elif kind == "bgra":
        img = np.dstack([img, alpha])

    fmt = str(opts.get("format", "png")).lower()
    if fmt not in ("png", "jpg"):
        fmt = "png"
    out_dir = WORKSPACE / project["id"]
    if opts.get("beside") and project.get("linked") and os.access(src.parent, os.W_OK):
        out_dir = src.parent
    stem = Path(opts.get("filename") or (Path(project["name"]).stem + "-blurred")).stem
    out_path = out_dir / f"{stem}.{fmt}"
    if out_path.resolve() == src.resolve():
        out_path = out_path.with_name(f"{stem}-blurred.{fmt}")

    if fmt == "jpg" and img.ndim == 3 and img.shape[2] == 4:
        img = img[:, :, :3]                       # jpeg cannot carry transparency
    params = ([cv2.IMWRITE_JPEG_QUALITY, int(opts.get("quality", 92))] if fmt == "jpg"
              else [cv2.IMWRITE_PNG_COMPRESSION, 6])
    try:
        ok = cv2.imwrite(str(out_path), img, params)
    except Exception as e:
        job.update(state="error", note=str(e)[:300])
        return
    if not ok:
        job.update(state="error", note="could not write the image")
        return
    job.update(state="done", progress=1.0, frame=1, note="finished",
               output=str(out_path), size=out_path.stat().st_size, audio=False)


@app.post("/api/project/<pid>/export")
def start_export(pid):
    p = load_project(pid)
    body = request.json or {}
    if "shapes" in body:
        p["shapes"] = body["shapes"]
        save_project(pid, p)
    shapes = [s for s in p.get("shapes", []) if s.get("enabled", True)]
    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS[job_id] = {"id": job_id, "project": pid, "state": "queued",
                        "progress": 0.0, "note": "starting"}
    worker = (run_image_export if p["meta"].get("media") == "image" else run_export)
    threading.Thread(target=worker, args=(job_id, p, shapes, body),
                     daemon=True).start()
    return jsonify({"job": job_id})


@app.get("/api/export/<job_id>")
def export_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404, "no such job")
    return jsonify({k: v for k, v in job.items() if k != "cancel"})


@app.post("/api/export/<job_id>/cancel")
def export_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404, "no such job")
    job["cancel"] = True
    return jsonify({"ok": True})


@app.get("/api/export/<job_id>/download")
def export_download(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("state") != "done":
        abort(404, "not ready")
    return send_file(job["output"], as_attachment=True,
                     download_name=Path(job["output"]).name)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def json_error(e):
    return jsonify({"error": getattr(e, "description", str(e))}), getattr(e, "code", 500)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Oval Blur Studio")
    ap.add_argument("--port", type=int, default=7788)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"\n  Oval Blur Studio  →  http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
