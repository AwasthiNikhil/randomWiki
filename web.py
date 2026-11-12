"""Shorts Machine studio UI: ./rw web  ->  http://127.0.0.1:8765

Runs the same rw.py commands as the CLI in a subprocess and streams their output.
Bound to localhost only; POSTs require an X-RW header so other websites can't trigger jobs.
"""
import json, os, re, shlex, shutil, signal, subprocess, sys, threading, urllib.parse, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import rw

ALLOWED = {"run", "add", "download", "connect", "discover", "upload", "unpublish", "rerender", "log", "score", "selftest", "preview", "voicetest",
           "auto", "schedule"}
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
BRAND_FILE = rw.BRAND_FILE
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def brand():
    return json.loads(BRAND_FILE.read_text())  # always fresh: settings can change while the server runs


def save_brand(new):
    """Validate a settings form and merge it into brand.json (unknown keys are kept)."""
    b = brand()

    def text(k, lo, hi):
        v = str(new[k]).strip()
        if not lo <= len(v) <= hi:
            raise ValueError(f"{k}: {lo}-{hi} characters")
        return v

    def num(k, lo, hi, cast=float):
        v = cast(new[k])
        if not lo <= v <= hi:
            raise ValueError(f"{k}: must be {lo}-{hi}")
        return v

    b["channel"], b["tagline"], b["outro_line"] = text("channel", 1, 40), text("tagline", 0, 120), text("outro_line", 0, 200)
    b["logo_text"] = text("logo_text", 0, 2)
    b["hashtags"] = [h for h in (re.sub(r"\W", "", str(x)) for x in new["hashtags"]) if h][:10]
    if any(not HEX.match(str(new["colors"].get(k, ""))) for k in ("bg", "panel", "accent", "text", "muted")):
        raise ValueError("colours must be #RRGGBB")
    b["colors"] = {k: new["colors"][k] for k in ("bg", "panel", "accent", "text", "muted")}
    for kind in ("display", "body"):
        path = str(new["fonts"][kind])
        if not (rw.ROOT / path).is_file() or not path.startswith("brand/fonts/"):
            raise ValueError(f"{kind} font file missing: {path}")
        b["fonts"][kind], b["fonts"][f"{kind}_family"] = path, str(new["fonts"].get(f"{kind}_family", ""))
    if new["fonts"].get("display_weight") not in ("Regular", "Medium", "SemiBold", "Bold", "ExtraBold", "Black"):
        raise ValueError("display weight not recognised")
    b["fonts"]["display_weight"] = new["fonts"]["display_weight"]
    if new["voice"] not in rw.KOKORO_VOICES:
        raise ValueError("unknown voice")
    b["voice"], b["voice_lang"], b["voice_speed"] = new["voice"], new["voice"][0], num("voice_speed", 0.7, 1.3)
    b["voice_engine"] = new.get("voice_engine", "kokoro")
    if b["voice_engine"] not in ("kokoro", "clone"):
        raise ValueError("unknown voice engine")
    b["clone_consent"] = bool(new.get("clone_consent"))
    if b["voice_engine"] == "clone" and not (b["clone_consent"] and (rw.ROOT / "brand" / "voice_sample.wav").exists()):
        raise ValueError("to use your cloned voice, record or upload a voice sample and tick the confirmation")
    b["voice_sample"] = "brand/voice_sample.wav"
    b["music_volume"], b["min_score"], b["max_seconds"] = num("music_volume", 0, 0.5), num("min_score", 1, 10, int), num("max_seconds", 20, 175, int)
    b["llm"] = text("llm", 1, 80)
    if "gemini_model" in new:
        b["gemini_model"] = text("gemini_model", 1, 80)
    if new.get("claude_model"):
        b["claude_model"] = text("claude_model", 1, 80)
    if "topics" in new:
        b["topics"] = [str(t).strip()[:40] for t in new["topics"] if str(t).strip()][:12]
    if new.get("idea_source") in ("mix", "topics", "trending", "wikipedia"):
        b["idea_source"] = new["idea_source"]
    if new.get("mode") in ("auto", "manual"):
        b["mode"] = new["mode"]
    if "publish_to" in new:
        b["publish_to"] = [x for x in rw.PLATFORMS if x in new["publish_to"]]
    if "ai" in new:
        if new["ai"] not in ("gemini", "claude", "local"):
            raise ValueError("unknown model choice")
        b["ai"] = new["ai"]
    sch = new.get("schedule") or {}
    times = sorted({str(t) for t in sch.get("times", [])})
    days = sorted({int(d) for d in sch.get("days", []) if 0 <= int(d) <= 6})
    if sch.get("enabled") and not (times and days):
        raise ValueError("automation needs at least one time and one day")
    if not all(HHMM.match(t) for t in times) or len(times) > 4 or not HHMM.match(str(sch.get("publish_time", ""))):
        raise ValueError("times must be HH:MM (up to 4 a day)")
    if (sch.get("fallback") not in ("wikipedia", "link", "hackernews", "apod", "arxiv", "rss") or sch.get("publish") not in ("private", "at")
            or sch.get("pick", "order") not in ("order", "newest")):
        raise ValueError("unknown automation option")
    if sch["fallback"] in ("arxiv", "rss", "link") and not str(sch.get("fallback_arg", "")).strip():
        raise ValueError(f"{sch['fallback']} needs {'a topic' if sch['fallback'] == 'arxiv' else 'a link'}")
    b["schedule"] = {"enabled": bool(sch.get("enabled")), "times": times, "days": days, "upload": bool(sch.get("upload")),
                     "publish": sch["publish"], "publish_time": sch["publish_time"], "fallback": sch["fallback"],
                     "fallback_arg": str(sch.get("fallback_arg", "")).strip()[:300], "pick": sch.get("pick", "order")}
    BRAND_FILE.write_text(json.dumps(b, indent=2) + "\n")
    return b
NOISE = re.compile(r"Redirects are currently not supported|warn\(|HF_TOKEN|torch/|WeightNorm|dropout option")
job = {"proc": None, "cmd": "", "lines": [], "code": None}
lock = threading.Lock()


def start(cmd):
    args = shlex.split(cmd)
    if not args or args[0] not in ALLOWED:
        raise ValueError(f"allowed commands: {', '.join(sorted(ALLOWED))}")
    with lock:
        if job["proc"] and job["proc"].poll() is None:
            raise RuntimeError("a command is already running")
        proc = subprocess.Popen([sys.executable, "-W", "ignore", "-u", str(rw.ROOT / "rw.py"), *args],
                                cwd=rw.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        job.update(proc=proc, cmd="rw " + cmd, lines=[f"$ rw {cmd}"], code=None)

    def pump():
        for line in proc.stdout:
            if not NOISE.search(line):
                job["lines"].append(line.rstrip("\n"))
        job["code"] = proc.wait()
        job["lines"].append(f"[exit {job['code']}]")
    threading.Thread(target=pump, daemon=True).start()


def poster(video):
    """out/.posters/<video>.jpg at 1.5 s, 270 px wide; remade if the video is newer. Images scale to thousands of
    cards where <video> elements don't."""
    import imageio_ffmpeg
    jpg = rw.OUT / ".posters" / (video.name + ".jpg")
    if not jpg.exists() or jpg.stat().st_mtime < video.stat().st_mtime:
        jpg.parent.mkdir(exist_ok=True)
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", "1.5", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=270:-2", "-q:v", "4", str(jpg)], timeout=60)
    return jpg


def store_audio(raw, kind, pid=None):
    """Convert audio bytes for its use and save it; checks first, so a bad file never replaces a good one."""
    import imageio_ffmpeg, tempfile, uuid
    dest = {"sample": rw.ROOT / "brand" / "voice_sample.wav", "narration": rw.WORK / str(pid) / "voice_recorded.wav",
            "music": rw.WORK / str(pid) / "music.m4a", "sfx": rw.WORK / str(pid) / f"sfx-{uuid.uuid4().hex[:8]}.m4a"}[kind]
    staged = dest.with_suffix(".new" + dest.suffix)
    enc = ["-ac", "1", "-ar", "24000"] if kind in ("sample", "narration") else ["-vn", "-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "160k", "-f", "mp4"]
    with tempfile.NamedTemporaryFile(suffix=".audio", dir=rw.DB.parent) as tmp:
        tmp.write(raw)
        tmp.flush()
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", tmp.name, *enc, str(staged)],
                           capture_output=True, text=True)
    if r.returncode:
        staged.unlink(missing_ok=True)
        raise ValueError("couldn't read that audio file")
    secs = rw.video_seconds(staged)
    bad = ("record 10-30 seconds" if kind == "sample" and not 5 <= secs <= 90 else
           "keep sound effects under 20 seconds" if kind == "sfx" and secs > 20 else "it looks empty" if secs < 0.2 else "")
    if bad:
        staged.unlink()
        raise ValueError(f"that audio is {secs:.1f}s; {bad}")
    staged.replace(dest)
    return dest, secs


def clean_overlays(items):
    """Arrows, circles and labels from the editor: up to 4 per visual, positions inside the panel."""
    out = []
    for o in (items if isinstance(items, list) else [])[:4]:
        if not isinstance(o, dict) or o.get("type") not in ("arrow", "circle", "label"):
            continue
        c = {"type": o["type"], "x": min(max(float(o.get("x", 0.5)), 0), 1), "y": min(max(float(o.get("y", 0.5)), 0), 1),
             "delay": min(max(float(o.get("delay", 0.3)), 0), 8)}
        if o["type"] == "arrow":
            c["dir"] = o.get("dir") if o.get("dir") in ("left", "right", "up", "down") else "left"
        if o["type"] == "circle":
            c["r"] = min(max(float(o.get("r", 0.12)), 0.04), 0.4)
        if o["type"] == "label":
            c["text"] = str(o.get("text", ""))[:40]
        out.append(c)
    return out or None


def set_audio_credit(pid, file, item):
    """Remember where a track or sound came from, for the description's credits (None: it's your own)."""
    meta = rw.WORK / str(pid) / "meta.json"
    m = json.loads(meta.read_text())
    creds = m.setdefault("audio_credits", {})
    if item:
        creds[file] = {k: str(item.get(k, ""))[:300] for k in ("name", "artist", "license", "page")}
    else:
        creds.pop(file, None)
    meta.write_text(json.dumps(m, indent=2))


_meta_cache = {}  # path -> (mtime, summary): thousands of videos shouldn't mean thousands of JSON reads per refresh


HEAVY = ("images", "yt_description", "potential_info", "audio_credits")  # only the open video needs these


def pages(pageid=None):
    """The list (light: the cards and filters need title, status, scores, a cover), or one video in full."""
    con = rw.db()
    rows = [dict(r) for r in (con.execute("select * from pages where pageid=?", (pageid,)) if pageid else
                              con.execute("select * from pages order by coalesce(ep, 0) desc, updated desc"))]
    for r in rows:
        meta = rw.WORK / str(r["pageid"]) / "meta.json"
        if r["ep"] and meta.exists():
            mt = meta.stat().st_mtime
            hit = _meta_cache.get(meta)
            if hit and hit[0] == mt:
                r.update(hit[1])
            else:
                _meta_cache[meta] = (mt, meta_summary(r["pageid"], json.loads(meta.read_text())))
                r.update(_meta_cache[meta][1])
            work = meta.parent  # files that change without touching meta.json: checked every time
            r.update(own_music=(work / "music.m4a").is_file(), has_recording=(work / "voice_recorded.wav").is_file())
        v = Path(r["video_path"] or "")
        r["video"] = v.name if r["video_path"] and v.exists() else None
        if not pageid and "images" in r:
            r["cover"] = (r["images"] or [{}])[0].get("file")
            for k in HEAVY:
                r.pop(k, None)
    return rows


def meta_summary(pid, m):
    return dict(yt_title=m["title"], script=m["script"], yt_description=m["yt_description"], potential_info=m.get("potential"),
                     narration=m.get("narration", "kokoro"), music=m.get("music", "brand"), music_volume=m.get("music_volume"),
                     ai=m.get("ai") or m.get("gemini"),
                     audio_credits=m.get("audio_credits") or {},
                     images=[{k: v for k, v in i.items() if k != "path"} | {"credit": rw.credit(i), "file": Path(i["path"]).name}
                             for i in m["images"]])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        if len(data) > 20_000 and "gzip" in self.headers.get("Accept-Encoding", ""):  # big lists: ~5x smaller
            import gzip
            data = gzip.compress(data, 5)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, ctype):
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):  # Safari/Chrome need range requests to seek video
            start, end = (int(m[1]), int(m[2]) if m[2] else size - 1) if m[1] else (size - int(m[2]), size - 1)
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def upload_image(self, raw):
        """POST /api/upload_image?pageid=N with the image bytes as the body -> saved JPEG entry."""
        import io, hashlib
        from PIL import Image
        m = re.search(r"pageid=(\d+)", self.path)
        work = rw.WORK / m[1] if m else None
        if not work or not work.is_dir() or not 0 < len(raw) <= 25_000_000:
            return self.send(400, {"error": "bad upload"})
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            return self.send(400, {"error": "not an image"})
        name = f"own_{hashlib.sha1(raw).hexdigest()[:10]}.jpg"
        im.save(work / name, "JPEG", quality=92)
        self.send(200, {"image": {"source": "own", "name": "Own image", "artist": "", "license": "", "page": "",
                                  "file": name, "credit": ""}})

    def upload_audio(self, raw):
        """POST /api/upload_audio?kind=sample|narration|music|sfx[&pageid=N] with recorded/uploaded audio (webm, ogg, m4a, mp3, wav).
        music: this video's own track; sfx: a sound effect for one visual (the reply names its file)."""
        kind = (re.search(r"kind=(sample|narration|music|sfx)", self.path) or [None, None])[1]
        pid = (re.search(r"pageid=(\d+)", self.path) or [None, None])[1]
        if not kind or not 0 < len(raw) <= 60_000_000 or (kind != "sample" and not (pid and (rw.WORK / pid).is_dir())):
            return self.send(400, {"error": "bad upload"})
        try:
            dest, secs = store_audio(raw, kind, pid)
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        if kind == "music":  # your own track: no third-party credit
            set_audio_credit(pid, dest.name, None)
        return self.send(200, {"ok": True, "seconds": round(secs, 1), "file": dest.name})

    def upload_brand(self, raw):
        """POST /api/upload_brand?kind=music|logo with the file as the body."""
        import io
        kind = (re.search(r"kind=(music|logo)", self.path) or [None, None])[1]
        if not kind or not 0 < len(raw) <= 30_000_000:
            return self.send(400, {"error": "bad upload"})
        b = brand()
        if kind == "logo":
            from PIL import Image
            try:
                im = Image.open(io.BytesIO(raw)).convert("RGBA")
            except Exception:
                return self.send(400, {"error": "not an image"})
            im.thumbnail((512, 512))
            im.save(rw.ROOT / "brand" / "logo.png")
            b["logo"] = "brand/logo.png"
            BRAND_FILE.write_text(json.dumps(b, indent=2) + "\n")
        else:
            if raw[:3] != b"ID3" and raw[:2] not in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
                return self.send(400, {"error": "music must be an MP3 file"})
            (rw.ROOT / b["music"]).write_bytes(raw)
        return self.send(200, {"ok": True})

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            return self.send(200, (rw.ROOT / "web.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/page":  # ?id=N: one video with everything the editor needs
            got = pages(int((re.search(r"id=(\d+)", query) or [0, 0])[1]))
            return self.send(200, got[0]) if got else self.send(404, {"error": "no such page"})
        if path == "/api/pages":
            return self.send(200, {"pages": pages(), "brand": brand()})
        if path == "/api/settings":
            try:
                models = sorted(m["name"] for m in rw.get_json(brand()["ollama_url"] + "/api/tags")["models"])
            except Exception:
                models = []
            gemini = {"key": "shell" if os.environ.get("GEMINI_API_KEY", "").strip() else "saved" if rw.gemini_key() else "", "models": []}
            try:
                gemini["today"] = json.loads(rw.USAGE_FILE.read_text()).get(rw.usage_day(), {})
            except (OSError, ValueError):
                gemini["today"] = {}
            if gemini["key"]:
                try:  # text models this key can use; aliases first so "latest" follows Google's current model
                    req = urllib.request.Request(rw.GEMINI_API + "?pageSize=200", headers={"x-goog-api-key": rw.gemini_key()})
                    names = [m["name"].removeprefix("models/") for m in json.load(urllib.request.urlopen(req, timeout=8))["models"]
                             if "generateContent" in m.get("supportedGenerationMethods", [])]
                    gemini["models"] = sorted((n for n in names if n.startswith("gemini") and not re.search(
                        r"tts|image|robotics|computer|omni|transcribe|customtools|banana", n)), key=lambda n: ("latest" not in n, n))
                except Exception as e:
                    gemini["error"] = f"couldn't list models ({type(e).__name__})"
            claude = {"key": "shell" if os.environ.get("ANTHROPIC_API_KEY", "").strip() else "saved" if rw.claude_key() else "",
                      "models": rw.CLAUDE_MODELS}
            b = brand()
            log = rw.AUTO_LOG.read_text(errors="ignore").splitlines()[-40:] if rw.AUTO_LOG.exists() else []
            return self.send(200, {"brand": b, "voices": sorted(rw.KOKORO_VOICES), "models": models, "gemini": gemini, "claude": claude, "ai": b.get("ai") or "gemini", "publish": rw.publish_status(),
                                   "publish_to": b.get("publish_to") or ["youtube"], "youtube": rw.youtube_status(),
                                   "stock": {n: "shell" if os.environ.get(f"{n.upper()}_API_KEY", "").strip() else "saved" if rw.stock_key(n) else "" for n in rw.STOCK},
                                   "schedule": rw.SCHEDULE_DEFAULTS | b.get("schedule", {}), "schedule_loaded": rw.schedule_loaded(),
                                   "auto_log": [l for l in log if l.startswith(("===", "make:", "queue empty", "  done", "  uploaded", "  upload failed", "  publishing failed", "  YouTube", "  Facebook", "  Instagram", "  TikTok"))][-12:],
                                   "music": (rw.ROOT / b["music"]).is_file(), "voice_sample": (rw.ROOT / "brand" / "voice_sample.wav").is_file(),
                                   "clone_ready": (rw.ROOT / ".venv-clone" / "bin" / "python").exists(),
                                   "logo": bool(b.get("logo")) and (rw.ROOT / b["logo"]).is_file()})
        if path == "/api/fonts":
            from urllib.parse import parse_qs
            qs = {k: v[0] for k, v in parse_qs(query).items()}
            ql, cat = qs.get("q", "").lower(), qs.get("category", "")
            try:
                found = [f for f in rw.google_fonts() if ql in f["family"].lower() and (not cat or f["category"] == cat)]
            except Exception as e:
                return self.send(502, {"error": f"Google Fonts unavailable: {e}"})
            return self.send(200, {"fonts": sorted(found, key=lambda f: f["popularity"])[:40]})
        m = re.fullmatch(r"/work/(\d+)/voice_recorded\.wav", path)
        if m and (rw.WORK / m[1] / "voice_recorded.wav").is_file():
            return self.send_file(rw.WORK / m[1] / "voice_recorded.wav", "audio/wav")
        m = re.fullmatch(r"/(data|brand)/(preview\.png|voice_preview\.wav|logo\.png|music\.mp3|voice_sample\.wav)", path)
        if m and (rw.ROOT / m[1] / m[2]).is_file():
            types = {"png": "image/png", "wav": "audio/wav", "mp3": "audio/mpeg"}
            return self.send_file(rw.ROOT / m[1] / m[2], types[m[2].rsplit(".", 1)[1]])
        if path == "/api/library":  # ?kind=video|audio|image, plus how many of each
            kind = (re.search(r"kind=(video|audio|image)", query) or [0, "video"])[1]
            return self.send(200, {"items": rw.library_items(kind), "counts": {k: len(rw.library_items(k)) for k in rw.MEDIA_KINDS}})
        m = re.fullmatch(r"/(?:media/(video|audio|image)|(library))/([\w.\-]+\.(mp4|jpg|png|m4a))", urllib.parse.unquote(path))
        if m:  # basename only: no path traversal; /library/ is the old address of media/video
            f = rw.MEDIA / (m[1] or "video") / m[3]
            if f.is_file():
                return self.send_file(f, {"mp4": "video/mp4", "jpg": "image/jpeg", "png": "image/png", "m4a": "audio/mp4"}[m[4]])
        if path == "/api/job":
            since = int((re.search(r"since=(\d+)", query) or [0, 0])[1])
            running = bool(job["proc"] and job["proc"].poll() is None)
            return self.send(200, {"running": running, "cmd": job["cmd"], "code": job["code"],
                                   "lines": job["lines"][since:], "next": len(job["lines"])})
        if path == "/api/audio/search":  # ?kind=music|sfx&q=…: free tracks and sounds via Openverse
            from urllib.parse import parse_qs
            qs = {k: v[0] for k, v in parse_qs(query).items()}
            if not qs.get("q") or qs.get("kind") not in ("music", "sfx"):
                return self.send(400, {"error": "need kind and q"})
            try:
                return self.send(200, {"results": rw.src_audio(qs["q"], qs["kind"])})
            except Exception as e:
                return self.send(502, {"error": f"audio search failed: {e}"})
        m = re.fullmatch(r"/poster/([\w.\-\[\],]+)\.jpg", path)  # small still of a rendered video, made once, for the grids
        if m and (rw.OUT / m[1]).is_file():
            return self.send_file(poster(rw.OUT / m[1]), "image/jpeg")
        if path == "/api/images/search":
            from urllib.parse import parse_qs
            qs = {k: v[0] for k, v in parse_qs(query).items()}
            fn = rw.IMAGE_SOURCES.get(qs.get("source", ""))
            if not fn or not qs.get("q"):
                return self.send(400, {"error": "need source and q"})
            try:
                return self.send(200, {"results": fn(qs["q"])})
            except Exception as e:
                return self.send(502, {"error": f"{qs['source']} search failed: {e}"})
        m = re.fullmatch(r"/work/(\d+)/([\w.\-]+\.(jpg|png|m4a))", path)  # episode images and sounds (for the editor)
        if m and (rw.WORK / m[1] / m[2]).is_file():
            return self.send_file(rw.WORK / m[1] / m[2], {"png": "image/png", "jpg": "image/jpeg", "m4a": "audio/mp4"}[m[3]])
        m = re.fullmatch(r"/(video|fonts)/([\w.\-\[\],]+)", path)
        if m:  # basename only: no path traversal
            base = rw.OUT if m[1] == "video" else rw.ROOT / "brand" / "fonts"
            f = base / m[2]
            if f.is_file():
                return self.send_file(f, "video/mp4" if m[1] == "video" else "font/ttf")
        self.send(404, {"error": "not found"})

    def media_upload(self):
        """POST /api/media_upload?kind=video|audio|image&name=<file name> with the file as the body."""
        import tempfile
        kind = (re.search(r"kind=(video|audio|image)", self.path) or [0, None])[1]
        name = urllib.parse.unquote((re.search(r"name=([^&]+)", self.path) or [0, "upload"])[1])
        size = int(self.headers.get("Content-Length", 0))
        if not kind or not 0 < size <= (4 << 30 if kind == "video" else 500 << 20):
            return self.send(400, {"error": "bad upload (videos up to 4 GB, audio and images up to 500 MB)"})
        with tempfile.NamedTemporaryFile(suffix=Path(name).suffix[:10], dir=rw.DB.parent) as tmp:
            left = size
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                tmp.write(chunk)
                left -= len(chunk)
            tmp.flush()
            try:
                side = rw.save_upload(kind, tmp.name, name)
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        return self.send(200, {"ok": True, "item": side})

    def do_POST(self):
        if self.headers.get("X-RW") != "1":
            return self.send(403, {"error": "missing X-RW header"})
        if self.path.startswith("/api/media_upload"):  # streamed to disk: videos can be large
            return self.media_upload()
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.startswith("/api/upload_image"):
            return self.upload_image(raw)
        if self.path.startswith("/api/upload_brand"):
            return self.upload_brand(raw)
        if self.path.startswith("/api/upload_audio"):
            return self.upload_audio(raw)
        body = json.loads(raw or "{}")
        try:
            if self.path == "/api/exec":
                start(str(body.get("cmd", "")))
            elif self.path == "/api/clear":  # wipe stored terminal output (only the screen while a job runs)
                with lock:
                    if not (job["proc"] and job["proc"].poll() is None):
                        job.update(lines=[], cmd="", code=None)
                    return self.send(200, {"next": len(job["lines"])})
            elif self.path == "/api/stop":
                if job["proc"] and job["proc"].poll() is None:
                    job["proc"].send_signal(signal.SIGINT)  # make() records it as failed
            elif self.path == "/api/reorder":  # body: {"order": [pageid, ...]} top to bottom
                con = rw.db()
                con.executemany("update pages set rank=? where pageid=? and status='queued'",
                                [(i, int(pid)) for i, pid in enumerate(body.get("order", []))])
                con.commit()
            elif self.path == "/api/script":  # save hand-edited title/narration; client then runs rerender
                pid, title, script = int(body["pageid"]), str(body["title"]).strip(), str(body["script"]).strip()
                if not (0 < len(title) <= 100 and 20 <= len(script.split()) <= 250):
                    raise ValueError("title must be 1-100 characters and narration 20-250 words")
                meta = rw.WORK / str(pid) / "meta.json"
                m = json.loads(meta.read_text())
                m.update(title=title, script=script)
                meta.write_text(json.dumps(m, indent=2))
            elif self.path == "/api/images":  # body: {"pageid", "images": [ordered list]}; downloads new picks
                pid = int(body["pageid"])
                work = rw.WORK / str(pid)
                meta = work / "meta.json"
                m = json.loads(meta.read_text())
                out = []
                for img in body.get("images", [])[:12]:
                    if img.get("file") and (work / Path(img["file"]).name).is_file():  # already in the folder
                        out.append({k: v for k, v in img.items() if k not in ("file", "credit", "tags") and (k != "thumb" or img.get("kind") == "video")}
                                   | {"path": str(work / Path(img["file"]).name)})
                    elif str(img.get("url", "")).startswith(("https://", "library:")):
                        out.append(rw.download_image(img, work))
                    else:
                        continue
                    edit = {k: img.get(k) for k in ("motion", "sfx", "sentence", "overlays")}  # your choices in the editor
                    edit["overlays"] = clean_overlays(edit["overlays"])
                    if edit["motion"] not in ("in", "out", "left", "right", "still"):
                        edit["motion"] = None
                    if not (re.fullmatch(r"sfx-[0-9a-f]{8}\.m4a", str(edit["sfx"])) and (work / edit["sfx"]).is_file()):
                        edit["sfx"] = None
                    if not (isinstance(edit["sentence"], int) and 0 <= edit["sentence"] < 40):
                        edit["sentence"] = None
                    out[-1].update(edit)
                if not out:
                    raise ValueError("keep at least one image")
                m["images"] = out
                a = body.get("audio") or {}
                if a.get("music") in ("brand", "none", "own"):
                    if a["music"] == "own" and not (work / "music.m4a").is_file():
                        raise ValueError("upload this video's music track first")
                    m["music"] = a["music"]
                if a.get("music_volume") is not None:
                    m["music_volume"] = max(0.0, min(0.5, float(a["music_volume"])))
                meta.write_text(json.dumps(m, indent=2))
            elif self.path == "/api/settings":
                return self.send(200, {"ok": True, "brand": save_brand(body["brand"])})
            elif self.path == "/api/library_delete":  # body: {"file": "<name>", "kind": "video"|"audio"|"image"}
                name, kind = str(body.get("file", "")), body.get("kind", "video")
                f = rw.MEDIA / (kind if kind in rw.MEDIA_KINDS else "video") / name
                if not re.fullmatch(r"[\w.\-]+", name) or not f.is_file():
                    raise ValueError("no such file in your media")
                f.unlink()
                f.with_suffix(".json").unlink(missing_ok=True)
                f.with_suffix(".jpg").unlink(missing_ok=True)
            elif self.path == "/api/library_use":  # body: {"file", "pageid", "kind"}: a clip or image becomes a visual, audio its music
                name, pid, kind = str(body.get("file", "")), int(body["pageid"]), body.get("kind", "video")
                meta = rw.WORK / str(pid) / "meta.json"
                if kind not in rw.MEDIA_KINDS or not re.fullmatch(r"[\w.\-]+", name) or not (rw.MEDIA / kind / name).is_file() or not meta.exists():
                    raise ValueError("pick something from your media and a video")
                side = next((v for v in rw.library_items(kind) if v["file"] == name), {})
                if kind == "audio":
                    store_audio((rw.MEDIA / "audio" / name).read_bytes(), "music", pid)
                    set_audio_credit(pid, "music.m4a", None if side.get("site") == "Uploaded" else  # your own file: no credit line
                                     {"name": side.get("title", name), "artist": side.get("uploader") or "unknown",
                                      "license": side.get("license") or "licence not stated", "page": side.get("url", "")})
                    m = json.loads(meta.read_text())
                    m["music"] = "own"
                    meta.write_text(json.dumps(m, indent=2))
                    return self.send(200, {"ok": True, "music": True})
                m = json.loads(meta.read_text())
                if kind == "image":
                    if len(m["images"]) >= 12:
                        raise ValueError("that video already has 12 visuals; remove one in its editor first")
                    m["images"].append(rw.download_image(next(c for c in rw.src_media_images("", 10**6) if c["url"] == "media:" + name), meta.parent))
                    meta.write_text(json.dumps(m, indent=2))
                    return self.send(200, {"ok": True, "n": len(m["images"])})
                if len(m["images"]) >= 12:
                    raise ValueError("that video already has 12 visuals; remove one in its editor first")
                clip = next(c for c in rw.src_library("", 10**6) if c["url"] == "library:" + name)
                m["images"].append(rw.download_clip(clip, meta.parent))
                meta.write_text(json.dumps(m, indent=2))
                return self.send(200, {"ok": True, "n": len(m["images"])})
            elif self.path == "/api/audio_use":  # body: {"pageid", "kind": "music"|"sfx", "item": {url, name, artist, license, page}}
                pid, kind, item = int(body["pageid"]), body.get("kind"), body.get("item") or {}
                if kind not in ("music", "sfx") or not (rw.WORK / str(pid) / "meta.json").exists():
                    raise ValueError("pick a track for a video")
                if not any(item.get("url") == x["url"] for x in rw.src_audio(str(body.get("q") or item.get("name", "")), kind)):
                    raise ValueError("that track isn't in the search results any more; search again")  # only fetch what Openverse listed
                dest, secs = store_audio(rw.fetch(item["url"]), kind, pid)
                set_audio_credit(pid, dest.name, item)
                return self.send(200, {"ok": True, "file": dest.name, "seconds": round(secs, 1)})
            elif self.path == "/api/stock_key":  # body: {"name": "pexels"|"pixabay", "key": "..."}; empty removes it
                name, key = body.get("name"), str(body.get("key", "")).strip()
                if name not in rw.STOCK:
                    raise ValueError("unknown stock library")
                keys = rw.secret(rw.STOCK_FILE)
                if key and not re.fullmatch(r"[\w\-]{20,100}", key):
                    raise ValueError(f"that doesn't look like a {rw.STOCK[name]} API key")
                keys[name] = key
                rw.secret(rw.STOCK_FILE, {k: v for k, v in keys.items() if v})
            elif self.path == "/api/youtube_client":  # body: {"json": "<client file contents>"}
                rw.save_youtube_client(str(body.get("json", "")))
            elif self.path == "/api/youtube_disconnect":
                rw.youtube_disconnect()
            elif self.path == "/api/meta_token":  # body: {"token": "..."}
                rw.save_meta(str(body.get("token", "")))
            elif self.path == "/api/meta_disconnect":
                rw.META_FILE.unlink(missing_ok=True)
            elif self.path == "/api/tiktok_app":  # body: {"key": "...", "secret": "..."}
                rw.save_tiktok_app(str(body.get("key", "")), str(body.get("secret", "")))
            elif self.path == "/api/tiktok_disconnect":
                rw.tiktok_disconnect()
            elif self.path in ("/api/gemini_key", "/api/claude_key"):  # body: {"key": "..."}; empty removes the saved key
                key, gem = str(body.get("key", "")).strip(), self.path == "/api/gemini_key"
                f = rw.GEMINI_KEY_FILE if gem else rw.CLAUDE_KEY_FILE
                if not key:
                    f.unlink(missing_ok=True)
                elif not re.fullmatch(r"[\w.\-]{20,200}" if gem else r"sk-ant-[\w\-]{20,200}", key):
                    raise ValueError(f"that doesn't look like {'a Gemini' if gem else 'an Anthropic'} API key")
                else:
                    rw.SECRETS.mkdir(exist_ok=True)
                    f.write_text(key)
                    f.chmod(0o600)
            elif self.path == "/api/fonts/install":
                family = str(body["family"])
                if not any(f["family"] == family for f in rw.google_fonts()):
                    raise ValueError("unknown font family")
                return self.send(200, {"path": rw.install_font(family), "family": family})
            elif self.path == "/api/remove_brand":  # body: {"kind": "music"|"logo"}
                b = brand()
                if body.get("kind") == "music":
                    (rw.ROOT / b["music"]).unlink(missing_ok=True)
                elif body.get("kind") == "logo":
                    (rw.ROOT / "brand" / "logo.png").unlink(missing_ok=True)
                    b.pop("logo", None)
                    BRAND_FILE.write_text(json.dumps(b, indent=2) + "\n")
            elif self.path == "/api/delete_video":  # body: {"pageid"}; keeps the page in the log as skipped
                pid = int(body["pageid"])
                con = rw.db()
                row = con.execute("select status, video_path from pages where pageid=?", (pid,)).fetchone()
                if not row or row["status"] == "uploaded":
                    raise ValueError("only videos that are not uploaded can be deleted here")
                if job["proc"] and job["proc"].poll() is None:
                    raise RuntimeError("wait for the running command to finish")
                rw.delete_local(con, pid, "video deleted")
            elif self.path == "/api/delete":  # body: {"pageids": [...]}; uploaded pages are never deleted
                con = rw.db()
                deleted, kept = 0, 0
                for pid in {int(x) for x in body.get("pageids", [])}:
                    row = con.execute("select status, video_path from pages where pageid=?", (pid,)).fetchone()
                    if not row:
                        continue
                    if row["status"] == "uploaded":
                        kept += 1
                        continue
                    shutil.rmtree(rw.WORK / str(pid), ignore_errors=True)
                    video = Path(row["video_path"] or "")
                    if row["video_path"] and video.resolve().parent == rw.OUT.resolve():  # only files we rendered
                        video.unlink(missing_ok=True)
                    con.execute("delete from pages where pageid=?", (pid,))
                    deleted += 1
                con.commit()
                return self.send(200, {"ok": True, "deleted": deleted, "kept_uploaded": kept})
            elif self.path == "/api/reorder_uploads":  # body: {"order": [pageid, ...]} = Publish list, top first
                con = rw.db()
                con.executemany("update pages set upload_rank=? where pageid=? and status='rendered'",
                                [(i, int(pid)) for i, pid in enumerate(body.get("order", []))])
                con.commit()
            elif self.path == "/api/skip":
                con = rw.db()
                con.execute("update pages set status='skipped' where pageid=? and status in ('rendered','queued','failed','draft')",
                            (int(body["pageid"]),))
                con.commit()
            else:
                return self.send(404, {"error": "not found"})
            self.send(200, {"ok": True})
        except (ValueError, RuntimeError) as e:
            self.send(409, {"error": str(e)})


def serve(port=8765, open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}"
    print(f"Shorts Machine studio: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
