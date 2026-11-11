"""Local studio UI: ./rw web  ->  http://127.0.0.1:8765

Runs the same rw.py commands as the CLI in a subprocess and streams their output.
Bound to localhost only; POSTs require an X-RW header so other websites can't trigger jobs.
"""
import json, re, shlex, signal, subprocess, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import rw

ALLOWED = {"run", "add", "discover", "upload", "rerender", "log", "score", "selftest"}
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


def pages():
    con = rw.db()
    rows = [dict(r) for r in con.execute("select * from pages order by coalesce(ep, 0) desc, updated desc")]
    for r in rows:
        meta = rw.WORK / str(r["pageid"]) / "meta.json"
        if r["ep"] and meta.exists():
            m = json.loads(meta.read_text())
            r.update(yt_title=m["title"], script=m["script"], yt_description=m["yt_description"], potential_info=m.get("potential"),
                     images=[{k: v for k, v in i.items() if k != "path"} | {"credit": rw.credit(i), "file": Path(i["path"]).name}
                             for i in m["images"]])
        v = Path(r["video_path"] or "")
        r["video"] = v.name if r["video_path"] and v.exists() else None
    return rows


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
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

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            return self.send(200, (rw.ROOT / "web.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/pages":
            return self.send(200, {"pages": pages(), "brand": rw.BRAND})
        if path == "/api/job":
            since = int((re.search(r"since=(\d+)", query) or [0, 0])[1])
            running = bool(job["proc"] and job["proc"].poll() is None)
            return self.send(200, {"running": running, "cmd": job["cmd"], "code": job["code"],
                                   "lines": job["lines"][since:], "next": len(job["lines"])})
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
        m = re.fullmatch(r"/work/(\d+)/([\w.\-]+\.jpg)", path)  # episode images (thumbnails in the editor)
        if m and (rw.WORK / m[1] / m[2]).is_file():
            return self.send_file(rw.WORK / m[1] / m[2], "image/jpeg")
        m = re.fullmatch(r"/(video|fonts)/([\w.\-\[\],]+)", path)
        if m:  # basename only: no path traversal
            base = rw.OUT if m[1] == "video" else rw.ROOT / "brand" / "fonts"
            f = base / m[2]
            if f.is_file():
                return self.send_file(f, "video/mp4" if m[1] == "video" else "font/ttf")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("X-RW") != "1":
            return self.send(403, {"error": "missing X-RW header"})
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.startswith("/api/upload_image"):
            return self.upload_image(raw)
        body = json.loads(raw or "{}")
        try:
            if self.path == "/api/exec":
                start(str(body.get("cmd", "")))
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
                        out.append({k: v for k, v in img.items() if k not in ("file", "credit", "thumb", "tags")}
                                   | {"path": str(work / Path(img["file"]).name)})
                    elif str(img.get("url", "")).startswith("https://"):
                        out.append(rw.download_image(img, work))
                if not out:
                    raise ValueError("keep at least one image")
                m["images"] = out
                meta.write_text(json.dumps(m, indent=2))
            elif self.path == "/api/skip":
                con = rw.db()
                con.execute("update pages set status='skipped' where pageid=? and status in ('rendered','queued','failed')",
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
    print(f"Random Wiki studio: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
