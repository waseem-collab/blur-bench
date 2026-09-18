"""
Oval Blur Studio — local video editor.

Pick a video, step through it frame by frame, draw ovals over anything you want
hidden, then export a copy of the video with those regions blurred.
"""
import json
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


def load_project(pid: str) -> dict:
    d = project_dir(pid)
    with open(d / "project.json") as fh:
        return json.load(fh)


def save_project(pid: str, data: dict) -> None:
    d = project_dir(pid)
    tmp = d / "project.json.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=1)
    tmp.replace(d / "project.json")


def probe(path: Path) -> dict:
    """Pull video metadata, preferring ffprobe and falling back to OpenCV."""
    meta = {"width": 0, "height": 0, "fps": 0.0, "frames": 0, "duration": 0.0, "has_audio": False}
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


_readers: dict[str, Reader] = {}
_readers_lock = threading.Lock()


def get_reader(pid: str, path: Path) -> Reader:
    with _readers_lock:
        r = _readers.get(pid)
        if r is None:
            r = Reader(path)
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
                p = json.loads(f.read_text())
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
    if src.suffix.lower() not in VIDEO_EXTS:
        abort(400, f"not a recognised video extension: {src.suffix}")
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
            elif e.suffix.lower() in VIDEO_EXTS:
                files.append({"name": e.name, "path": str(e), "size": e.stat().st_size})
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
    threading.Thread(target=run_export, args=(job_id, p, shapes, body),
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
