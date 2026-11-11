"""Random Wiki Shorts: random Wikipedia page -> branded YouTube Short.

  ./rw run [--url URL] [--upload] [--publish-at ISO]   make one video
  ./rw add URL [URL ...]                              queue curated pages
  ./rw upload PAGEID [--publish-at ISO]               upload a rendered video
  ./rw log [N]                                        show the page log
  ./rw selftest
"""
import argparse, json, os, re, sqlite3, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for k, sub in [("HF_HOME", "hf"), ("TORCH_HOME", "torch"), ("XDG_CACHE_HOME", "xdg")]:
    os.environ.setdefault(k, str(ROOT / ".cache" / sub))  # keep every download inside the project

BRAND = json.loads((ROOT / "brand.json").read_text())
C = BRAND["colors"]
DB = ROOT / "data" / "randomwiki.db"
WORK, OUT, SECRETS = ROOT / "work", ROOT / "out", ROOT / "secrets"
API = "https://en.wikipedia.org/w/api.php"
W, H, FPS = 1080, 1920, 30
PX, PY, PW, PH = 40, 440, 1000, 1000  # image panel


# ---------- http / db ----------
def fetch(url, params=None, data=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": BRAND["user_agent"], "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except urllib.error.URLError as e:
            if attempt == 3 or getattr(e, "code", 0) in (400, 403, 404):
                raise
            time.sleep(5 * (attempt + 1))  # Wikimedia rate-limits bursts (429)


def get_json(url, params=None, data=None):
    return json.loads(fetch(url, params, data))


def wiki(**params):
    return get_json(API, {"action": "query", "format": "json", "formatversion": 2, **params})


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""create table if not exists pages(
        pageid integer primary key, title text, url text, source text, status text,
        score integer, reason text, ep integer, video_path text, video_id text,
        created text default current_timestamp, updated text default current_timestamp)""")
    return con


def upsert(con, pageid, **f):
    f["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cols = ", ".join(f)
    con.execute(f"insert into pages(pageid, {cols}) values(?, {', '.join('?' * len(f))}) "
                f"on conflict(pageid) do update set {', '.join(f'{k}=excluded.{k}' for k in f)}",
                [pageid, *f.values()])
    con.commit()


# ---------- llm ----------
def llm(prompt, temperature=0.7):
    r = get_json(BRAND["ollama_url"] + "/api/chat", data=json.dumps({
        "model": BRAND["llm"], "stream": False, "format": "json",
        "options": {"num_ctx": 8192, "temperature": temperature},
        "messages": [{"role": "user", "content": prompt}]}).encode())
    return json.loads(r["message"]["content"])


SCORE_PROMPT = """You choose topics for a YouTube Shorts channel that turns random Wikipedia articles into 50-second stories.
Rate how likely this article is to make a viewer think "wait, what?".
Most random articles (minor species, small villages, obscure athletes, routine politicians, ordinary companies, albums) score 1-4.
Score 7+ only if the text contains a genuinely surprising fact, strange event, record, mystery, scandal or conflict.

Title: {title}
Text: {text}

Reply as JSON: {{"score": <1-10>, "hook": "<the single most surprising fact, one sentence>", "reason": "<short>"}}"""

SCRIPT_PROMPT = """Write the narration for a YouTube Short about the Wikipedia article below.
Rules:
- 100 to 120 words (about 7 sentences of 15 words). Plain spoken English, short sentences, no lists, no emojis, no hashtags.
- The first sentence is a hook of under 14 words built on the most surprising fact.
- Tell it as a story with a payoff at the end. Use ONLY facts stated in the article. Do not invent anything.
- Do not say "Wikipedia", "did you know", "in this video", or ask viewers to like or subscribe.

Article title: {title}
Article text:
{text}

Reply as JSON: {{"title": "<YouTube title, max 60 characters, curious but accurate>", "script": "<narration>", "tags": ["<5 to 8 lowercase tags>"]}}"""

REVISE_PROMPT = """Your narration below is {n} words. It must be 100 to 120 words (about 7 sentences).
{action} Keep the hook first and the payoff last. Use ONLY facts from the article. The title must be at most 60 characters.

Article text:
{text}

Narration:
{script}

Reply as JSON: {{"title": "<max 60 characters>", "script": "<narration, 100-120 words>", "tags": {tags}}}"""

CHECK_PROMPT = """You are a strict fact-checker. Compare every claim in the NARRATION against the ARTICLE.
Check dates, numbers, names, places and cause/effect. Fix any claim the article does not support by
rewriting it to match the article exactly, or removing it. Keep the style, hook and length (100-120 words).

ARTICLE:
{text}

NARRATION:
{script}

Reply as JSON: {{"errors": ["<each unsupported claim, empty if none>"], "script": "<corrected narration>"}}"""


# ---------- picking pages ----------
def page_url(title):
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


def precheck(p):
    """Cheap filters before spending an LLM call. Returns rejection reason or None."""
    if "disambiguation" in p.get("pageprops", {}):
        return "disambiguation"
    if re.match(r"(List|Lists|Index|Outline) of ", p["title"]):
        return "list page"
    if p.get("length", 0) < 6000:
        return f"too short ({p.get('length', 0)} bytes)"
    known = [v for v in (p.get("pageviews") or {}).values() if v is not None]
    views = sum(known)
    if known and views < 150:  # batch queries often omit view data; missing = unknown, not zero
        return f"low views ({views}/60d)"
    return None


def discover(con, max_batches=15):
    for _ in range(max_batches):
        res = wiki(generator="random", grnnamespace=0, grnlimit=20,
                   prop="info|pageprops|pageviews|extracts", ppprop="disambiguation",
                   exintro=1, explaintext=1, exlimit=20)
        for p in res["query"]["pages"]:
            if con.execute("select 1 from pages where pageid=?", (p["pageid"],)).fetchone():
                continue
            base = dict(title=p["title"], url=page_url(p["title"]), source="random")
            reason = precheck(p)
            if reason:
                upsert(con, p["pageid"], status="rejected", reason=reason, **base)
                continue
            s = llm(SCORE_PROMPT.format(title=p["title"], text=p.get("extract", "")[:3000]), 0.2)
            score = int(s.get("score", 0))
            ok = score >= BRAND["min_score"]
            upsert(con, p["pageid"], status="queued" if ok else "rejected", score=score,
                   reason=f"llm: {s.get('hook') if ok else s.get('reason')}", **base)
            print(f"  {score:>2}/10  {p['title']}")
            if ok:
                return p["pageid"]
    raise SystemExit("No interesting page found; run again.")


def resolve(url_or_title):
    t = url_or_title
    if "/wiki/" in t:
        t = urllib.parse.unquote(t.split("/wiki/", 1)[1].split("#")[0])
    p = wiki(titles=t.replace("_", " "), redirects=1)["query"]["pages"][0]
    if p.get("missing"):
        raise SystemExit(f"Not found on Wikipedia: {url_or_title}")
    return p["pageid"], p["title"]


def add_curated(con, url, force=False):
    pageid, title = resolve(url)
    row = con.execute("select status from pages where pageid=?", (pageid,)).fetchone()
    if row and row["status"] in ("rendered", "uploaded", "queued") and not force:
        print(f"skip: {title} is already {row['status']} (use --force)")
        return None
    upsert(con, pageid, title=title, url=page_url(title), source="curated", status="queued", reason="curated")
    print(f"queued: {title}")
    return pageid


# ---------- content ----------
def fetch_article(pageid):
    p = wiki(pageids=pageid, prop="extracts|description|pageimages", explaintext=1, piprop="name")["query"]["pages"][0]
    text = re.split(r"\n== (See also|References|Notes|External links|Further reading) ==", p.get("extract", ""))[0]
    return p["title"], p.get("description", ""), text, p.get("pageimage")


def write_script(title, text):
    s = {}
    for _ in range(6):
        n = len(str(s.get("script", "")).split())
        if n < 40:  # nothing usable yet: fresh draft
            s = llm(SCRIPT_PROMPT.format(title=title, text=text[:12000]))
        else:  # revise the draft we have; far more reliable than redrafting blind
            action = ("Cut the least important sentences and tighten the rest." if n > 128 else
                      "Add one or two more facts from the article." if n < 85 else "Keep the length.")
            r = llm(REVISE_PROMPT.format(n=n, action=action, text=text[:12000], script=s["script"],
                                         tags=json.dumps(s.get("tags", []))))
            s = {**s, **{k: v for k, v in r.items() if v}}
        n = len(str(s.get("script", "")).split())
        if 85 <= n <= 128 and 0 < len(str(s.get("title", ""))) <= 70:
            s["tags"] = [str(t) for t in s.get("tags", [])][:8]
            c = llm(CHECK_PROMPT.format(text=text[:12000], script=s["script"]), 0.1)
            fixed = str(c.get("script", ""))
            if c.get("errors") and 85 <= len(fixed.split()) <= 128:
                print("  fact-check fixed: " + "; ".join(map(str, c["errors"]))[:300])
                s["script"] = fixed
            return s
        print(f"  script was {n} words / title {len(str(s.get('title', '')))} chars, retrying")
    raise RuntimeError("LLM could not produce a valid script")


def strip_html(s):
    s = re.sub(r"<(\w+)[^>]*display:\s*none[^>]*>.*?</\1>", "", s or "", flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


UNKNOWN = re.compile(r"^(unknown|anonymous|unidentified|not specified|none|n/a)\b", re.I)


def credit(img):
    """'Ludwig Wegmann · CC BY-SA 3.0' or just 'Public domain' when the author is unknown."""
    artist = img.get("artist") or ""
    return img["license"] if not artist or UNKNOWN.match(artist) else f"{artist} · {img['license']}"


BAD_FILE = re.compile(r"icon|logo|flag|symbol|commons-|wiki|ambox|question_book|edit-|stub|padlock|signature|location_map|blank", re.I)
OK_LICENSE = re.compile(r"^(cc[ -]?by|cc0|cc-zero|public domain|pd)", re.I)


def fetch_images(title, pageimage, dest, limit=6):
    """Free-licensed images from the article itself (Commons), then Openverse."""
    got = []
    res = wiki(generator="images", titles=title, gimlimit=50, prop="imageinfo",
               iiprop="url|size|mime|extmetadata", iiurlwidth=1400)
    files = res.get("query", {}).get("pages", [])
    files.sort(key=lambda f: f["title"].replace(" ", "_") != f"File:{pageimage}")  # lead image first
    for f in files:
        ii = (f.get("imageinfo") or [{}])[0]
        meta = {k: v.get("value") for k, v in ii.get("extmetadata", {}).items()}
        if (BAD_FILE.search(f["title"]) or ii.get("mime") not in ("image/jpeg", "image/png", "image/webp")
                or ii.get("width", 0) < 600 or ii.get("height", 0) < 450
                or meta.get("NonFree") or not OK_LICENSE.match(meta.get("LicenseShortName") or "")):
            continue
        got.append(dict(url=ii.get("thumburl") or ii["url"], page=ii.get("descriptionurl"),
                        name=f["title"][5:], artist=strip_html(meta.get("Artist")) or "unknown",
                        license=meta.get("LicenseShortName")))
        if len(got) >= limit:
            break
    if len(got) < 2:  # fallback: Openverse, only results that mention the topic
        words = {w.lower() for w in re.findall(r"\w{4,}", title)}
        try:
            ov = get_json("https://api.openverse.org/v1/images/",
                          {"q": title, "license_type": "commercial", "page_size": 10})
        except Exception:
            ov = {"results": []}
        for r in ov.get("results", []):
            hay = (r.get("title", "") + " " + " ".join(t["name"] for t in r.get("tags") or [])).lower()
            if words & set(re.findall(r"\w{4,}", hay)) and (r.get("width") or 0) >= 600:
                got.append(dict(url=r["url"], page=r.get("foreign_landing_url"), name=r.get("title") or "image",
                                artist=r.get("creator") or "unknown",
                                license=f"CC {r['license'].upper()} {r.get('license_version', '')}".strip()))
            if len(got) >= limit:
                break
    out = []
    for i, g in enumerate(got):
        path = dest / f"img{i}.jpg"
        try:
            time.sleep(1)
            path.write_bytes(fetch(g["url"]))
            g["path"] = str(path)
            out.append(g)
        except Exception as e:
            print(f"  image failed: {g['name']} ({e})")
    return out


def tts(text, dest):
    """Kokoro narration -> wav + per-word timings [(word, start, end)]."""
    import numpy as np, soundfile as sf
    from kokoro import KPipeline
    pipe = KPipeline(lang_code=BRAND["voice_lang"], repo_id="hexgrad/Kokoro-82M")
    speed = BRAND["voice_speed"]
    for _ in range(3):
        audio, words, t0 = [], [], 0.0
        for r in pipe(text, voice=BRAND["voice"], speed=speed):
            a = r.audio.numpy()
            for tok in r.tokens or []:
                if tok.start_ts is None:
                    continue
                if re.search(r"\w", tok.text):
                    words.append([tok.text, t0 + tok.start_ts, t0 + (tok.end_ts or tok.start_ts + 0.2)])
                elif words:
                    words[-1][0] += tok.text  # attach punctuation
            gap = np.zeros(int(24000 * 0.12), dtype=a.dtype)
            audio += [a, gap]
            t0 += (len(a) + len(gap)) / 24000
        dur = t0 + 0.5
        if dur <= BRAND["max_seconds"] or speed >= 1.2:
            break
        speed = min(1.2, speed * dur / (BRAND["max_seconds"] - 1))
        print(f"  narration {dur:.1f}s too long, re-voicing at speed {speed:.2f}")
    audio.append(np.zeros(int(24000 * 0.5), dtype=audio[0].dtype))
    sf.write(dest, np.concatenate(audio), 24000)
    return words, dur


def group_words(words, max_words=3):
    """Caption groups: up to N words, break after punctuation. -> [(start, end, [words])]"""
    groups, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or re.search(r"[.,!?;:]$", w[0]):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [(g[0][1], (groups[i + 1][0][1] if i + 1 < len(groups) else g[-1][2] + 0.4), g)
            for i, g in enumerate(groups)]


# ---------- rendering ----------
def render(meta, words, audio_path, duration, out_path):
    import subprocess, imageio_ffmpeg
    from PIL import Image, ImageDraw, ImageFont

    def font(kind, size, weight=None):
        f = ImageFont.truetype(str(ROOT / BRAND["fonts"][kind]), size)
        if weight:
            f.set_variation_by_name(weight)
        return f

    def wrap(text, f, width):
        lines, cur = [], ""
        for w in text.split():
            t = f"{cur} {w}".strip()
            if cur and f.getlength(t) > width:
                lines.append(cur)
                t = w
            cur = t
        return lines + [cur] if cur else lines

    def fits(f, lines, width):
        return all(f.getlength(l) <= width for l in lines)

    def fit_lines(text, kind, size, width, max_lines, weight=None, min_size=12):
        while True:  # shrink until it fits; never truncate
            f = font(kind, size, weight)
            lines = wrap(text, f, width)
            if size <= min_size or (len(lines) <= max_lines and fits(f, lines, width)):
                return f, lines
            size -= 2

    # static layer: header, title, footer
    bg = Image.new("RGB", (W, H), C["bg"])
    d = ImageDraw.Draw(bg)
    d.rounded_rectangle((60, 66, 150, 156), 20, fill=C["accent"])
    d.text((105, 113), "?", font=font("display", 70), fill=C["bg"], anchor="mm")
    d.text((176, 111), BRAND["channel"].upper(), font=font("display", 62), fill=C["text"], anchor="lm")
    d.text((W - 60, 111), f"EP {meta['ep']:03d}", font=font("body", 38, "Bold"), fill=C["accent"], anchor="rm")
    # title (<=3 lines) + description (<=2 lines) scaled together to fit the header block
    desc, room, tw = meta.get("description") or "", PY - 24 - 190, W - 120
    for pct in range(100, 30, -4):
        tf, df = font("display", 84 * pct // 100), font("body", max(22, 36 * pct // 100), "Medium")
        tl, dl = wrap(meta["article_title"], tf, tw), wrap(desc, df, tw)
        height = len(tl) * tf.size * 1.18 + (8 + len(dl) * df.size * 1.3 if dl else 0)
        if len(tl) <= 3 and len(dl) <= 2 and fits(tf, tl, tw) and fits(df, dl, tw) and height <= room:
            break
    y = 190 + (room - height) / 2
    for line in tl:
        d.text((60, y), line, font=tf, fill=C["text"])
        y += tf.size * 1.18
    y += 8
    for line in dl:
        d.text((60, y), line, font=df, fill=C["muted"])
        y += df.size * 1.3
    d.text((W // 2, 1878), "Source: Wikipedia · CC BY-SA 4.0", font=font("body", 28, "Medium"), fill=C["muted"], anchor="mm")

    mask = Image.new("L", (PW, PH), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, PW, PH), 36, fill=255)

    # panel sources: article images, else a branded text card
    srcs = []
    for img in meta["images"]:
        try:
            im = Image.open(img["path"])
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, C["panel"])
                flat.paste(im, mask=im.split()[-1])
                im = flat
            im = im.convert("RGB")
            s = max(PW * 1.15 / im.width, PH * 1.15 / im.height)
            srcs.append((im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS),
                         fit_lines("Image: " + credit(img), "body", 26, PW, 2, min_size=18)))
        except Exception as e:
            print(f"  skip image {img['path']}: {e}")
    if not srcs:
        card = Image.new("RGB", (int(PW * 1.15), int(PH * 1.15)), C["panel"])
        cd = ImageDraw.Draw(card)
        cf, cl = fit_lines(meta["article_title"].upper(), "display", 150, card.width - 160, 4)
        cy = card.height // 2 - len(cl) * cf.size * 0.6
        for line in cl:
            cd.text((card.width // 2, cy), line, font=cf, fill=C["accent"], anchor="mt")
            cy += cf.size * 1.2
        srcs.append((card, None))
        # ponytail: one text card when no free images exist; add AI image gen (mflux) if this happens often

    def kb(i, p):  # Ken Burns crop of source i at progress p in [0,1]
        im = srcs[i][0]
        z = 1 + 0.12 * (p if i % 2 == 0 else 1 - p)
        bw0 = min(im.width, im.height * PW / PH)
        bw, bh = bw0 / z, bw0 / z * PH / PW
        dx = 1 if i % 2 == 0 else -1
        f = 0.5 + dx * (p - 0.5) * 0.6
        cx = bw / 2 + (im.width - bw) * f
        cy = bh / 2 + (im.height - bh) * (1 - f if i % 3 == 2 else 0.5)
        return im.resize((PW, PH), Image.BILINEAR, box=(cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2))

    groups = group_words(words)
    cap_cache = {}

    def caption(gi, active):
        key = (gi, active)
        if key not in cap_cache:
            ws = [w[0].upper() for w in groups[gi][2]]
            size = 104
            while True:
                cf = font("display", size)
                sp = cf.getlength(" ")
                total = sum(cf.getlength(w) for w in ws) + sp * (len(ws) - 1)
                if total <= W - 100:
                    break
                size -= 6
            im = Image.new("RGBA", (W, 200), (0, 0, 0, 0))
            cd = ImageDraw.Draw(im)
            x = (W - total) / 2
            for j, w in enumerate(ws):
                cd.text((x, 100), w, font=cf, anchor="lm", fill=C["accent"] if j == active else C["text"],
                        stroke_width=8, stroke_fill="#000000")
                x += cf.getlength(w) + sp
            cap_cache[key] = im
        return cap_cache[key]

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    music = ROOT / BRAND["music"]
    cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-i", str(audio_path)]
    if music.exists():
        cmd += ["-stream_loop", "-1", "-i", str(music), "-filter_complex",
                f"[2:a]volume={BRAND['music_volume']}[m];[1:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    n, seg, fade = len(srcs), duration / len(srcs), 0.4
    gi = 0
    for fi in range(int(duration * FPS)):
        t = fi / FPS
        i = min(int(t / seg), n - 1)
        panel = kb(i, (t - i * seg) / seg)
        if i < n - 1 and t > (i + 1) * seg - fade:
            panel = Image.blend(panel, kb(i + 1, 0), 1 - ((i + 1) * seg - t) / fade)
        frame = bg.copy()
        frame.paste(panel, (PX, PY), mask)
        fd = ImageDraw.Draw(frame)
        if srcs[i][1]:
            cf, cl = srcs[i][1]
            for k, line in enumerate(cl):
                fd.text((PX, PY + PH + 14 + k * cf.size * 1.25), line, font=cf, fill=C["muted"])
        while gi < len(groups) - 1 and t >= groups[gi + 1][0]:
            gi += 1
        if groups and groups[gi][0] <= t < groups[gi][1]:
            active = max([j for j, w in enumerate(groups[gi][2]) if w[1] <= t] or [0])
            cap = caption(gi, active)
            frame.paste(cap, (0, 1560), cap)
        fd.rounded_rectangle((PX, 1826, PX + PW, 1836), 5, fill=C["panel"])
        fd.rounded_rectangle((PX, 1826, PX + max(10, int(PW * t / duration)), 1836), 5, fill=C["accent"])
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError("ffmpeg failed")


# ---------- orchestration ----------
def description(m):
    lines = [m["script"].split(". ")[0].rstrip(".") + ".", "",
             f"Read the full article: {m['url']}", "",
             f"Episode {m['ep']} of {BRAND['channel']}. {BRAND['tagline']}", "", "CREDITS",
             f"Text adapted from the Wikipedia article \"{m['article_title']}\", CC BY-SA 4.0: {m['url']}"]
    lines += [f"Image: \"{i['name']}\", {credit(i).replace(' · ', ', ')}: {i['page']}" for i in m["images"]]
    lines += ["Narration: AI voice (Kokoro TTS). Fonts: Anton, Inter (SIL OFL).", "",
              " ".join("#" + re.sub(r"\W", "", t) for t in BRAND["hashtags"] + m["tags"][:3])]
    return "\n".join(lines)[:4900]


def make(con, pageid):
    row = con.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    work = WORK / str(pageid)
    work.mkdir(parents=True, exist_ok=True)
    ep = row["ep"] or (con.execute("select coalesce(max(ep), 0) + 1 from pages").fetchone()[0])
    upsert(con, pageid, ep=ep)
    try:
        title, desc, text, pageimage = fetch_article(pageid)
        print(f"[{ep:03d}] {title}\n  writing script…")
        s = write_script(title, text)
        print(f"  \"{s['title']}\"\n  fetching images…")
        images = fetch_images(title, pageimage, work)
        print(f"  {len(images)} images\n  voicing…")
        narration = s["script"].strip() + " " + BRAND["outro_line"]
        words, dur = tts(narration, work / "voice.wav")
        m = dict(ep=ep, pageid=pageid, article_title=title, description=desc, url=page_url(title),
                 title=s["title"], script=s["script"], tags=s["tags"], images=images)
        m["yt_description"] = description(m)
        (work / "meta.json").write_text(json.dumps(m, indent=2))
        out = OUT / f"{ep:03d}-{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:50]}.mp4"
        print(f"  rendering {dur:.1f}s…")
        render(m, words, work / "voice.wav", dur, out)
        upsert(con, pageid, status="rendered", video_path=str(out))
        print(f"  done: {out}")
        return out
    except BaseException as e:
        upsert(con, pageid, status="failed", reason=f"{type(e).__name__}: {e}"[:300])
        raise


def upload(con, pageid, publish_at=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    row = con.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    if not row or row["status"] != "rendered":
        raise SystemExit(f"page {pageid} is not in 'rendered' state")
    m = json.loads((WORK / str(pageid) / "meta.json").read_text())
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    tok = SECRETS / "token.json"
    creds = Credentials.from_authorized_user_file(tok, scopes) if tok.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        cs = SECRETS / "client_secret.json"
        if not cs.exists():
            raise SystemExit(f"Missing {cs}; see README.md")
        creds = InstalledAppFlow.from_client_secrets_file(cs, scopes).run_local_server(port=0)
    tok.write_text(creds.to_json())
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at
    body = {"snippet": {"title": m["title"], "description": m["yt_description"],
                        "tags": BRAND["hashtags"] + m["tags"], "categoryId": "27"}, "status": status}
    yt = build("youtube", "v3", credentials=creds)
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(row["video_path"], chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    upsert(con, pageid, status="uploaded", video_id=resp["id"])
    print(f"  uploaded: https://youtube.com/shorts/{resp['id']} ({status['privacyStatus']})")


def selftest():
    g = group_words([["In", 0, .1], ["1932,", .1, .5], ["Australia", .5, .9], ["lost", .9, 1], ["a", 1, 1.1],
                     ["war", 1.1, 1.3], ["to", 1.3, 1.4], ["emus.", 1.4, 1.8]])
    assert [len(x[2]) for x in g] == [2, 3, 3], g
    assert g[0][1] == g[1][0] and abs(g[-1][1] - 2.2) < 1e-9
    assert strip_html('<bdi>Unknown author<span style="display: none;">Unknown author</span></bdi>') == "Unknown author"
    assert credit({"artist": "Unknown author", "license": "Public domain"}) == "Public domain"
    assert credit({"artist": "Fairfax", "license": "Public domain"}) == "Fairfax · Public domain"
    assert precheck({"title": "List of moths", "length": 9e9}) == "list page"
    assert precheck({"title": "X", "length": 9000, "pageviews": {"a": 100, "b": None}}).startswith("low views")
    assert precheck({"title": "X", "length": 9000, "pageviews": {"a": 500}}) is None
    assert precheck({"title": "X", "length": 9000, "pageviews": {"a": None}}) is None
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--url"); r.add_argument("--force", action="store_true")
    r.add_argument("--upload", action="store_true"); r.add_argument("--publish-at")
    a = sub.add_parser("add"); a.add_argument("urls", nargs="+"); a.add_argument("--force", action="store_true")
    u = sub.add_parser("upload"); u.add_argument("pageid", type=int); u.add_argument("--publish-at")
    lg = sub.add_parser("log"); lg.add_argument("n", nargs="?", type=int, default=30)
    sub.add_parser("selftest")
    args = ap.parse_args()
    for d in (DB.parent, WORK, OUT, SECRETS):
        d.mkdir(exist_ok=True)
    con = db()
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "add":
        for url in args.urls:
            add_curated(con, url, args.force)
    elif args.cmd == "log":
        for row in con.execute("select * from pages order by updated desc limit ?", (args.n,)):
            print(f"{row['status']:<9} {row['score'] or '':>2} {row['source']:<8} {row['pageid']:>9}  "
                  f"{row['title'][:45]:<45}  {(row['reason'] or '')[:60]}")
        print(dict(con.execute("select status, count(*) from pages group by status").fetchall()))
    elif args.cmd == "upload":
        upload(con, args.pageid, args.publish_at)
    elif args.cmd == "run":
        if args.url:
            pageid = add_curated(con, args.url, args.force)
            if pageid is None:
                return
        else:
            row = con.execute("select pageid from pages where status='queued' "
                              "order by source='curated' desc, created limit 1").fetchone()
            pageid = row["pageid"] if row else None
            if pageid is None:
                print("discovering…")
                pageid = discover(con)
        make(con, pageid)
        if args.upload:
            upload(con, pageid, args.publish_at)


if __name__ == "__main__":
    main()
