"""Shorts Machine: Wikipedia pages, web articles, feeds, Hacker News, arXiv and NASA APOD -> branded YouTube Shorts.

  ./rw run [--url URL] [--upload] [--publish-at ISO]   make one video
  ./rw add URL [URL ...]                              queue curated pages
  ./rw discover [--count N] [--topic KEY --degree 1|2|3|any]   queue N Wikipedia ideas (random, or around a topic)
  ./rw discover --source hackernews|apod|arxiv --topic T|rss --feed URL   queue ideas from other sources
  ./rw upload [--ep N] [--publish-at ISO]             upload every rendered video (or just episode N)
  ./rw auto [--dry-run]                               one scheduled run (make next video, upload)
  ./rw schedule                                       install/remove the launchd job from brand.json "schedule"
  ./rw unpublish --ep N [--keep-local]                delete an uploaded episode from YouTube (and locally)
  ./rw rerender --ep N                                re-voice/re-render from the saved (edited) script
  ./rw log [N]                                        show the page log
  ./rw web [--port 8765]                              local studio UI in the browser
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
    for col in ("potential integer", "rank integer", "via text", "origin text", "ref text"):  # added after v1; ignore if present
        try:
            con.execute(f"alter table pages add column {col}")
        except sqlite3.OperationalError:
            pass
    return con


QUEUE_ORDER = "order by rank is null, rank, source='curated' desc, created"  # manual drag order first


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


SCORE_PROMPT = """You choose topics for a YouTube Shorts channel that turns articles (Wikipedia, news, papers, blogs) into 50-second stories.
Rate how likely this article is to make a viewer think "wait, what?".
Most random articles (minor species, small villages, obscure athletes, routine politicians, ordinary companies, albums) score 1-4.
Score 7+ only if the text contains a genuinely surprising fact, strange event, record, mystery, scandal or conflict.
{context}
Title: {title}
Text: {text}

Reply as JSON: {{"score": <1-10>, "hook": "<the single most surprising fact, one sentence>", "reason": "<short>"}}"""

SCRIPT_PROMPT = """Write the narration for a YouTube Short about the article below.
Rules:
- 100 to 120 words (about 7 sentences of 15 words). Plain spoken English, short sentences, no lists, no emojis, no hashtags.
- The first sentence is a hook of under 14 words built on the most surprising fact.
- Tell it as a story with a payoff at the end. Use ONLY facts stated in the article. Do not invent anything.
- Do not say "Wikipedia", "this article", "did you know", "in this video", or ask viewers to like or subscribe.

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


CANDIDATE_PROPS = dict(prop="info|pageprops|pageviews|extracts", ppprop="disambiguation",
                       exintro=1, explaintext=1, exlimit=20)
JUNK_TITLE = re.compile(r"^(List|Lists|Index|Outline|Timeline|Glossary) of |^\d{1,4}s?( BC| AD)?$|^\d{4} in |"
                        r"^(January|February|March|April|May|June|July|August|September|October|November|December) \d")


def evaluate(con, p, source, via=None, seed=None):
    """Filter + LLM-score one candidate page; logs it and queues it if good. Returns True if queued."""
    if p.get("missing") or con.execute("select 1 from pages where pageid=?", (p["pageid"],)).fetchone():
        return False
    base = dict(title=p["title"], url=page_url(p["title"]), source=source, via=via)
    reason = precheck(p)
    if reason:
        upsert(con, p["pageid"], status="rejected", reason=reason, **base)
        return False
    context = (f"\nThe channel is currently exploring topics connected to {seed}. "
               f"The article must still be fascinating on its own, not just related.\n") if seed else ""
    s = llm(SCORE_PROMPT.format(title=p["title"], text=p.get("extract", "")[:3000], context=context), 0.2)
    score = int(s.get("score", 0) or 0)
    ok = score >= BRAND["min_score"]
    upsert(con, p["pageid"], status="queued" if ok else "rejected", score=score,
           reason=f"llm: {s.get('hook') if ok else s.get('reason')}", **base)
    print(f"  {score:>2}/10  {p['title']}" + (f"   [{via}]" if via else "") + ("  -> queued" if ok else ""))
    return ok


def discover(con, want=1):
    """Pull random pages until `want` pass the filters; they are queued. Returns their pageids."""
    found = []
    for _ in range(10 + 8 * want):  # ~1 in 50 random pages passes; each batch is 20 pages
        res = wiki(generator="random", grnnamespace=0, grnlimit=20, **CANDIDATE_PROPS)
        for p in res["query"]["pages"]:
            if evaluate(con, p, "random"):
                found.append(p["pageid"])
                if len(found) >= want:
                    return found
    if not found:
        raise SystemExit("No interesting page found; run again.")
    print(f"found {len(found)} of {want}; run again for more")
    return found


def discover_source(con, origin, want=5, arg=None):
    """Queue up to `want` interesting items from another source (hackernews, rss, arxiv, apod)."""
    import sources
    cands = sources.discover(origin, want * 4, arg)
    print(f"{len(cands)} candidates from {origin}" + (f" ({arg})" if arg else ""))
    found = []
    for c in cands:
        if len(found) >= want:
            break
        origin_c = c["origin"]
        try:
            a = sources.fetch_article(origin_c, c["ref"])
        except (SystemExit, Exception) as e:
            print(f"   –  {c['title'][:60]}  ({e})")
            continue
        pid = sources.synthetic_id(a["key"])
        if con.execute("select 1 from pages where pageid=?", (pid,)).fetchone():
            continue
        base = dict(title=a["title"], url=a["url"], source=origin if origin == "rss" else "random", origin=a["origin"], ref=c["ref"])
        s = llm(SCORE_PROMPT.format(title=a["title"], text=a["text"][:3000], context=""), 0.2)
        score = int(s.get("score", 0) or 0)
        ok = score >= BRAND["min_score"]
        upsert(con, pid, status="queued" if ok else "rejected", score=score,
               reason=f"llm: {s.get('hook') if ok else s.get('reason')}", **base)
        print(f"  {score:>2}/10  {a['title'][:70]}" + ("  -> queued" if ok else ""))
        if ok:
            found.append(pid)
    print(f"found {len(found)} of {want}")
    return found


def wiki_all(max_calls=3, **params):
    """wiki() following 'continue' for up to max_calls requests."""
    out, cont = [], {}
    for _ in range(max_calls):
        r = wiki(**params, **cont)
        out.append(r)
        if "continue" not in r:
            break
        cont = r["continue"]
    return out


def resolve_seed(key):
    """Any key ('nepal', 'black holes', 'Beatles') -> best matching article title."""
    p = wiki(titles=key, redirects=1, prop="pageprops", ppprop="disambiguation")["query"]["pages"][0]
    if not p.get("missing") and "disambiguation" not in p.get("pageprops", {}):
        return p["title"]
    hits = wiki(list="search", srsearch=key, srlimit=1, srnamespace=0)["query"]["search"]
    if not hits:
        raise SystemExit(f"No Wikipedia article found for '{key}'")
    return hits[0]["title"]


def link_graph(seed, max_degree, frontier_size=25):
    """Map the seed's network: {degree: {title: [strength, path]}}.
    Degree 1 = linked from the seed, linking to it, or mentioning it. Degrees 2-3 follow outgoing links
    from the best-connected pages one level closer. Strength = number of distinct links found to the page."""
    import random
    levels = {d: {} for d in range(1, max_degree + 1)}
    seen = {seed}

    def add(d, title, parent_path):
        if title in seen and title not in levels[d]:
            return
        seen.add(title)
        node = levels[d].setdefault(title, [0, parent_path + [title]])
        node[0] += 1

    for r in wiki_all(3, titles=seed, prop="links", plnamespace=0, pllimit="max"):
        for pg in r["query"]["pages"]:
            for l in pg.get("links", []):
                add(1, l["title"], [seed])
    for r in wiki_all(2, list="backlinks", bltitle=seed, blnamespace=0, bllimit="max", blfilterredir="nonredirects"):
        for l in r["query"]["backlinks"]:
            add(1, l["title"], [seed])
    for l in wiki(list="search", srsearch=seed, srlimit=100, srnamespace=0)["query"]["search"]:
        add(1, l["title"], [seed])

    for d in range(2, max_degree + 1):
        prev = sorted(levels[d - 1].items(), key=lambda kv: -kv[1][0])
        prev = [kv for kv in prev if not JUNK_TITLE.search(kv[0])]
        # best-connected parents plus a random slice, so distant results aren't always the same
        frontier = prev[:frontier_size // 2] + random.sample(prev[frontier_size // 2:], min(frontier_size // 2, max(0, len(prev) - frontier_size // 2)))
        for i in range(0, len(frontier), 25):
            chunk = dict(frontier[i:i + 25])
            for r in wiki_all(3, titles="|".join(chunk), redirects=1, prop="links", plnamespace=0, pllimit="max"):
                back = {x["to"]: x["from"] for x in r["query"].get("redirects", [])}
                for pg in r["query"]["pages"]:
                    parent = chunk.get(pg["title"]) or chunk.get(back.get(pg["title"]))
                    if not parent:
                        continue
                    for l in pg.get("links", []):
                        add(d, l["title"], parent[1])
    return levels


def explore(con, key, degree="any", want=5, max_checks=300):
    """Queue up to `want` interesting pages within `degree` hops of the topic `key`."""
    import random
    seed = resolve_seed(key)
    degrees = [1, 2, 3] if degree == "any" else [int(degree)]
    print(f"exploring the network around '{seed}' (degree {degree})…")
    levels = link_graph(seed, max(degrees))
    for d in sorted(levels):
        print(f"  degree {d}: {len(levels[d])} pages")
    known = {r[0] for r in con.execute("select title from pages")}
    cands = [(t, n) for d in degrees for t, n in levels[d].items() if t not in known and not JUNK_TITLE.search(t)]
    random.shuffle(cands)
    cands.sort(key=lambda c: (-min(c[1][0], 5), len(c[1][1])))  # strongest links first, then closest
    found, checked = [], 0
    for i in range(0, min(len(cands), max_checks), 20):
        batch = dict(cands[i:i + 20])
        res = wiki(titles="|".join(batch), redirects=1, **CANDIDATE_PROPS)
        redirects = {r["to"]: r["from"] for r in res["query"].get("redirects", [])}
        for p in res["query"]["pages"]:
            checked += 1
            node = batch.get(p.get("title")) or batch.get(redirects.get(p.get("title")))
            via = " → ".join(node[1]) if node else seed
            if evaluate(con, p, "seed", via=via, seed=seed):
                found.append(p["pageid"])
                if len(found) >= want:
                    return found
    print(f"found {len(found)} of {want} after checking {checked} pages" + ("" if found else "; try another degree"))
    return found


def resolve(url_or_title):
    t = url_or_title
    if "/wiki/" in t:
        t = urllib.parse.unquote(t.split("/wiki/", 1)[1].split("#")[0])
    p = wiki(titles=t.replace("_", " "), redirects=1)["query"]["pages"][0]
    if p.get("missing"):
        raise SystemExit(f"Not found on Wikipedia: {url_or_title}")
    return p["pageid"], p["title"]


def add_curated(con, url, force=False, make_now=False):
    import sources
    origin, ref = sources.detect(url)
    if origin == "wikipedia":
        pageid, title = resolve(url)
        extra = dict(url=page_url(title), origin="wikipedia")
    else:
        a = sources.fetch_article(origin, ref)  # also validates the link has usable text
        pageid, title = sources.synthetic_id(a["key"]), a["title"]
        extra = dict(url=a["url"], origin=a["origin"], ref=ref)
    row = con.execute("select status from pages where pageid=?", (pageid,)).fetchone()
    if make_now and row and row["status"] == "queued":
        return pageid  # already waiting in the queue: just make it, keep its source/reason
    if row and row["status"] in ("rendered", "uploaded", "queued") and not force:
        print(f"skip: {title} is already {row['status']} (use --force)")
        return None
    upsert(con, pageid, title=title, source="curated", status="queued", reason="curated", **extra)
    print(f"queued: {title}" + ("" if extra["origin"] == "wikipedia" else f"  [{extra['origin']}]"))
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
    """'Ludwig Wegmann · CC BY-SA 3.0', just 'Public domain' when the author is unknown, '' for own images."""
    if not img.get("license"):
        return ""
    artist = img.get("artist") or ""
    return img["license"] if not artist or UNKNOWN.match(artist) else f"{artist} · {img['license']}"


BAD_FILE = re.compile(r"icon|logo|flag|symbol|commons-|wiki|ambox|question_book|edit-|stub|padlock|signature|location_map|blank", re.I)
OK_LICENSE = re.compile(r"^(cc[ -]?by|cc0|cc-zero|public domain|pd)", re.I)
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def commons_files(pages, limit):
    """imageinfo pages -> usable, free-licensed image dicts."""
    got = []
    for f in pages:
        ii = (f.get("imageinfo") or [{}])[0]
        meta = {k: v.get("value") for k, v in ii.get("extmetadata", {}).items()}
        if (BAD_FILE.search(f["title"]) or ii.get("mime") not in ("image/jpeg", "image/png", "image/webp")
                or ii.get("width", 0) < 600 or ii.get("height", 0) < 450
                or meta.get("NonFree") or not OK_LICENSE.match(meta.get("LicenseShortName") or "")):
            continue
        got.append(dict(source="commons", url=ii.get("thumburl") or ii["url"], thumb=ii.get("thumburl") or ii["url"],
                        page=ii.get("descriptionurl"), name=f["title"][5:],
                        artist=strip_html(meta.get("Artist")) or "unknown", license=meta.get("LicenseShortName")))
        if len(got) >= limit:
            break
    return got


IMAGE_PROPS = dict(prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=1400)


def src_article(title, limit=12, pageimage=None):
    pages = wiki(generator="images", titles=title, gimlimit=50, **IMAGE_PROPS).get("query", {}).get("pages", [])
    pages.sort(key=lambda f: f["title"].replace(" ", "_") != f"File:{pageimage}")  # lead image first
    return commons_files(pages, limit)


def src_commons(q, limit=24):
    r = get_json(COMMONS_API, {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
                               "gsrsearch": f"{q} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 40, **IMAGE_PROPS})
    pages = sorted(r.get("query", {}).get("pages", []), key=lambda f: f.get("index", 0))
    return commons_files(pages, limit)


def src_openverse(q, limit=24):
    # anonymous Openverse requests are capped at 20 per page
    r = get_json("https://api.openverse.org/v1/images/", {"q": q, "license_type": "commercial", "page_size": min(limit, 20)})
    return [dict(source="openverse", url=x["url"], thumb=x.get("thumbnail") or x["url"], page=x.get("foreign_landing_url"),
                 name=x.get("title") or "image", artist=x.get("creator") or "unknown",
                 license=f"CC {x['license'].upper()} {x.get('license_version', '')}".strip(),
                 tags=" ".join(t["name"] for t in x.get("tags") or []))
            for x in r.get("results", []) if (x.get("width") or 0) >= 600]


def src_nasa(q, limit=24):
    items = get_json("https://images-api.nasa.gov/search", {"q": q, "media_type": "image"})["collection"]["items"]
    out = []
    for it in items[:limit]:
        d, thumb = it["data"][0], it["links"][0]["href"]
        out.append(dict(source="nasa", url=thumb.replace("~thumb.", "~large."), thumb=thumb,
                        page=f"https://images.nasa.gov/details/{d['nasa_id']}", name=d.get("title", "NASA image"),
                        artist=d.get("photographer") or d.get("center") or "NASA", license="Public domain (NASA)"))
    return out


def src_met(q, limit=12):
    ids = get_json("https://collectionapi.metmuseum.org/public/collection/v1.1/search",  # v1 retired 2026-10-01
                   {"q": q, "hasImages": "true", "limit": limit * 2}).get("objectIDs") or []
    out = []
    for oid in ids[:limit * 2]:
        o = get_json(f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{oid}")
        if o.get("isPublicDomain") and o.get("primaryImage"):
            out.append(dict(source="met", url=o["primaryImage"], thumb=o.get("primaryImageSmall") or o["primaryImage"],
                            page=o.get("objectURL"), name=o.get("title") or "artwork",
                            artist=o.get("artistDisplayName") or "The Met", license="CC0 (The Met)"))
            if len(out) >= limit:
                break
    return out


IMAGE_SOURCES = {"article": src_article, "commons": src_commons, "openverse": src_openverse, "nasa": src_nasa, "met": src_met}


def download_image(img, dest):
    """Download one candidate into the episode folder; returns it with 'path' set, or None."""
    from PIL import Image
    import hashlib, io
    path = dest / f"img_{hashlib.sha1(img['url'].encode()).hexdigest()[:10]}.jpg"
    if not path.exists():
        time.sleep(0.5)  # Wikimedia rate-limits bursts
        data = fetch(img["url"])
        im = Image.open(io.BytesIO(data))  # also validates that it really is an image
        im.convert("RGB").save(path, "JPEG", quality=92) if im.format not in ("JPEG",) else path.write_bytes(data)
    return {k: v for k, v in img.items() if k not in ("thumb", "tags")} | {"path": str(path)}


def fetch_images(title, pageimage, dest, want=4, limit=6, own=None):
    """Automatic pick: the article's own images (Wikipedia, or `own` from another source),
    topped up from a Commons search, then on-topic Openverse."""
    got = list(own) if own is not None else src_article(title, limit, pageimage)
    for extra in (lambda: src_commons(title, 10), lambda: src_openverse(title, 10)):
        if len(got) >= want:
            break
        try:
            words = {w.lower() for w in re.findall(r"\w{4,}", title)}
            for c in extra():
                hay = (c["name"] + " " + c.get("tags", "")).lower()
                if c["url"] not in {g["url"] for g in got} and words & set(re.findall(r"\w{4,}", hay)):
                    got.append(c)
                if len(got) >= want:
                    break
        except Exception as e:
            print(f"  image search failed: {e}")
    out = []
    for g in got[:limit]:
        try:
            out.append(download_image(g, dest))
        except Exception as e:
            print(f"  image failed: {g['name']} ({e})")
    return out


ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
        "sixteen seventeen eighteen nineteen").split()
TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
IRREGULAR_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
                 "nine": "ninth", "twelve": "twelfth"}
# after these words a Roman numeral is a number ("World War Two"); otherwise it is regnal ("Elizabeth the Second")
CARDINAL_AFTER = {"war", "part", "chapter", "volume", "vol", "phase", "act", "book", "type", "class", "mark", "mk",
                  "apollo", "gemini", "bowl", "episode", "season", "series", "round", "stage", "grade", "level",
                  "generation", "article", "title", "section", "vatican", "schedule", "category", "division", "league"}


def number_words(n, ordinal=False):
    w = ONES[n] if n < 20 else TENS[n // 10] + (f"-{ONES[n % 10]}" if n % 10 else "")
    if ordinal:
        head, _, last = w.rpartition("-")
        last = IRREGULAR_ORD.get(last) or (last[:-1] + "ieth" if last.endswith("y") else last + "th")
        w = f"{head}-{last}" if head else last
    return w


def roman(s):
    """'XIV' -> 14, or None if not a canonical Roman numeral (1-99)."""
    v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
    if not s or any(c not in v for c in s):
        return None
    n = sum(-v[a] if b and v[a] < v[b] else v[a] for a, b in zip(s, list(s[1:]) + [None]))
    canon, k = "", n
    for r, val in (("XC", 90), ("L", 50), ("XL", 40), ("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1)):
        while k >= val:
            canon, k = canon + r, k - val
    return n if 0 < n < 100 and canon == s else None


def speakable(text):
    """Text the voice model reads correctly: 'World War II' -> 'World War Two', 'Louis XIV' -> 'Louis the Fourteenth'."""
    def fix(m):
        word, num = m[1], m[2]
        n = roman(num)
        cardinal = word.lower() in CARDINAL_AFTER
        regnal = word[0].isupper() and not cardinal  # names are capitalised: "Charles III", not "and II"
        if not n or not (cardinal or regnal) or (not cardinal and num in ("I", "X", "C", "L")):  # "Malcolm X"
            return m[0]
        return f"{word} {number_words(n).title()}" if cardinal else f"{word} the {number_words(n, True).title()}"
    return re.sub(r"\b([A-Za-z]+) ([IVXLC]{1,7})\b(?![\w-])", fix, text)


def tts(text, dest):
    """Kokoro narration -> wav + per-word timings [(word, start, end)]."""
    text = speakable(text)
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


# ---------- typography (Google Fonts, open licences) ----------
KOKORO_VOICES = {  # English voices bundled with Kokoro-82M: a* = American, b* = British; f = female, m = male
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky", "af_aoede", "af_kore", "af_nova", "af_river",
    "am_adam", "am_michael", "am_fenrir", "am_puck", "am_echo", "am_eric", "am_liam", "am_onyx",
    "bf_emma", "bf_isabella", "bf_alice", "bf_lily", "bm_george", "bm_lewis", "bm_daniel", "bm_fable"}


def google_fonts():
    """The Google Fonts catalogue (family, category, popularity), cached for a week in data/."""
    cache = DB.parent / "google_fonts.json"
    if not cache.exists() or time.time() - cache.stat().st_mtime > 7 * 86400:
        d = get_json("https://fonts.google.com/metadata/fonts")
        cache.write_text(json.dumps([{"family": f["family"], "category": f["category"], "popularity": f["popularity"]}
                                     for f in d["familyMetadataList"] if f.get("isOpenSource", True)]))
    return json.loads(cache.read_text())


def install_font(family):
    """Download a Google Fonts family's .ttf (plus licence) into brand/fonts/. Returns its project-relative path."""
    slug = re.sub(r"[^a-z0-9]", "", family.lower())
    for lic in ("ofl", "apache", "ufl"):
        try:
            files = get_json(f"https://api.github.com/repos/google/fonts/contents/{lic}/{slug}")
        except urllib.error.HTTPError:
            continue
        ttfs = [f for f in files if f["name"].endswith(".ttf") and "Italic" not in f["name"]]
        if not ttfs:
            continue
        # prefer a variable font (has every weight), then Bold/Black, then Regular
        rank = lambda f: (0 if "[" in f["name"] else 1 if re.search(r"-(Bold|Black|ExtraBold)\.ttf", f["name"])
                          else 2 if "-Regular" in f["name"] else 3)
        pick = sorted(ttfs, key=rank)[0]
        dest = ROOT / "brand" / "fonts" / f"{re.sub(r'[^A-Za-z0-9]', '', family)}.ttf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(fetch(pick["download_url"]))
        for f in files:
            if f["name"] in ("OFL.txt", "LICENSE.txt", "UFL.txt"):
                (dest.parent / f"{dest.stem}-{f['name']}").write_bytes(fetch(f["download_url"]))
        return str(dest.relative_to(ROOT))
    raise SystemExit(f"Font '{family}' not found in the Google Fonts repository")


def ensure_fonts():
    """Fresh clone: re-download any brand font that is missing locally (brand/fonts is git-ignored)."""
    for kind in ("display", "body"):
        if not (ROOT / BRAND["fonts"][kind]).exists():
            family = BRAND["fonts"].get(f"{kind}_family") or {"display": "Anton", "body": "Inter"}[kind]
            print(f"  downloading font {family}…")
            BRAND["fonts"][kind] = install_font(family)


# ---------- rendering ----------
def render(meta, words, audio_path, duration, out_path, frame_at=None):
    """Draw the branded video and encode it to out_path. With frame_at=seconds, save just that frame as an image."""
    import subprocess, imageio_ffmpeg
    from PIL import Image, ImageDraw, ImageFont
    ensure_fonts()

    def font(kind, size, weight=None):
        f = ImageFont.truetype(str(ROOT / BRAND["fonts"][kind]), size)
        weight = weight or (BRAND["fonts"].get("display_weight", "Bold") if kind == "display" else None)
        try:
            f.set_variation_by_name(weight)  # variable fonts only; static fonts keep their single weight
        except Exception:
            pass
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
    logo, mark = ROOT / BRAND.get("logo", "brand/logo.png"), BRAND.get("logo_text", "?")[:2]
    if BRAND.get("logo") and logo.exists():  # uploaded logo image, fitted into the mark
        lm = Image.open(logo).convert("RGBA")
        lm.thumbnail((78, 78), Image.LANCZOS)
        bg.paste(lm, (105 - lm.width // 2, 111 - lm.height // 2), lm)
    else:
        d.text((105, 113), mark, font=font("display", 70 if len(mark) < 2 else 46), fill=C["bg"], anchor="mm")
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
    d.text((W // 2, 1878), meta.get("source_label") or "Source: Wikipedia · CC BY-SA 4.0", font=font("body", 28, "Medium"), fill=C["muted"], anchor="mm")

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
                         fit_lines("Image: " + credit(img), "body", 26, PW, 2, min_size=18) if credit(img) else None))
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

    n, seg, fade = len(srcs), duration / len(srcs), 0.4

    def frame(t):
        i = min(int(t / seg), n - 1)
        panel = kb(i, (t - i * seg) / seg)
        if i < n - 1 and t > (i + 1) * seg - fade:
            panel = Image.blend(panel, kb(i + 1, 0), 1 - ((i + 1) * seg - t) / fade)
        im = bg.copy()
        im.paste(panel, (PX, PY), mask)
        fd = ImageDraw.Draw(im)
        if srcs[i][1]:
            cf, cl = srcs[i][1]
            for k, line in enumerate(cl):
                fd.text((PX, PY + PH + 14 + k * cf.size * 1.25), line, font=cf, fill=C["muted"])
        gi = max([k for k, g in enumerate(groups) if g[0] <= t] or [0])
        if groups and groups[gi][0] <= t < groups[gi][1]:
            active = max([j for j, w in enumerate(groups[gi][2]) if w[1] <= t] or [0])
            cap = caption(gi, active)
            im.paste(cap, (0, 1560), cap)
        fd.rounded_rectangle((PX, 1826, PX + PW, 1836), 5, fill=C["panel"])
        fd.rounded_rectangle((PX, 1826, PX + max(10, int(PW * t / duration)), 1836), 5, fill=C["accent"])
        return im

    if frame_at is not None:
        frame(frame_at).save(out_path)
        return

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

    for fi in range(int(duration * FPS)):
        proc.stdin.write(frame(fi / FPS).tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError("ffmpeg failed")


# ---------- orchestration ----------
def description(m):
    lines = [m["script"].split(". ")[0].rstrip(".") + ".", "",
             f"Source: {m['url']}", "",
             f"Episode {m['ep']} of {BRAND['channel']}. {BRAND['tagline']}", "", "CREDITS",
             m.get("credit") or f"Text adapted from the Wikipedia article \"{m['article_title']}\", CC BY-SA 4.0: {m['url']}"]
    lines += [f"Image: \"{i['name']}\", {credit(i).replace(' · ', ', ')}: {i['page']}" for i in m["images"] if credit(i)]
    lines += ["Narration: AI voice (Kokoro TTS). Fonts: Anton, Inter (SIL OFL).", "",
              " ".join("#" + re.sub(r"\W", "", t) for t in BRAND["hashtags"] + m["tags"][:3])]
    return "\n".join(lines)[:4900]


APPEAL_PROMPT = """Rate this YouTube Short as it would appear to someone scrolling the Shorts feed.
Title: {title}
Opening line: {hook}
Topic: {topic} ({desc})

hook: 1-10, how strongly the title and opening line create curiosity to keep watching. Vague or textbook = low.
appeal: 1-10, how interesting the topic is to a broad international audience. Niche, local or technical = low.
Reply as JSON: {{"hook": <1-10>, "appeal": <1-10>, "why": "<one short sentence>"}}"""

# ponytail: hand-set weights with no performance data yet; refit them against real YouTube views
# (Analytics API) once ~30 videos are public.
WEIGHTS = {"demand": 25, "hook": 25, "appeal": 15, "reach": 10, "interest": 10, "visuals": 10, "length": 5}


def potential_score(views60, langs, hook, appeal, interest, images, seconds):
    """Signals -> (0-100 score, {signal: 0-1}). Pure function so it can be tested and refitted."""
    import math
    parts = {
        "demand": min(1, math.log10(views60 + 1) / 5),        # 100k views in 60 days = full marks
        "hook": hook / 10, "appeal": appeal / 10,
        "reach": min(1, langs / 60),                          # 60+ Wikipedia language editions
        "interest": interest / 10,
        "visuals": min(1, images / 4),
        "length": max(0, 1 - max(0, abs(seconds - 40) - 10) / 20),  # 30-50s ideal, 0 at <=10s or >=70s
    }
    return round(sum(WEIGHTS[k] * v for k, v in parts.items())), {k: round(v, 2) for k, v in parts.items()}


def score_video(con, pageid):
    """Compute and store the potential score for a rendered video."""
    import soundfile as sf
    row = con.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    mpath = WORK / str(pageid) / "meta.json"
    m = json.loads(mpath.read_text())
    if m.get("origin", "wikipedia") == "wikipedia":
        p = wiki(pageids=pageid, prop="pageviews|langlinks", pvipdays=60, lllimit="max")["query"]["pages"][0]
        views = sum(v or 0 for v in (p.get("pageviews") or {}).values())
        langs = len(p.get("langlinks") or [])
    else:
        # ponytail: no pageviews/language editions outside Wikipedia; use the source's popularity
        # (e.g. HN points) for demand, else a neutral midpoint, and a neutral reach
        views = int(10 ** (5 * (m.get("popularity") if m.get("popularity") is not None else 0.5))) - 1
        langs = 30
    hook_line = m["script"].split(". ")[0]
    a = llm(APPEAL_PROMPT.format(title=m["title"], hook=hook_line, topic=m["article_title"], desc=m.get("description", "")), 0.2)
    clamp = lambda v: max(1, min(10, int(v or 5)))
    hook, appeal = clamp(a.get("hook")), clamp(a.get("appeal"))
    seconds = sf.info(str(WORK / str(pageid) / "voice.wav")).duration
    score, parts = potential_score(views, langs, hook, appeal, row["score"] or hook, len(m["images"]), seconds)
    m["potential"] = dict(score=score, parts=parts, why=a.get("why", ""), views60=views, languages=langs,
                          seconds=round(seconds, 1), scored=time.strftime("%Y-%m-%d"))
    mpath.write_text(json.dumps(m, indent=2))
    con.execute("update pages set potential=? where pageid=?", (score, pageid))
    con.commit()
    print(f"  potential {score}/100  (views60={views}, languages={langs}, hook={hook}, appeal={appeal})")
    return score


def make(con, pageid):
    row = con.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    work = WORK / str(pageid)
    work.mkdir(parents=True, exist_ok=True)
    ep = row["ep"] or (con.execute("select coalesce(max(ep), 0) + 1 from pages").fetchone()[0])
    upsert(con, pageid, ep=ep)
    try:
        origin = row["origin"] or "wikipedia"
        if origin == "wikipedia":
            title, desc, text, pageimage = fetch_article(pageid)
            src = dict(url=page_url(title), own=None)
        else:
            import sources
            a = sources.fetch_article(origin, row["ref"])
            title, desc, text, pageimage = a["title"], a["description"], a["text"], None
            src = dict(url=a["url"], own=a["images"], credit=a["credit"], source_label=a["source_label"],
                       popularity=a.get("popularity"))
        print(f"[{ep:03d}] {title}" + ("" if origin == "wikipedia" else f"  [{origin}]") + "\n  writing script…")
        s = write_script(title, text)
        print(f"  \"{s['title']}\"\n  fetching images…")
        images = fetch_images(title, pageimage, work, own=src.pop("own"))
        print(f"  {len(images)} images")
        m = dict(ep=ep, pageid=pageid, origin=origin, article_title=title, description=desc,
                 title=s["title"], script=s["script"], tags=s["tags"], images=images, **src)
        out = OUT / f"{ep:03d}-{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:50]}.mp4"
        produce(con, m, out)
    except BaseException as e:
        upsert(con, pageid, status="failed", reason=f"{type(e).__name__}: {e}"[:300])
        raise
    try:
        score_video(con, pageid)
    except Exception as e:  # a failed score never costs us the video
        print(f"  could not score: {e}")
    return out


def produce(con, m, out):
    """Voice + render a video from its meta (script, title, images) and save everything."""
    work = WORK / str(m["pageid"])
    print("  voicing…")
    words, dur = tts(m["script"].strip() + " " + BRAND["outro_line"], work / "voice.wav")
    m["yt_description"] = description(m)
    (work / "meta.json").write_text(json.dumps(m, indent=2))
    print(f"  rendering {dur:.1f}s…")
    render(m, words, work / "voice.wav", dur, out)
    upsert(con, m["pageid"], status="rendered", video_path=str(out))
    print(f"  done: {out}")


# ---------- automation (macOS launchd) ----------
SCHEDULE_DEFAULTS = {"enabled": False, "times": ["09:00"], "days": [0, 1, 2, 3, 4, 5, 6], "upload": True,
                     "publish": "private", "publish_time": "15:00", "fallback": "wikipedia", "fallback_arg": ""}
LAUNCH_LABEL = "com.shortsmachine.auto"
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
AUTO_LOG = DB.parent / "auto.log"


def schedule_settings():
    return SCHEDULE_DEFAULTS | BRAND.get("schedule", {})


def notify(msg):
    import subprocess
    safe = msg.replace("\\", "").replace('"', "'")[:200]
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "Shorts Machine"'], capture_output=True)


def schedule_apply():
    """Install (or remove) the launchd job that runs `rw auto` at the scheduled times."""
    import os, plistlib, subprocess
    sch, domain = schedule_settings(), f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_PLIST)], capture_output=True)  # drop any old job
    if not sch["enabled"]:
        LAUNCH_PLIST.unlink(missing_ok=True)
        print("automation is off (launchd job removed)")
        return
    days = sorted(set(sch["days"]))
    slots = [dict(Hour=int(t[:2]), Minute=int(t[3:])) for t in sch["times"]]
    # launchd: Weekday 0 = Sunday; leave it out when every day is selected
    intervals = slots if len(days) == 7 else [s | {"Weekday": d} for s in slots for d in days]
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCH_PLIST, "wb") as f:
        plistlib.dump({"Label": LAUNCH_LABEL, "ProgramArguments": ["/bin/bash", str(ROOT / "rw"), "auto"],
                       "WorkingDirectory": str(ROOT), "StartCalendarInterval": intervals,
                       "StandardOutPath": str(AUTO_LOG), "StandardErrorPath": str(AUTO_LOG),
                       "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}, f)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_PLIST)], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"launchctl refused the job: {r.stderr.strip()}")
    print(f"automation on: {', '.join(sch['times'])} on {'every day' if len(days) == 7 else ', '.join('Sun Mon Tue Wed Thu Fri Sat'.split()[d] for d in days)}"
          f"; job {LAUNCH_LABEL}, log data/auto.log")


def schedule_loaded():
    import os, subprocess
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_LABEL}"], capture_output=True).returncode == 0


def publish_time_utc(hhmm, min_gap_hours=2):
    """Next local HH:MM at least `min_gap_hours` from now (time to review), as YouTube's UTC timestamp."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now().astimezone()
    t = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
    while t < now + timedelta(hours=min_gap_hours):
        t += timedelta(days=1)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def auto(con, dry_run=False):
    """One scheduled run: make the next queued video (discovering one if the queue is empty), then upload."""
    import fcntl
    lock = open(DB.parent / "auto.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another automated run is still going; skipping this one")
    sch = schedule_settings()
    print(f"=== automated run {time.strftime('%Y-%m-%d %H:%M')}" + (" (dry run)" if dry_run else ""))
    try:
        row = con.execute(f"select pageid, title from pages where status='queued' {QUEUE_ORDER} limit 1").fetchone()
        when = publish_time_utc(sch["publish_time"]) if sch["publish"] == "at" else None
        plan = (f"make: {row['title']}" if row else f"queue empty: discover 1 idea from {sch['fallback']}"
                + (f" ({sch['fallback_arg']})" if sch["fallback_arg"] else ""))
        plan += f"; then {'upload ' + ('public at ' + when + ' UTC' if when else 'as private') if sch['upload'] else 'no upload'}"
        print(plan)
        if dry_run:
            return
        if row:
            pid = row["pageid"]
        elif sch["fallback"] == "wikipedia":
            pid = (explore(con, sch["fallback_arg"], "any", 1) if sch["fallback_arg"] else discover(con, 1))[0]
        else:
            pid = discover_source(con, sch["fallback"], 1, sch["fallback_arg"] or None)[0]
        make(con, pid)
        if sch["upload"]:
            ep = con.execute("select ep from pages where pageid=?", (pid,)).fetchone()["ep"]
            upload_pending(con, when, ep)
        print("=== done")
    except BaseException as e:
        notify(f"Automated run failed: {e}")
        print(f"=== failed: {type(e).__name__}: {e}")
        raise


def preview(con):
    """Render one frame with the saved brand settings to data/preview.png (latest episode, or a sample)."""
    row = con.execute("select pageid from pages where ep is not null and video_path is not null order by ep desc").fetchone()
    mpath = WORK / str(row["pageid"]) / "meta.json" if row else None
    m = json.loads(mpath.read_text()) if mpath and mpath.exists() else dict(
        ep=1, article_title="Emu War", description="1932 Australian wildlife management operation", images=[],
        script="In 1932, Australia went to war with emus, and the emus won.")
    m["images"] = [i for i in m["images"] if Path(i.get("path", "")).exists()][:1]
    words = [[w, k * 0.35, k * 0.35 + 0.35] for k, w in enumerate(m["script"].split()[:12])]
    render(m, words, None, 12, DB.parent / "preview.png", frame_at=0.75)
    print("preview saved: data/preview.png")


def voicetest(text=None):
    """Speak a sample with the saved voice settings to data/voice_preview.wav."""
    tts(text or "In 1932, Australia went to war with emus. " + BRAND["outro_line"], DB.parent / "voice_preview.wav")
    print("voice sample saved: data/voice_preview.wav")


def rerender(con, ep):
    """Re-voice and re-render an episode from its saved (possibly hand-edited) script. No LLM rewrite."""
    row = con.execute("select * from pages where ep=?", (ep,)).fetchone()
    if not row or not (WORK / str(row["pageid"]) / "meta.json").exists():
        raise SystemExit(f"episode {ep} has no saved script to re-render")
    if row["status"] == "uploaded":
        raise SystemExit(f"episode {ep} is already on YouTube; a re-render can't replace it there")
    m = json.loads((WORK / str(row["pageid"]) / "meta.json").read_text())
    print(f"[{ep:03d}] re-rendering {m['article_title']}")
    m.pop("potential", None)
    produce(con, m, Path(row["video_path"]) if row["video_path"] else OUT / f"{ep:03d}-rerender.mp4")
    score_video(con, row["pageid"])



YT_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
YT_MANAGE = "https://www.googleapis.com/auth/youtube.force-ssl"  # needed to delete videos


def youtube(scope):
    """Authorised YouTube client. Asks Google for consent (browser) only if the saved token lacks `scope`."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    tok = SECRETS / "token.json"
    creds = Credentials.from_authorized_user_file(tok) if tok.exists() else None
    if creds and not creds.has_scopes([scope]):
        print("  YouTube needs an extra permission; a browser window will ask you to allow it")
        creds = None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        cs = SECRETS / "client_secret.json"
        if not cs.exists():
            raise SystemExit(f"Missing {cs}; see README.md")
        had = json.loads(tok.read_text()).get("scopes", []) if tok.exists() else []
        creds = InstalledAppFlow.from_client_secrets_file(cs, sorted({*had, scope})).run_local_server(port=0)
    tok.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def delete_local(con, pageid, reason):
    """Delete an episode's video and work files; the page stays in the log as skipped."""
    import shutil
    row = con.execute("select video_path from pages where pageid=?", (pageid,)).fetchone()
    shutil.rmtree(WORK / str(int(pageid)), ignore_errors=True)
    video = Path(row["video_path"] or "") if row else Path()
    if row and row["video_path"] and video.resolve().parent == OUT.resolve():  # only files we rendered
        video.unlink(missing_ok=True)
    con.execute("update pages set status='skipped', reason=?, ep=null, video_path=null, video_id=null, potential=null, "
                "updated=? where pageid=?", (reason, time.strftime("%Y-%m-%d %H:%M:%S"), pageid))
    con.commit()


def unpublish(con, ep, keep_local=False):
    """Delete an uploaded episode from YouTube; keep it locally (back to 'rendered') or delete it everywhere."""
    from googleapiclient.errors import HttpError
    row = con.execute("select * from pages where ep=? and status='uploaded'", (ep,)).fetchone()
    if not row:
        raise SystemExit(f"episode {ep} is not uploaded")
    print(f"[{ep:03d}] deleting https://youtube.com/shorts/{row['video_id']} from YouTube…")
    try:
        youtube(YT_MANAGE).videos().delete(id=row["video_id"]).execute()
    except HttpError as e:
        if e.resp.status != 404:  # 404 = already gone from YouTube, carry on
            raise
        print("  it was already gone from YouTube")
    note = f"removed from YouTube (was {row['video_id']})"
    if keep_local and row["video_path"] and Path(row["video_path"]).exists():
        upsert(con, row["pageid"], status="rendered", video_id=None, reason=note)
        print("  deleted from YouTube; local copy kept and ready to upload again")
    else:
        delete_local(con, row["pageid"], note)
        print("  deleted from YouTube and locally")


def upload(con, pageid, publish_at=None):
    from googleapiclient.http import MediaFileUpload
    row = con.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    if not row or row["status"] != "rendered":
        raise SystemExit(f"page {pageid} is not in 'rendered' state")
    m = json.loads((WORK / str(pageid) / "meta.json").read_text())
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at
    body = {"snippet": {"title": m["title"], "description": m["yt_description"],
                        "tags": BRAND["hashtags"] + m["tags"], "categoryId": "27"}, "status": status}
    req = youtube(YT_UPLOAD).videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(row["video_path"], chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    upsert(con, pageid, status="uploaded", video_id=resp["id"])
    print(f"  uploaded: https://youtube.com/shorts/{resp['id']} ({status['privacyStatus']})")


def upload_pending(con, publish_at=None, ep=None):
    """Upload every rendered-but-not-uploaded video (or just episode `ep`), best potential first."""
    rows = con.execute("select pageid, ep, title from pages where status='rendered' and (? is null or ep=?) "
                       "order by potential is null, potential desc, ep",
                       (ep, ep)).fetchall()
    if not rows:
        print(f"episode {ep} is not ready to upload" if ep else "nothing to upload")
    for r in rows:
        print(f"[{r['ep']:03d}] uploading {r['title']}")
        try:
            upload(con, r["pageid"], publish_at)
        except Exception as e:  # keep going; it stays 'rendered' and is retried next time
            print(f"  upload failed: {e}")


def selftest():
    g = group_words([["In", 0, .1], ["1932,", .1, .5], ["Australia", .5, .9], ["lost", .9, 1], ["a", 1, 1.1],
                     ["war", 1.1, 1.3], ["to", 1.3, 1.4], ["emus.", 1.4, 1.8]])
    assert [len(x[2]) for x in g] == [2, 3, 3], g
    assert g[0][1] == g[1][0] and abs(g[-1][1] - 2.2) < 1e-9
    assert strip_html('<bdi>Unknown author<span style="display: none;">Unknown author</span></bdi>') == "Unknown author"
    assert credit({"artist": "Unknown author", "license": "Public domain"}) == "Public domain"
    assert credit({"artist": "Fairfax", "license": "Public domain"}) == "Fairfax · Public domain"
    assert credit({"artist": "", "license": ""}) == ""  # own upload: no credit line
    assert precheck({"title": "List of moths", "length": 9e9}) == "list page"
    assert precheck({"title": "X", "length": 9000, "pageviews": {"a": 100, "b": None}}).startswith("low views")
    assert precheck({"title": "X", "length": 9000, "pageviews": {"a": 500}}) is None
    assert precheck({"title": "X", "length": 9000, "pageviews": {"a": None}}) is None
    hi, _ = potential_score(200000, 80, 9, 9, 9, 6, 42)
    lo, parts = potential_score(30, 2, 3, 2, 3, 0, 75)
    assert hi >= 90 and lo <= 25 and parts["length"] == 0 and parts["visuals"] == 0, (hi, lo, parts)
    assert potential_score(0, 0, 10, 10, 10, 4, 40)[0] == 65  # perfect content, zero demand/reach
    assert JUNK_TITLE.search("List of rivers") and JUNK_TITLE.search("1990s") and JUNK_TITLE.search("2015 in Nepal")
    assert not JUNK_TITLE.search("Mount Everest") and not JUNK_TITLE.search("1918 flu pandemic")
    assert speakable("served in World War II and World War I.") == "served in World War Two and World War One."
    assert speakable("Elizabeth II met Louis XIV at Super Bowl LVII") == \
        "Elizabeth the Second met Louis the Fourteenth at Super Bowl Fifty-Seven"
    assert speakable("Malcolm X said I am") == "Malcolm X said I am" and speakable("Pope John Paul II.") == "Pope John Paul the Second."
    assert speakable("Henry VIII's wives; Apollo XI") == "Henry the Eighth's wives; Apollo Eleven"
    assert [roman(x) for x in ["II", "IV", "IX", "XIV", "XL", "XC", "LVII", "IIII", "VX", "IC"]] == [2, 4, 9, 14, 40, 90, 57, None, None, None]
    assert speakable("Mix of I and II") == "Mix of I and II"
    import sources
    sources.selftest()
    from datetime import datetime, timezone
    when = datetime.strptime(publish_time_utc("15:00"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert 2 * 3600 <= (when - datetime.now(timezone.utc)).total_seconds() <= 26 * 3600 + 60
    assert when.astimezone().strftime("%H:%M") == "15:00"
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--url"); r.add_argument("--force", action="store_true")
    r.add_argument("--upload", action="store_true"); r.add_argument("--publish-at")
    a = sub.add_parser("add"); a.add_argument("urls", nargs="+"); a.add_argument("--force", action="store_true")
    u = sub.add_parser("upload"); u.add_argument("--publish-at"); u.add_argument("--ep", type=int)
    lg = sub.add_parser("log"); lg.add_argument("n", nargs="?", type=int, default=30)
    sub.add_parser("selftest")
    sub.add_parser("preview")
    au = sub.add_parser("auto"); au.add_argument("--dry-run", action="store_true")
    sub.add_parser("schedule")
    vt = sub.add_parser("voicetest"); vt.add_argument("--text")
    up = sub.add_parser("unpublish"); up.add_argument("--ep", type=int, required=True)
    up.add_argument("--keep-local", action="store_true")
    rr = sub.add_parser("rerender"); rr.add_argument("--ep", type=int, required=True)
    dc = sub.add_parser("discover"); dc.add_argument("--count", type=int, default=5)
    dc.add_argument("--topic"); dc.add_argument("--degree", choices=["1", "2", "3", "any"], default="any")
    dc.add_argument("--source", choices=["wikipedia", "hackernews", "rss", "arxiv", "apod"], default="wikipedia")
    dc.add_argument("--feed", help="RSS/Atom feed URL (for --source rss)")
    sc = sub.add_parser("score"); sc.add_argument("--all", action="store_true")
    wb = sub.add_parser("web"); wb.add_argument("--port", type=int, default=8765); wb.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    for d in (DB.parent, WORK, OUT, SECRETS):
        d.mkdir(exist_ok=True)
    con = db()
    if args.cmd == "web":
        import web
        return web.serve(args.port, not args.no_open)
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
    elif args.cmd == "auto":
        auto(con, args.dry_run)
    elif args.cmd == "schedule":
        schedule_apply()
    elif args.cmd == "preview":
        preview(con)
    elif args.cmd == "voicetest":
        voicetest(args.text)
    elif args.cmd == "unpublish":
        unpublish(con, args.ep, args.keep_local)
    elif args.cmd == "rerender":
        rerender(con, args.ep)
    elif args.cmd == "discover" and args.source != "wikipedia":
        discover_source(con, args.source, args.count, args.feed if args.source == "rss" else args.topic)
    elif args.cmd == "discover":
        if args.topic:
            explore(con, args.topic, args.degree, args.count)
        else:
            print(f"finding {args.count} interesting random pages…")
            discover(con, args.count)
    elif args.cmd == "score":
        rows = con.execute("select pageid, ep, title from pages where ep is not null and status in ('rendered','uploaded') "
                           "and (? or potential is null) order by ep", (args.all,)).fetchall()
        for r in rows:
            if (WORK / str(r["pageid"]) / "meta.json").exists():
                print(f"[{r['ep']:03d}] {r['title']}")
                score_video(con, r["pageid"])
        if not rows:
            print("everything is scored (use --all to rescore)")
    elif args.cmd == "upload":
        upload_pending(con, args.publish_at, args.ep)
    elif args.cmd == "run":
        if args.url:
            pageid = add_curated(con, args.url, args.force, make_now=True)
            if pageid is None:
                return
        else:
            row = con.execute("select pageid from pages where status='queued' "
                              f"{QUEUE_ORDER} limit 1").fetchone()
            pageid = row["pageid"] if row else None
            if pageid is None:
                print("discovering…")
                pageid = discover(con)[0]
        make(con, pageid)
        if args.upload:
            upload_pending(con, args.publish_at)


if __name__ == "__main__":
    main()
