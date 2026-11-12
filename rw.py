"""Shorts Machine: Wikipedia pages, web articles, feeds, Hacker News, arXiv and NASA APOD -> branded YouTube Shorts.

  ./rw run ["ANYTHING"] [--upload] [--publish-at ISO]  make a video from a question, topic, paragraph or link
                                                      (nothing = the next idea, else a random Wikipedia page)
  ./rw add "ANYTHING" [...]                           save ideas for later
  ./rw discover [--count N] [--topic TOPIC|LINK --degree 1|2|3|any]   find N ideas (random, around a topic, or in a link)
  ./rw upload [--ep N] [--publish-at ISO]             upload every rendered video (or just episode N)
  ./rw auto [--dry-run]                               one scheduled run (make next video, upload)
  ./rw schedule                                       install/remove the launchd job from brand.json "schedule"
  ./rw unpublish --ep N [--keep-local]                delete an uploaded episode from YouTube (and locally)
  ./rw rerender --ep N                                re-voice/re-render from the saved (edited) script
  ./rw log [N]                                        show the page log
  ./rw web [--port 8765]                              local studio UI in the browser
  ./rw selftest
"""
import argparse, json, os, re, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for k, sub in [("HF_HOME", "hf"), ("TORCH_HOME", "torch"), ("XDG_CACHE_HOME", "xdg")]:
    os.environ.setdefault(k, str(ROOT / ".cache" / sub))  # keep every download inside the project

BRAND_FILE = ROOT / "brand.json"  # your settings (git-ignored); seeded from brand.example.json on first run
if not BRAND_FILE.exists():
    import shutil
    shutil.copy(ROOT / "brand.example.json", BRAND_FILE)
BRAND = json.loads(BRAND_FILE.read_text())
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
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:  # timeouts too: a slow LLM call is retried
            code = getattr(e, "code", 0)
            if attempt == 4 or code in (400, 403, 404):
                raise
            wait = 5 * (attempt + 1)
            if code == 429:  # rate-limited: wait as long as the server asks, else back off harder (15, 30, 45, 60 s)
                ra = e.headers.get("Retry-After", "") if getattr(e, "headers", None) else ""
                wait = min(int(ra), 120) if ra.isdigit() else 15 * (attempt + 1)
                print(f"  {urllib.parse.urlsplit(url).netloc} is rate-limiting; waiting {wait}s")
            time.sleep(wait)


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
    for col in ("potential integer", "rank integer", "via text", "origin text", "ref text", "upload_rank integer"):  # added after v1; ignore if present
        try:
            con.execute(f"alter table pages add column {col}")
        except sqlite3.OperationalError:
            pass
    return con


QUEUE_ORDER = "order by rank is null, rank, source='curated' desc, created"  # manual drag order first
UPLOAD_ORDER = "order by upload_rank is null, upload_rank, potential is null, potential desc, ep"  # your order, then best score


def upsert(con, pageid, **f):
    f["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cols = ", ".join(f)
    con.execute(f"insert into pages(pageid, {cols}) values(?, {', '.join('?' * len(f))}) "
                f"on conflict(pageid) do update set {', '.join(f'{k}=excluded.{k}' for k in f)}",
                [pageid, *f.values()])
    con.commit()


# ---------- llm ----------
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/"
GEMINI_KEY_FILE = SECRETS / "gemini_api_key"
gemini_down = None  # why Gemini failed this run; the rest of the run then uses the local model
gemini_used = {"requests": 0, "tokens": 0}  # this run, for the per-video count
USAGE_FILE = ROOT / "data" / "gemini_usage.json"  # {pacific date: {model: {requests, tokens}}}


def usage_day():
    """Google resets the free daily quota at midnight Pacific time, so days are counted in that zone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def count_gemini(model, usage):
    tokens = int(usage.get("totalTokenCount") or 0)
    gemini_used["requests"] += 1
    gemini_used["tokens"] += tokens
    try:
        log = json.loads(USAGE_FILE.read_text())
    except (OSError, ValueError):
        log = {}
    day = log.setdefault(usage_day(), {}).setdefault(model, {"requests": 0, "tokens": 0})
    day["requests"] += 1
    day["tokens"] += tokens
    USAGE_FILE.write_text(json.dumps(dict(sorted(log.items())[-30:]), indent=1))  # ponytail: last 30 days is plenty


def record_usage(pageid, before):
    """Add the Gemini requests/tokens used since `before` to the video's meta."""
    used = {k: gemini_used[k] - before[k] for k in gemini_used}
    mpath = WORK / str(pageid) / "meta.json"
    if not used["requests"] or not mpath.exists():
        return
    m = json.loads(mpath.read_text())
    g = m.get("gemini") or {"requests": 0, "tokens": 0}
    m["gemini"] = {k: g.get(k, 0) + used[k] for k in used}
    mpath.write_text(json.dumps(m, indent=2))
    print(f"  Gemini: {used['requests']} requests, {used['tokens']:,} tokens")


def gemini_key():
    """GEMINI_API_KEY from the shell, else the key saved in Settings (secrets/, git-ignored). Each user brings their own."""
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    return k or (GEMINI_KEY_FILE.read_text().strip() if GEMINI_KEY_FILE.exists() else "")


def gemini(prompt, temperature=0.7, tools=None):
    """One Gemini call -> (text, candidate). Tries the chosen model, then Flash-Lite (busy or out of quota)."""
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": temperature}}
    if tools:
        body["tools"] = tools
    else:
        body["generationConfig"]["responseMimeType"] = "application/json"
    why = ""
    models = [BRAND.get("gemini_model") or "gemini-flash-latest", "gemini-flash-lite-latest"]
    for model in models + models:  # second round: busy (503) and timeouts are usually over in seconds
        if why:
            time.sleep(3 if model != models[0] else 10)
        req = urllib.request.Request(GEMINI_API + model + ":generateContent", json.dumps(body).encode(),
                                     {"x-goog-api-key": gemini_key(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.load(r)
            count_gemini(d.get("modelVersion") or model, d.get("usageMetadata") or {})
            c = d["candidates"][0]
            return "".join(p.get("text", "") for p in c["content"]["parts"]), c
        except urllib.error.HTTPError as e:
            try:  # Google's own message only; never the request (it carries the key)
                why = f"{model}: {json.load(e)['error']['message'].split('. ')[0]}"
            except Exception:
                why = f"{model}: HTTP {e.code}"
            if e.code in (400, 401, 403):
                break  # bad key or request: another model won't help
            if e.code == 429 and model == models[-1]:
                break  # out of quota on the fallback too: waiting won't help this run
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
            why = f"{model}: {type(e).__name__}"
    raise RuntimeError(why)


def json_in(text):
    """The JSON object in a reply that may wrap it in prose or a code fence."""
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m[0]) if m else {}


def llm(prompt, temperature=0.7, local=False):
    """JSON reply from Gemini when a key is set (unless local: bulk scoring stays free), else from Ollama."""
    global gemini_down
    if not local and not gemini_down and gemini_key():
        try:
            return json_in(gemini(prompt, temperature)[0])
        except (RuntimeError, ValueError) as e:
            gemini_down = str(e)
            print(f"  Gemini unavailable ({gemini_down}); using the local model for the rest of this run")
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
    s = llm(SCORE_PROMPT.format(title=p["title"], text=p.get("extract", "")[:3000], context=context), 0.2, local=True)
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


def discover_source(con, origin, want=5, arg=None, cands=None):
    """Queue up to `want` interesting items from another source (hackernews, rss, arxiv, apod, or any link)."""
    import sources
    cands = cands if cands is not None else sources.discover(origin, want * 4, arg)
    print(f"{len(cands)} candidates from {origin}" + (f" ({arg})" if arg else ""))
    if cands and cands[0]["origin"] == "topic":  # a Wikipedia article link: explore around it
        return explore(con, cands[0]["ref"], "1", want)
    found = []
    wiki_titles = [c["ref"] for c in cands if c["origin"] == "wikipedia"]
    for i in range(0, len(wiki_titles), 20):  # Wikipedia items (category/list members, links in feeds): normal Wikipedia scoring
        if len(found) >= want:
            break
        titles = [t.split("/wiki/", 1)[-1].replace("_", " ") for t in wiki_titles[i:i + 20]]
        res = wiki(titles="|".join(urllib.parse.unquote(t) for t in titles), redirects=1, **CANDIDATE_PROPS)
        for pg in res["query"]["pages"]:
            if len(found) < want and evaluate(con, pg, "link", via=arg if origin == "link" else None):
                found.append(pg["pageid"])
    for c in cands:
        if len(found) >= want:
            break
        origin_c = c["origin"]
        if origin_c == "wikipedia":
            continue
        try:
            a = sources.fetch_article(origin_c, c["ref"])
        except (SystemExit, Exception) as e:
            print(f"   –  {c['title'][:60]}  ({e})")
            continue
        pid = sources.synthetic_id(a["key"])
        seen = con.execute("select status from pages where pageid=?", (pid,)).fetchone()
        if seen:  # never process the same item twice; say so instead of skipping silently
            print(f"   –  {a['title'][:60]}  (already in your log as {seen['status'].replace('queued', 'idea').replace('rendered', 'in queue')})")
            continue
        base = dict(title=a["title"], url=a["url"], source=origin if origin in ("rss", "link") else "random",
                    origin=a["origin"], ref=c["ref"], via=arg if origin == "link" else None)
        s = llm(SCORE_PROMPT.format(title=a["title"], text=a["text"][:3000], context=""), 0.2, local=True)
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


def explore(con, key, degree="any", want=5, max_checks=60):  # ~60 LLM checks max, so a dead-end topic stops in minutes
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
    print(f"found {len(found)} of {want} after checking {checked} pages around '{seed}'" + ("" if found else
          ". Nothing passed the interest filter: technical or niche topics rarely score high. Try a broader topic, "
          "another degree, a lower minimum score in Settings, or save specific ideas with the Make a video box."))
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


QUESTION_PROMPT = """Write the narration for a YouTube Short that answers the question (or explains the topic) below, using the research notes.
Rules:
- 100 to 120 words (about 7 sentences of 15 words). Plain spoken English for a curious 14-year-old; explain any jargon in a few words.
- Open by asking the question in a vivid way (one short sentence). For a topic, open with a curious question about it.
- For "how" questions, answer as 3 or 4 steps in the order they actually happen, using "First", "Next", "Then", "Finally".
  For "why" or "what" questions, give the cause or core idea first, then one concrete example.
- End with a satisfying one-line takeaway.
- Use ONLY facts from the research notes. Do not invent numbers, names or dates.
- Do not say "Wikipedia", "this article", "in this video", or ask viewers to like or subscribe.

Question or topic: {title}
Research notes:
{text}

Reply as JSON: {{"title": "<YouTube title, max 60 characters>", "script": "<narration>", "tags": ["<5 to 8 lowercase tags>"]}}"""

RESEARCH_PROMPT = """We are making a 50-second explainer video that answers: "{question}"
List 3 to 5 English Wikipedia article titles that together contain the answer: the main topic first, then the key
parts, steps or mechanisms involved. Use real, existing article titles.
Reply as JSON: {{"articles": ["<title>", ...]}}"""


GEMINI_RESEARCH = """Research this for a 50-second explainer video: "{question}"
{how}
Write factual research notes of 300 to 500 words for a scriptwriter: the key facts, numbers and names, and for "how"
questions the steps in the order they happen. Use only facts from the sources you read; skip anything uncertain.
Also list up to 6 concrete things worth showing on screen (objects, places, people, machines, diagrams), each as the
exact title of an existing English Wikipedia article.
Reply with only this JSON: {{"notes": "<notes>", "visuals": ["<Wikipedia title>", ...]}}"""

GEMINI_SOURCES = """We are researching a 50-second explainer video about: "{question}"
List 4 to 6 web pages that explain it reliably and in detail: encyclopedias, universities, museums, official
documentation, standards bodies, reputable science or news sites. Give exact, real URLs of specific pages
(not home pages). Include the most relevant English Wikipedia article.
Reply as JSON: {{"urls": ["<url>", ...]}}"""


def research_gemini(question):
    """Gemini researches the web: Google Search if the key's plan allows it, else it picks pages and reads them itself."""
    sources = []
    try:
        text, c = gemini(GEMINI_RESEARCH.format(question=question, how="Search the web for it."), 0.2,
                         tools=[{"google_search": {}}])
        sources = [(ch["web"].get("title", ""), ch["web"].get("uri", "")) for ch in
                   (c.get("groundingMetadata") or {}).get("groundingChunks", []) if "web" in ch]
        how = "Google Search"
    except RuntimeError as e:
        print(f"  Google Search via Gemini unavailable ({e}); Gemini will choose and read pages instead")
        urls = [u for u in (json_in(gemini(GEMINI_SOURCES.format(question=question), 0.2)[0]).get("urls") or [])
                if isinstance(u, str) and u.startswith("https://")][:6]
        if not urls:
            raise RuntimeError("Gemini suggested no pages")
        text, c = gemini(GEMINI_RESEARCH.format(question=question, how="Read these pages:\n" + "\n".join(urls)), 0.2,
                         tools=[{"url_context": {}}])
        read = [m["retrievedUrl"] for m in (c.get("urlContextMetadata") or {}).get("urlMetadata", [])
                if m.get("urlRetrievalStatus") == "URL_RETRIEVAL_STATUS_SUCCESS"]
        sources = [(urllib.parse.urlsplit(u).netloc.removeprefix("www."), u) for u in read]
        how = f"{len(read)} of {len(urls)} pages read"
    r = json_in(text)
    notes = str(r.get("notes") or "").strip()
    if len(notes.split()) < 80 or not sources:  # nothing it actually read: don't script from the model's memory
        raise RuntimeError(f"too little sourced research ({how})")
    titles = []
    for t in [str(v) for v in (r.get("visuals") or [])][:6]:  # visual subjects -> real Wikipedia articles (their lead images)
        p = wiki(titles=t, redirects=1)["query"]["pages"][0]
        if not p.get("missing") and p["title"] not in titles:
            titles.append(p["title"])
    sites = list(dict.fromkeys(name for name, _ in sources))
    print(f"  researched with Gemini ({how}): {', '.join(sites[:6])}")
    return dict(origin="question", title=question, description="",
                text=notes, url=next((u for n, u in sources if "wikipedia.org/wiki/" in u), sources[0][1]), images=[], popularity=None,
                credit="Researched with Gemini from: " + ("; ".join(sites[:8]) if how == "Google Search"  # search links are redirects
                                                          else " ".join(u for _, u in sources[:8])),
                source_label="Sources: " + ", ".join(sites[:3]), titles=titles)


def research(question):
    """Answer a question: with a Gemini key, Gemini researches the web; otherwise (or if that fails) Wikipedia,
    by picking the articles that cover it, reading them, and packaging the notes as an Article."""
    if gemini_key() and not gemini_down:
        try:
            return research_gemini(question)
        except (RuntimeError, ValueError) as e:
            print(f"  Gemini research failed ({e}); researching on Wikipedia instead")
    wanted = [str(t) for t in (llm(RESEARCH_PROMPT.format(question=question), 0.2).get("articles") or [])][:5]
    found = {}  # title -> pageid
    for q in [question] + wanted:  # resolve each suggestion to a real article via search (the question itself too)
        if len(found) == 5:
            break
        hits = wiki(list="search", srsearch=q, srlimit=1, srnamespace=0, srprop="")["query"]["search"]
        if hits and not JUNK_TITLE.search(hits[0]["title"]):
            found.setdefault(hits[0]["title"], hits[0]["pageid"])
    if not found:
        raise SystemExit(f"couldn't find Wikipedia articles about: {question}")
    titles = list(found)
    print(f"  researching: {', '.join(titles)}")
    notes = []
    simple = get_json("https://simple.wikipedia.org/w/api.php", {"action": "query", "format": "json", "formatversion": 2,
                      "titles": titles[0], "prop": "extracts", "explaintext": 1, "exintro": 1, "redirects": 1})
    plain = (simple["query"]["pages"][0].get("extract") or "").strip()
    if plain:  # Simple English Wikipedia: written for plain explanations
        notes.append(f"## {titles[0]} (in simple words)\n{plain[:1500]}")
    for t in titles:
        _, _, text, _ = fetch_article(found[t])
        notes.append(f"## {t}\n{text[:2600]}")
    urls = [page_url(t) for t in titles]
    return dict(origin="question", title=question, description="",
                text="\n\n".join(notes), url=urls[0], images=[], popularity=None,
                credit="Researched from the Wikipedia articles " + ", ".join(f'"{t}"' for t in titles)
                       + " (CC BY-SA 4.0): " + " ".join(urls),
                source_label="Sources: Wikipedia · CC BY-SA 4.0", titles=titles)

TEXT_PROMPT = """Write the narration for a YouTube Short based on the creator's text below.
Rules:
- 100 to 120 words (about 7 sentences of 15 words). Plain spoken English, short sentences, no lists, no emojis, no hashtags.
- Keep the text's main points, angle and order. The first sentence is a hook of under 14 words.
- Facts come from the creator's text; background notes may only fill in or explain. Do not invent anything.
- Do not say "Wikipedia", "this text", "in this video", or ask viewers to like or subscribe.

Title: {title}
{text}

Reply as JSON: {{"title": "<YouTube title, max 60 characters, curious but accurate>", "script": "<narration>", "tags": ["<5 to 8 lowercase tags>"]}}"""

QUESTION_WORDS = re.compile(r"^(how|why|what|when|where|who|which|is|are|can|could|do|does|did|should|would|will)\b", re.I)


def add_input(con, text, force=False, make_now=False):
    """Anything -> a queued idea. A link is made into a video (a feed, site, subreddit, category or list gets its
    best items queued instead); a paragraph is the script's source; a short query or question is researched."""
    import sources
    t = text.strip()
    if not t:
        raise SystemExit("type something: a question, a topic, a paragraph or a link")
    if "\n" not in t and sources.looks_like_link(t):
        url = t if "://" in t else "https://" + t
        origin, _ = sources.detect(url)
        listing = origin == "wikipedia" and re.search(r"/wiki/(Category:|Lists? of|Index of|Outline of)", urllib.parse.unquote(url).replace("_", " "))
        if origin == "web" or listing:
            kind, cands = sources.discover_link(url, 20)
            if kind != "single article":
                print(f"link is: {kind}, so finding the best ideas in it")
                found = discover_source(con, "link", 5, url, cands)
                return found[0] if found else None
        return add_curated(con, url, force, make_now)
    if len(t.split()) > 30 or "\n" in t:
        return add_text(con, t, force)
    return add_question(con, t, force)


def add_text(con, text, force=False):
    """Queue the creator's own text (a paragraph, notes, a draft) as an idea."""
    import sources
    pid = sources.synthetic_id("text:" + re.sub(r"\s+", " ", text).lower())
    row = con.execute("select status from pages where pageid=?", (pid,)).fetchone()
    if row and row["status"] in ("rendered", "uploaded") and not force:
        print(f"skip: already made ({row['status']}); use --force to make it again")
        return None
    words = re.split(r"(?<=[.!?])\s", text, 1)[0].split()
    title = " ".join(words[:9]).rstrip(".,;:") + ("…" if len(words) > 9 else "")
    if not row or row["status"] != "queued":
        upsert(con, pid, title=title, url="", source="curated", status="queued", reason="your text", origin="text", ref=text)
    print(f"queued your text: {title}")
    return pid


def question_id(question):
    """'how do vaccines work' -> ('How do vaccines work?', its stable id)."""
    import sources
    q = re.sub(r"\s+", " ", question).strip()
    q = q[0].upper() + q[1:]
    if QUESTION_WORDS.match(q):
        q = q.rstrip("?") + "?"
    return q, sources.synthetic_id("question:" + q.lower())


BRAINSTORM_PROMPT = """You find ideas for a YouTube Shorts channel of 50-second explainers and surprising true stories.
Topic: "{topic}"
{closeness}
Suggest {n} video ideas. Each is one specific question or subject a curious viewer would click on: concrete, surprising,
with a clear payoff. Mix "how", "why" and "what happened" ideas. Only well-documented subjects. Phrase them as open
questions or subjects, never as claims, so nothing rests on a false premise.
Already covered (suggest different ones): {seen}
Each idea must be about one real subject that has its own English Wikipedia article.
Reply as JSON: {{"ideas": [{{"idea": "<question or subject, max 70 characters>", "article": "<exact Wikipedia article title>",
"hook": "<why it's interesting, one sentence>", "score": <1-10, how likely it makes a viewer think "wait, what?">}}]}}"""

IDEA_CHECK_PROMPT = """Check video ideas against the Wikipedia text about their subject.
For each idea: if the text supports what the idea assumes, keep it as it is. If the idea assumes something false, doubtful
or not in the text, rewrite it into a true, still-intriguing question or subject based on the text, or drop it.
{items}
Reply as JSON: {{"ideas": [{{"n": <number>, "idea": "<kept or rewritten idea, max 70 characters>"}}]}} (leave out dropped ones)"""

CLOSENESS = {"1": "Stay directly on the topic: its parts, history, people, places, and how it works.",
             "2": "Go one step out: subjects closely connected to the topic, but not the topic itself.",
             "3": "Go far: surprising, distant connections that still genuinely link back to the topic.",
             "any": "Mix ideas directly about the topic with a few surprising, more distant connections."}


def brainstorm(con, topic, degree="any", want=5):
    """The local model suggests ideas around a topic. Each is queued like a typed question, so its facts come from
    research when the video is made, never from the model's memory."""
    seen = [r["title"] for r in con.execute("select title from pages where via like ? order by updated desc limit 40", (topic + "%",))]
    print(f"brainstorming ideas around '{topic}' with the local model…")
    r = llm(BRAINSTORM_PROMPT.format(topic=topic, closeness=CLOSENESS.get(degree, CLOSENESS["any"]), n=want * 2,
                                     seen="; ".join(seen) or "nothing yet"), 0.9, local=True)
    score = lambda i: int(m[0]) if (m := re.match(r"\d+", str(i.get("score", "")))) else 0
    ideas = sorted((i for i in r.get("ideas") or [] if isinstance(i, dict) and str(i.get("idea", "")).strip()
                    and str(i.get("article", "")).strip()), key=lambda i: -score(i))[:20]
    # ground each idea in its real Wikipedia article: drop invented subjects, then check the premise against the intro
    res = wiki(titles="|".join({str(i["article"]) for i in ideas}), redirects=1, prop="extracts", exintro=1,
               explaintext=1, exlimit=20)["query"] if ideas else {}
    alias = {x["from"]: x["to"] for x in res.get("normalized", []) + res.get("redirects", [])}
    intro = {p["title"]: p.get("extract", "") for p in res.get("pages", []) if not p.get("missing") and p.get("extract")}
    resolve = lambda t: alias.get(alias.get(t, t), alias.get(t, t))  # normalised, then redirected
    real = [(i, intro[resolve(i["article"])]) for i in ideas if resolve(i["article"]) in intro]
    if len(real) < len(ideas):
        print(f"  dropped {len(ideas) - len(real)} with no real Wikipedia article")
    items = "\n\n".join(f"{n}. Idea: {i['idea']}\nText: {t[:700]}" for n, (i, t) in enumerate(real, 1))
    checked = llm(IDEA_CHECK_PROMPT.format(items=items), 0.2, local=True).get("ideas") or [] if real else []
    kept = []
    for c in checked:
        n = int(c["n"]) if isinstance(c, dict) and str(c.get("n", "")).isdigit() else 0
        if 1 <= n <= len(real) and str(c.get("idea", "")).strip():
            kept.append({**real[n - 1][0], "idea": str(c["idea"])})
    if len(kept) < len(real):
        print(f"  dropped {len(real) - len(kept)} the article doesn't support")
    found = []
    for i in kept:
        q, pid = question_id(str(i["idea"])[:120])
        if len(found) >= want or con.execute("select 1 from pages where pageid=?", (pid,)).fetchone():
            continue
        upsert(con, pid, title=q, url="https://en.wikipedia.org/w/index.php?search=" + urllib.parse.quote(q), source="seed",
               status="queued", score=min(score(i), 10), reason=f"llm: {i.get('hook', '')}"[:300], origin="question", ref=q,
               via=f"{topic} (degree {degree})")
        print(f"  {score(i):>2}/10  {q}  -> queued")
        found.append(pid)
    print(f"found {len(found)} of {want}" + ("" if found else "; try again, or another degree"))
    return found


def add_question(con, question, force=False):
    """Queue a question as an idea; it is researched when the video is made."""
    q, pid = question_id(question)
    row = con.execute("select status from pages where pageid=?", (pid,)).fetchone()
    if row and row["status"] in ("rendered", "uploaded") and not force:
        print(f"skip: already made ({row['status']}); use --force to make it again")
        return None
    if not row or row["status"] != "queued":
        upsert(con, pid, title=q, url="https://en.wikipedia.org/w/index.php?search=" + urllib.parse.quote(q),
               source="curated", status="queued", reason="question", origin="question", ref=q)
    print(f"queued: {q} (researched when it's made)")
    return pid


def write_script(title, text, prompt=None, check=True):
    s = {}
    for _ in range(6):
        n = len(str(s.get("script", "")).split())
        if n < 40:  # nothing usable yet: fresh draft
            s = llm((prompt or SCRIPT_PROMPT).format(title=title, text=text[:12000]))
        else:  # revise the draft we have; far more reliable than redrafting blind
            action = ("Cut the least important sentences and tighten the rest." if n > 128 else
                      "Add one or two more facts from the article." if n < 85 else "Keep the length.")
            r = llm(REVISE_PROMPT.format(n=n, action=action, text=text[:12000], script=s["script"],
                                         tags=json.dumps(s.get("tags", []))))
            s = {**s, **{k: v for k, v in r.items() if v}}
        n = len(str(s.get("script", "")).split())
        if 85 <= n <= 128 and 0 < len(str(s.get("title", ""))) <= 70:
            s["tags"] = [str(t) for t in s.get("tags", [])][:8]
            c = llm(CHECK_PROMPT.format(text=text[:12000], script=s["script"]), 0.1) if check else {}
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


def commons_files(pages, limit, entity=False):
    """imageinfo pages -> usable, free-licensed image dicts. entity=True: the lead image of a Wikipedia article about
    the thing itself, so logos, flags and SVG diagrams are allowed (SVGs come as PNG renders) and shown whole."""
    got = []
    for f in pages:
        ii = (f.get("imageinfo") or [{}])[0]
        meta = {k: v.get("value") for k, v in ii.get("extmetadata", {}).items()}
        svg = ii.get("mime") == "image/svg+xml"
        if ((not entity and BAD_FILE.search(f["title"])) or ii.get("mime") not in ("image/jpeg", "image/png", "image/webp") + (("image/svg+xml",) if entity else ())
                or (not svg and (ii.get("width", 0) < (300 if entity else 600) or ii.get("height", 0) < (200 if entity else 450)))
                or meta.get("NonFree") or not OK_LICENSE.match(meta.get("LicenseShortName") or "")):
            continue
        got.append(dict(source="wikipedia" if entity else "commons", url=ii.get("thumburl") or ii["url"], thumb=ii.get("thumburl") or ii["url"],
                        page=ii.get("descriptionurl"), name=f["title"][5:],
                        artist=strip_html(meta.get("Artist")) or "unknown", license=meta.get("LicenseShortName"))
                   | ({"fit": "contain"} if svg or re.search(r"logo|wordmark|icon|diagram|map|chart|screenshot|screen shot|interface|homepage|\.png$", f["title"], re.I) else {}))
        if len(got) >= limit:
            break
    return got


# Wikimedia only serves standard thumbnail widths (20…500, 960, 1280, 1920, 3840); others are refused (HTTP 429)
IMAGE_PROPS = dict(prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=1280)


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


VIDEO_MIME = ("video/webm", "video/ogg", "application/ogg", "video/mp4")


def src_commons_video(q, limit=12):
    """Free-licensed video clips on Wikimedia Commons (3 s to 10 min, under 150 MB)."""
    r = get_json(COMMONS_API, {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
                               "gsrsearch": f"{q} filetype:video", "gsrnamespace": 6, "gsrlimit": 30,
                               "prop": "videoinfo", "viprop": "url|size|mime|extmetadata|derivatives", "viurlwidth": 500})
    out = []
    for f in sorted(r.get("query", {}).get("pages", []), key=lambda f: f.get("index", 0)):
        ii = (f.get("videoinfo") or [{}])[0]
        meta = {k: v.get("value") for k, v in ii.get("extmetadata", {}).items()}
        # Wikimedia asks for its transcoded versions, not original files: prefer the small 480p/360p ones
        small = {d.get("transcodekey"): d["src"] for d in ii.get("derivatives", []) if d.get("transcodekey")}
        url = next((small[k] for k in ("480p.vp9.webm", "360p.mpeg4.mov", "360p.vp9.webm", "240p.vp9.webm") if k in small), None)
        if (not url or ii.get("mime") not in VIDEO_MIME or not 3 <= (ii.get("duration") or 0) <= 600 or (ii.get("size") or 0) > 150e6
                or meta.get("NonFree") or not OK_LICENSE.match(meta.get("LicenseShortName") or "")):
            continue
        out.append(dict(source="commons", kind="video", url=url, thumb=ii.get("thumburl") or "", page=ii.get("descriptionurl"),
                        name=f["title"][5:], artist=strip_html(meta.get("Artist")) or "unknown", license=meta.get("LicenseShortName")))
        if len(out) >= limit:
            break
    return out


def src_nasa_video(q, limit=8):
    """NASA video clips (public domain), smallest MP4 rendition."""
    items = get_json("https://images-api.nasa.gov/search", {"q": q, "media_type": "video"})["collection"]["items"]
    out = []
    for it in items[:limit * 2]:
        d = it["data"][0]
        try:
            assets = get_json(urllib.parse.quote(it["href"], safe=":/"))
        except Exception:
            continue
        mp4 = next((a for tag in ("~medium.mp4", "~small.mp4", "~mobile.mp4") for a in assets if a.endswith(tag)), None)
        if not mp4:
            continue
        thumb = next((l["href"] for l in it.get("links", []) if l.get("render") == "image"), "")
        out.append(dict(source="nasa", kind="video", url=urllib.parse.quote(mp4, safe=":/"), thumb=thumb,
                        page=f"https://images.nasa.gov/details/{urllib.parse.quote(d['nasa_id'])}", name=d.get("title", "NASA video"),
                        artist=d.get("center") or "NASA", license="Public domain (NASA)"))
        if len(out) >= limit:
            break
    return out


IMAGE_SOURCES = {"article": src_article, "commons": src_commons, "openverse": src_openverse, "nasa": src_nasa, "met": src_met,
                 "commons_video": src_commons_video, "nasa_video": src_nasa_video}


def download_image(img, dest):
    """Download one candidate into the episode folder; returns it with 'path' set, or None."""
    from PIL import Image
    import hashlib, io
    if img.get("kind") == "video":
        return download_clip(img, dest)
    stem = dest / f"img_{hashlib.sha1(img['url'].encode()).hexdigest()[:10]}"
    path = next((p for p in (stem.with_suffix(".png"), stem.with_suffix(".jpg")) if p.exists()), None)
    if not path:
        time.sleep(0.5)  # Wikimedia rate-limits bursts
        data = fetch(img["url"])
        im = Image.open(io.BytesIO(data))  # also validates that it really is an image
        if im.mode in ("RGBA", "LA") or "transparency" in im.info:  # logos: keep transparency (JPEG would turn it black)
            path = stem.with_suffix(".png")
            im.convert("RGBA").save(path, "PNG")
        else:
            path = stem.with_suffix(".jpg")
            path.write_bytes(data) if im.format == "JPEG" else im.convert("RGB").save(path, "JPEG", quality=92)
    return {k: v for k, v in img.items() if k not in ("thumb", "tags")} | {"path": str(path)}


VISUAL_PROMPT = """You pick images for a YouTube Short. For each numbered sentence of the narration, give ONE short image
search query (1-5 words) naming the most concrete, picturable thing it mentions: a named person, place, building,
organisation, company, product, software, logo, artwork, animal, object or event. Use proper nouns exactly as written.
If a sentence has nothing concrete, repeat the main subject.

Main subject: {title}
Sentences:
{sentences}

Reply as JSON: {{"queries": ["<query for sentence 1>", "<query for sentence 2>", ...]}}"""


MONTHS = r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(tember)?|oct(ober)?|nov(ember)?|dec(ember)?"


def visual_query(q, subject):
    """Dates and numbers aren't pictures: 'August 2026' or '70 percent' -> the main subject."""
    rest = re.sub(rf"\b({MONTHS})\b|\b\d[\d,.%]*\b|\b(percent|century|year|years|day|days|today|yesterday)\b", "", q, flags=re.I)
    return q if len(re.sub(r"\W", "", rest)) >= 3 else subject


PICK_PROMPT = """A YouTube Short says: "{sentence}"
It needs an image of: {query}
Candidate images (title or file name, then source):
{candidates}

Which ONE clearly shows {query} in the sense this sentence means? Reject candidates that only share a word with a
different meaning (a uniform patch is not a software patch) or that are about something else.
Reply as JSON: {{"pick": <candidate number, or 0 if none fit>}}"""


def split_sentences(script):
    return [x for x in re.split(r"(?<=[.!?])\s+", script.strip()) if x]


def src_entity(q, limit=2, every=False):
    """Lead images of the Wikipedia articles a search for q finds ('White House' -> the White House photo)."""
    r = wiki(generator="search", gsrsearch=q, gsrlimit=3, prop="pageimages", piprop="name")
    # only articles that are actually about the query ('Internet Archive' -> 'Internet Archive', not 'Website')
    names = [p["pageimage"] for p in sorted(r.get("query", {}).get("pages", []), key=lambda p: p.get("index", 0))
             if p.get("pageimage") and mentions(q, {"name": p["title"]}, every)]
    if not names:
        return []
    info = wiki(titles="|".join("File:" + n for n in names), **IMAGE_PROPS).get("query", {}).get("pages", [])
    order = {"File:" + n.replace("_", " "): k for k, n in enumerate(names)}
    return commons_files(sorted(info, key=lambda f: order.get(f["title"], 99)), limit, entity=True)


def mentions(query, c, every=False):
    """Does a search result mention the query (title or tags)? every=True: all of its words, not just half."""
    stem = lambda w: re.sub(r"(es|s)$", "", w) if len(w) > 4 else w  # 'crawlers' matches 'crawler'
    words = {stem(w.lower()) for w in re.findall(r"[A-Za-z0-9]{3,}", query)} - {"the", "and", "for", "with"}
    hay = (c.get("name", "") + " " + c.get("tags", "")).lower()
    hits = sum(w in hay for w in words)
    return bool(words) and (hits == len(words) if every else hits >= max(1, len(words) // 2))


def pick_visuals(title, script, dest, own=None, pageimage=None, limit=6, topics=()):
    """One image per sentence where possible, for the concrete thing that sentence mentions, from Wikipedia entity
    images, then Commons, then Openverse. Each image remembers its sentence so it appears when that is spoken."""
    sents = split_sentences(script)
    try:
        qs = [str(q).strip() for q in llm(VISUAL_PROMPT.format(title=title, sentences="\n".join(
            f"{k + 1}. {x}" for k, x in enumerate(sents))), 0.2).get("queries", [])]
    except Exception:
        qs = []
    qs = [visual_query(q, title) for q in (qs + [title] * len(sents))[:len(sents)]]
    chosen, seen_q, seen_img = [], set(), set()
    for k, q in enumerate(qs):
        if len(chosen) >= limit or not q or q.lower() in seen_q:
            continue  # the same thing again: keep the current image on screen
        seen_q.add(q.lower())
        named = bool(re.search(r"[A-Z]", q))
        try:  # 1. the lead image of a Wikipedia article about exactly this thing (vague phrases: every word must match)
            entity = [c for c in src_entity(q, every=not named) if c["url"] not in seen_img]
        except Exception:
            entity = []
        if entity:
            c = entity[0]
            seen_img.add(c["url"])
            chosen.append(c | {"sentence": k, "query": q})
            print(f"  sentence {k + 1}: {q!r} -> {c['name'][:60]} [{c['source']}]")
            continue
        if not named:  # 3. generic phrase: photo search would match words, not meaning
            print(f"  sentence {k + 1}: {q!r} -> keeping the previous image")
            continue
        cands = []  # 2. a named thing: photo libraries, all words required, then a meaning check
        for search in (lambda: [c for c in src_commons(q, 12) if mentions(q, c, every=True)][:4],
                       lambda: [c for c in src_commons_video(q, 12) if mentions(q, c, every=True)][:2],
                       lambda: [c for c in src_openverse(q, 12) if mentions(q, c, every=True)][:3]):
            try:
                cands += [c for c in search() if c["url"] not in seen_img]
            except Exception:
                pass
        if not cands:
            continue
        try:  # the LLM judges meaning, not just matching words
            pick = int(llm(PICK_PROMPT.format(sentence=sents[k], query=q, candidates="\n".join(
                f"{n + 1}. {re.sub(r'[_]', ' ', c['name'])[:90]} ({'video, ' if c.get('kind') == 'video' else ''}{c['source']})"
                for n, c in enumerate(cands[:10]))), 0.1).get("pick", 0) or 0)
        except Exception:
            pick = 0
        if not 1 <= pick <= min(10, len(cands)):
            print(f"  sentence {k + 1}: {q!r} -> nothing fitting")
            continue
        c = cands[pick - 1]
        seen_img.add(c["url"])
        chosen.append(c | {"sentence": k, "query": q})
        print(f"  sentence {k + 1}: {q!r} -> {c['name'][:60]} [{c['source']}]")
    for t in topics:  # explainers: lead images of the researched articles (usually diagrams) fill gaps in order
        if len(chosen) >= min(limit, len(sents)):
            break
        free = sorted(set(range(len(sents))) - {c["sentence"] for c in chosen})
        try:
            c = next((c for c in src_entity(t, every=True) if c["url"] not in seen_img), None)
        except Exception:
            c = None
        if c and free:  # place it on the sentence that talks about this topic, else the first empty one
            k = next((k for k in free if mentions(t, {"name": sents[k]})), free[0])
            seen_img.add(c["url"])
            chosen.append(c | {"sentence": k, "query": t})
            print(f"  sentence {k + 1}: researched topic {t!r} -> {c['name'][:60]} [{c['source']}]")
    chosen.sort(key=lambda c: c["sentence"])
    if len(chosen) < 2:  # too little found for the script: fall back to the article's own images
        print("  few matches for the script; using the article's images")
        return fetch_images(title, pageimage, dest, own=own)
    chosen[0]["sentence"] = 0  # something on screen from the first second
    out = []
    for c in chosen:
        try:
            out.append(download_image(c, dest))
        except Exception as e:
            print(f"  image failed: {c['name']} ({e})")
    return out


def download_clip(clip, dest, seconds=12):
    """Fetch only the part of a video we use (ffmpeg seeks over HTTP), skip any opening slate, drop the audio,
    and save a small H.264 MP4 once."""
    import hashlib, subprocess, imageio_ffmpeg
    path = dest / f"clip_{hashlib.sha1(clip['url'].encode()).hexdigest()[:10]}.mp4"
    ok = lambda: path.exists() and path.stat().st_size > 20_000

    def convert(src, input_params=()):
        dur = next(imageio_ffmpeg.read_frames(src, input_params=list(input_params))).get("duration") or 0
        skip = min(dur * 0.25, 120) if dur > 20 else 0  # long clips usually open with a title slate
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *input_params, "-ss", f"{skip:.1f}", "-i", src,
                        "-t", str(seconds), "-an", "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                        "-pix_fmt", "yuv420p", str(path)], timeout=600)

    if not ok():
        try:  # MP4 can be read straight from the server, fetching only the part we use
            convert(clip["url"], ["-user_agent", BRAND["user_agent"]])
        except Exception:
            pass
    if not ok():  # WebM/Ogg often can't seek over HTTP: download the file (Commons clips are capped at 150 MB)
        raw = dest / (path.stem + ".src")
        try:
            raw.write_bytes(fetch(clip["url"]))
            convert(str(raw))
        finally:
            raw.unlink(missing_ok=True)
    if not ok():
        path.unlink(missing_ok=True)
        raise RuntimeError(f"couldn't convert the clip {clip.get('name', '')}")
    return {k: v for k, v in clip.items() if k != "tags"} | {"path": str(path)}  # thumb kept: poster in the editor


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


NUM = r"\d+(?:\.\d+)*"
SPEAK = re.compile(rf"(?P<num>(?<![\w.]){NUM}(?:\s*[–—]\s*{NUM}|-{NUM})?)(?![\w])|(?P<rom>\b(?P<w>[A-Za-z]+) (?P<r>[IVXLC]{{1,7}})\b(?![\w-]))")


def say_number(expr):
    """'5.4–8.5' -> '5 point 4 to 8 point 5'; '5.4.8' -> '5 point 4 point 8'; '3.14' -> '3 point 1 4'."""
    def one(x):
        parts = x.split(".")
        if len(parts) == 2:  # decimal: digits after the point are read one by one
            return f"{parts[0]} point {' '.join(parts[1])}"
        return " point ".join(parts)  # plain number, or a version like 5.4.8
    return " to ".join(one(x) for x in re.split(r"\s*[–—]\s*|-", expr))


def speakable_map(text):
    """Text the voice reads correctly, plus [(written, spoken)] pairs in order, so captions can show what was written.
    'World War II' -> 'World War Two', 'Louis XIV' -> 'Louis the Fourteenth', 'PHP 5.4–8.5' -> 'PHP 5 point 4 to 8 point 5'."""
    pairs = []

    def fix(m):
        if m["num"]:
            if "." not in m["num"] and not re.search("[–—-]", m["num"]):
                return m[0]  # plain numbers are read fine
            spoken = say_number(m["num"])
        else:
            word, num = m["w"], m["r"]
            n = roman(num)
            cardinal = word.lower() in CARDINAL_AFTER
            regnal = word[0].isupper() and not cardinal  # names are capitalised: "Charles III", not "and II"
            if not n or not (cardinal or regnal) or (not cardinal and num in ("I", "X", "C", "L")):  # "Malcolm X"
                return m[0]
            spoken = f"{word} {number_words(n).title()}" if cardinal else f"{word} the {number_words(n, True).title()}"
        pairs.append((m[0], spoken))
        return spoken
    return SPEAK.sub(fix, text), pairs


def speakable(text):
    return speakable_map(text)[0]


def restore_written(words, pairs):
    """Swap spoken caption words back to the written form: [5][point][4][to][8][point][5] -> [5.4–8.5], same timing."""
    norm = lambda w: re.sub(r"\W", "", w.lower())
    out, i = [], 0
    for written, spoken in pairs:
        target = [norm(t) for t in spoken.split()]
        for j in range(i, len(words) - len(target) + 1):
            if [norm(w[0]) for w in words[j:j + len(target)]] == target:
                out += words[i:j]
                span = words[j:j + len(target)]
                tail = re.sub(r"^.*?(\W*)$", r"\1", span[-1][0])  # keep trailing punctuation for caption breaks
                parts = written.split()
                if len(parts) == len(span):  # 'World War II' over 'World War Two': word for word
                    out += [[pw + (tail if k == len(span) - 1 else ""), w[1], w[2]] for k, (pw, w) in enumerate(zip(parts, span))]
                else:  # one written token over several spoken words
                    out.append([written + tail, span[0][1], span[-1][2]])
                i = j + len(target)
                break
    return out + words[i:]


# ---------- narration engines: Kokoro preset voices, your cloned voice, or your own recording ----------
_whisper = None


def transcribe_words(wav):
    """Word timings in any narration audio (local faster-whisper, model kept in .cache/whisper)."""
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel("small.en", device="cpu", compute_type="int8", download_root=str(ROOT / ".cache" / "whisper"))
    import numpy as np, soundfile as sf
    audio, sr = sf.read(str(wav), dtype="float32")  # our narration is always WAV: skip PyAV, feed 16 kHz mono samples
    audio = audio.mean(axis=1) if audio.ndim > 1 else audio
    if sr != 16000:
        audio = np.interp(np.arange(0, len(audio), sr / 16000), np.arange(len(audio)), audio).astype("float32")
    segments, _ = _whisper.transcribe(audio, language="en", word_timestamps=True, vad_filter=False)
    return [(w.word.strip(), float(w.start), float(w.end)) for seg in segments for w in seg.words if w.word.strip()]


def align_words(text, heard, duration=None):
    """Script words (as written, for captions) timed by what was actually heard. Words the recogniser got differently
    (names, numbers) are spread evenly between the matched words around them."""
    import difflib
    norm = lambda w: re.sub(r"\W", "", w.lower())
    written = text.split()
    if not heard:
        step = (duration or len(written) * 0.4) / max(1, len(written))
        return [[w, k * step, (k + 1) * step] for k, w in enumerate(written)]
    times = [None] * len(written)
    sm = difflib.SequenceMatcher(None, [norm(w) for w in written], [norm(h[0]) for h in heard], autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            times[a + k] = (heard[b + k][1], heard[b + k][2])
    end = duration or heard[-1][2]
    k = 0
    while k < len(written):  # fill gaps between anchors evenly
        if times[k] is not None:
            k += 1
            continue
        j = k
        while j < len(written) and times[j] is None:
            j += 1
        t0 = times[k - 1][1] if k else 0.0
        t1 = times[j][0] if j < len(written) else max(end, t0 + 0.3 * (j - k))
        step = (t1 - t0) / (j - k)
        for n in range(k, j):
            times[n] = (t0 + (n - k) * step, t0 + (n - k + 1) * step)
        k = j
    return [[w, a, b] for w, (a, b) in zip(written, times)]


def tts(text, dest):
    """Narrate with the engine chosen in Settings. Returns (caption words as written, duration)."""
    return tts_clone(text, dest) if BRAND.get("voice_engine") == "clone" else tts_kokoro(text, dest)


def tts_clone(text, dest):
    """Your cloned voice (Chatterbox, in .venv-clone), then timings from the audio itself."""
    import subprocess, numpy as np, soundfile as sf
    sample = ROOT / BRAND.get("voice_sample", "brand/voice_sample.wav")
    if not sample.exists() or not BRAND.get("clone_consent"):
        raise SystemExit("cloned voice needs a voice sample and your confirmation in Settings → Voice")
    spoken, _ = speakable_map(text)
    print("  cloned voice (this takes a while)…")
    r = subprocess.run([str(ROOT / ".venv-clone" / "bin" / "python"), "-W", "ignore", str(ROOT / "clone_tts.py"),
                        "--text", spoken, "--sample", str(sample), "--out", str(dest)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("voice cloning failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    audio, sr = sf.read(str(dest))
    dur = len(audio) / sr + 0.5
    if dur > BRAND["max_seconds"]:  # too long: speed up slightly, pitch preserved
        import imageio_ffmpeg
        tempo = min(1.25, dur / (BRAND["max_seconds"] - 1))
        tmp = Path(str(dest) + ".fast.wav")
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(dest), "-filter:a",
                        f"atempo={tempo:.3f}", str(tmp)], check=True)
        tmp.replace(dest)
        audio, sr = sf.read(str(dest))
        print(f"  sped up ×{tempo:.2f} to fit")
    sf.write(str(dest), np.concatenate([audio, np.zeros(int(sr * 0.5), dtype=audio.dtype)]), sr)
    dur = len(audio) / sr + 0.5
    return align_words(text, transcribe_words(dest), dur), dur


def tts_kokoro(text, dest):
    """Kokoro narration -> wav + per-word timings [(word, start, end)]; captions show the text as written."""
    text, written = speakable_map(text)
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
    words = restore_written(words, written)
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
def image_starts(cues, words, duration, min_gap=1.5):
    """When each image appears: the moment its sentence starts being spoken. None if images have no sentence cues.
    Entries become None (image dropped) when an image would show for less than min_gap seconds."""
    if not cues or any(c is None for c in cues):
        return None
    sent_start, k = {0: 0.0}, 0
    for j, w in enumerate(words):
        if j and re.search(r"[.!?][\"')\]]*$", words[j - 1][0]):
            k += 1
            sent_start[k] = w[1]
    out, last = [], -min_gap
    for i, c in enumerate(cues):
        t = 0.0 if i == 0 else sent_start.get(c)
        if t is None or t - last < min_gap or t > duration - min_gap:
            out.append(None)
            continue
        out.append(t)
        last = t
    return out


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
    # title (<=3 lines) + description (<=2 lines) scaled together to fit the header block
    desc = "" if meta.get("origin") == "question" else meta.get("description") or ""  # older explainers stored a source count here
    room, tw = PY - 24 - 190, W - 120
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

    mask = Image.new("L", (PW, PH), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, PW, PH), 36, fill=255)

    # panel sources: article images, else a branded text card
    class Clip:  # a video in the panel: frames streamed from ffmpeg, looped if the sentence outlasts the clip
        def __init__(self, path):
            self.path, self.gen = path, None
            self.first = self.next()

        def next(self):
            for _ in range(2):
                if self.gen is None:
                    self.gen = imageio_ffmpeg.read_frames(self.path, output_params=[
                        "-vf", f"scale={PW}:{PH}:force_original_aspect_ratio=increase,crop={PW}:{PH}", "-r", str(FPS)])
                    next(self.gen)  # metadata
                try:
                    return Image.frombytes("RGB", (PW, PH), next(self.gen))
                except StopIteration:
                    self.gen = None
            raise RuntimeError(f"no frames in {self.path}")

    srcs, cues, still = [], [], set()  # still: whole-image cards (logos, diagrams) that must not be zoomed into
    for img in meta["images"]:
        try:
            if img.get("kind") == "video":
                srcs.append((Clip(img["path"]), None))  # credits go in the YouTube description, not on screen
                cues.append(img.get("sentence"))
                continue
            im = Image.open(img["path"])
            if img.get("fit") == "contain":  # logo/diagram: whole, centred on a light card, never cropped
                card = Image.new("RGB", (int(PW * 1.15), int(PH * 1.15)), "#F4F5F7")
                im = im.convert("RGBA")
                scale = 0.62 if re.search(r"logo|wordmark|icon|symbol", img.get("name", ""), re.I) else 0.92  # screenshots fill the card
                im.thumbnail((int(card.width * scale), int(card.height * scale)), Image.LANCZOS)
                card.paste(im, ((card.width - im.width) // 2, (card.height - im.height) // 2), im)
                still.add(len(srcs))
                srcs.append((card, None))
                cues.append(img.get("sentence"))
                continue
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, C["panel"])
                flat.paste(im, mask=im.split()[-1])
                im = flat
            im = im.convert("RGB")
            s = max(PW * 1.15 / im.width, PH * 1.15 / im.height)
            srcs.append((im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS), None))
            cues.append(img.get("sentence"))
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
        cues.append(None)
        # ponytail: one text card when no free images exist; add AI image gen (mflux) if this happens often

    def kb(i, p, start=False):  # Ken Burns crop of source i at progress p in [0,1]; clips play instead
        if isinstance(srcs[i][0], Clip):
            return srcs[i][0].first if start else srcs[i][0].next()
        im = srcs[i][0]
        z = 1 + (0.04 if i in still else 0.12) * (p if i % 2 == 0 else 1 - p)
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

    starts = image_starts(cues, words, duration)
    if starts is None:  # no sentence cues (older videos, hand-picked mixes): spread evenly
        starts = [k * duration / len(srcs) for k in range(len(srcs))]
    else:  # images whose sentence comes too soon after the previous image are dropped
        keep = [k for k in range(len(srcs)) if k < len(starts) and starts[k] is not None]
        srcs, starts = [srcs[k] for k in keep], [starts[k] for k in keep]
    n, fade = len(srcs), 0.4
    ends = starts[1:] + [duration]

    def frame(t):
        i = max(k for k in range(n) if starts[k] <= t)
        panel = kb(i, (t - starts[i]) / max(0.1, ends[i] - starts[i]))
        if i < n - 1 and t > ends[i] - fade:
            panel = Image.blend(panel, kb(i + 1, 0, start=True), 1 - (ends[i] - t) / fade)
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
             *([f"Source: {m['url']}", ""] if m.get("url") else []),
             f"{BRAND['channel']}: {BRAND['tagline']}", "", "CREDITS",
             m.get("credit") or f"Text adapted from the Wikipedia article \"{m['article_title']}\", CC BY-SA 4.0: {m['url']}"]
    lines += [f"Image: \"{i['name']}\", {credit(i).replace(' · ', ', ')}: {i['page']}" for i in m["images"] if credit(i)]
    narration = {"recording": f"Narration: {BRAND['channel']}.", "clone": "Narration: AI voice clone of the creator (Chatterbox)."}
    lines += [narration.get(m.get("narration"), "Narration: AI voice (Kokoro TTS).") + " Fonts: SIL OFL.", "",
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
    before = dict(gemini_used)
    row = con.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    work = WORK / str(pageid)
    work.mkdir(parents=True, exist_ok=True)
    ep = row["ep"] or (con.execute("select coalesce(max(ep), 0) + 1 from pages").fetchone()[0])
    upsert(con, pageid, ep=ep)
    try:
        origin = row["origin"] or "wikipedia"
        prompt = None
        if origin == "wikipedia":
            title, desc, text, pageimage = fetch_article(pageid)
            src = dict(url=page_url(title), own=None)
        elif origin == "question":
            a = research(row["ref"])
            title, desc, text, pageimage, prompt = a["title"], a["description"], a["text"], None, QUESTION_PROMPT
            src = dict(url=a["url"], own=None, credit=a["credit"], source_label=a["source_label"], popularity=None,
                       topics=a["titles"])
        elif origin == "text":
            text, title, desc, pageimage, prompt = row["ref"], row["title"], "", None, TEXT_PROMPT
            src = dict(url="", own=None, credit=f"Written by {BRAND['channel']}.", source_label=f"Written by {BRAND['channel']}",
                       popularity=None)
            if len(text.split()) < 150:  # a short text: add background notes so the narration has enough to say
                try:
                    a = research(text[:300])
                    text = f"Creator's text:\n{text}\n\nBackground notes:\n{a['text']}"
                    src.update(url=a["url"], credit=f"Written by {BRAND['channel']}. Background: {a['credit']}",
                               source_label=f"Written by {BRAND['channel']} · " + a["source_label"].removeprefix("Sources: "))
                    # no topic images: research matches the subject loosely, and a personal story shouldn't show a stranger's tower
                except SystemExit as e:
                    print(f"  no background found ({e}); using your text only")
        else:
            import sources
            a = sources.fetch_article(origin, row["ref"])
            title, desc, text, pageimage = a["title"], a["description"], a["text"], None
            src = dict(url=a["url"], own=a["images"], credit=a["credit"], source_label=a["source_label"],
                       popularity=a.get("popularity"))
        print(f"[{ep:03d}] {title}" + ("" if origin == "wikipedia" else f"  [{origin}]") + "\n  writing script…")
        s = write_script(title, text, prompt, check=origin != "text")  # your own text is yours to vouch for
        print(f"  \"{s['title']}\"\n  fetching images…")
        images = pick_visuals(title, s["script"], work, own=src.pop("own"), pageimage=pageimage, topics=src.get("topics", ()))
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
    record_usage(pageid, before)
    return out


def produce(con, m, out):
    """Voice + render a video from its meta (script, title, images) and save everything."""
    import soundfile as sf
    work = WORK / str(m["pageid"])
    text = m["script"].strip() + " " + BRAND["outro_line"]
    recording = work / "voice_recorded.wav"
    if m.get("narration") == "recording" and recording.exists():  # your own voice, recorded in the studio
        print("  using your recorded narration; timing captions to it…")
        dur = sf.info(str(recording)).duration + 0.3
        words, voice = align_words(text, transcribe_words(recording), dur), recording
    else:
        m["narration"] = "clone" if BRAND.get("voice_engine") == "clone" else "kokoro"
        print("  voicing…")
        words, dur = tts(text, work / "voice.wav")
        voice = work / "voice.wav"
    m["yt_description"] = description(m)
    (work / "meta.json").write_text(json.dumps(m, indent=2))
    print(f"  rendering {dur:.1f}s…")
    render(m, words, voice, dur, out)
    upsert(con, m["pageid"], status="rendered", video_path=str(out))
    print(f"  done: {out}")


# ---------- automation (macOS launchd) ----------
SCHEDULE_DEFAULTS = {"enabled": False, "times": ["09:00"], "days": [0, 1, 2, 3, 4, 5, 6], "upload": True,
                     "publish": "private", "publish_time": "15:00", "fallback": "wikipedia", "fallback_arg": "",
                     "pick": "order"}  # pick: "order" = top of the Queue (ready videos), "newest" = the video this run made
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
    sys.stdout.reconfigure(line_buffering=True)  # launchd writes to data/auto.log; show progress as it happens
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
        ready = con.execute(f"select ep, title from pages where status='rendered' {UPLOAD_ORDER} limit 1").fetchone()
        which = ("the video it makes" if sch["pick"] == "newest" or not ready
                 else f"EP {ready['ep']:03d} {ready['title']} (top of the Queue)")
        plan += f"; then {'upload ' + which + (', public at ' + when + ' UTC' if when else ', private') if sch['upload'] else 'no upload'}"
        print(plan)
        if dry_run:
            return
        if row:
            pid = row["pageid"]
        elif sch["fallback"] == "wikipedia":
            pid = (brainstorm(con, sch["fallback_arg"], "any", 1) if sch["fallback_arg"] else discover(con, 1))[0]
        else:
            pid = discover_source(con, sch["fallback"], 1, sch["fallback_arg"] or None)[0]
        make(con, pid)
        if sch["upload"]:
            pick = (f"select ep from pages where status='rendered' {UPLOAD_ORDER} limit 1" if sch["pick"] == "order"
                    else f"select ep from pages where pageid={int(pid)}")
            upload_pending(con, when, con.execute(pick).fetchone()["ep"])
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


def rerender(con, ep, repick_images=False, narration=None):
    """Re-voice and re-render an episode from its saved (possibly hand-edited) script. No LLM rewrite."""
    row = con.execute("select * from pages where ep=?", (ep,)).fetchone()
    if not row or not (WORK / str(row["pageid"]) / "meta.json").exists():
        raise SystemExit(f"episode {ep} has no saved script to re-render")
    if row["status"] == "uploaded":
        raise SystemExit(f"episode {ep} is already on YouTube; a re-render can't replace it there")
    m = json.loads((WORK / str(row["pageid"]) / "meta.json").read_text())
    print(f"[{ep:03d}] re-rendering {m['article_title']}")
    m.pop("potential", None)
    if narration == "recording":
        if not (WORK / str(row["pageid"]) / "voice_recorded.wav").exists():
            raise SystemExit("no recording for this episode yet: record one in the studio first")
        m["narration"] = "recording"
    elif narration == "ai":
        m["narration"] = "kokoro"
    before = dict(gemini_used)
    if repick_images:
        print("  picking images from the script…")
        m["images"] = pick_visuals(m["article_title"], m["script"], WORK / str(row["pageid"]), own=[],
                                   topics=m.get("topics", ())) or m["images"]
    produce(con, m, Path(row["video_path"]) if row["video_path"] else OUT / f"{ep:03d}-rerender.mp4")
    score_video(con, row["pageid"])
    record_usage(row["pageid"], before)



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
    """Upload every rendered-but-not-uploaded video (or just episode `ep`) in the Publish order."""
    rows = con.execute("select pageid, ep, title from pages where status='rendered' and (? is null or ep=?) "
                       + UPLOAD_ORDER,
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
    assert json_in('Here you go:\n```json\n{"notes": "a {b}", "visuals": []}\n```') == {"notes": "a {b}", "visuals": []}
    assert json_in("no json") == {}
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
    ws = [["One.", 0, 1], ["Two", 2, 2.5], ["two.", 2.5, 3], ["Three", 4, 4.5], ["three.", 4.5, 5], ["Four.", 6.2, 6.8]]
    assert image_starts([0, 1, 2, 3], ws, 7) == [0.0, 2, 4, None]  # 'Four.' starts too close to the end
    assert image_starts([0, 1, 1], ws, 7) == [0.0, 2, None]  # same sentence twice: second image dropped
    assert image_starts([0, None], ws, 7) is None  # hand-picked image without a cue: even spacing
    al = align_words("In 1932, Australia lost a war to 20,000 emus.",
                     [("In", 0, .2), ("1932,", .2, .9), ("Australia", 1, 1.5), ("lost", 1.5, 1.8), ("a", 1.8, 1.9),
                      ("war", 1.9, 2.2), ("to", 2.2, 2.4), ("twenty", 2.4, 2.8), ("thousand", 2.8, 3.3), ("emus.", 3.3, 4)])
    assert [w[0] for w in al] == ["In", "1932,", "Australia", "lost", "a", "war", "to", "20,000", "emus."]
    assert al[7][1:] == [2.4, 3.3] and al[8][1:] == [3.3, 4]  # '20,000' fills the gap the recogniser heard as words
    assert visual_query("August 2026", "PHP2Shell") == "PHP2Shell" and visual_query("70 percent", "X") == "X"
    assert visual_query("Internet Archive", "X") == "Internet Archive" and visual_query("World War II", "X") == "World War II"
    assert mentions("web crawlers", {"name": "Web crawler"}, every=True)
    assert mentions("Internet Archive", {"name": "Internet Archive logo.svg"}) and not mentions("Internet Archive", {"name": "Cat.jpg"})
    assert speakable("PHP versions 5.4–8.5 that") == "PHP versions 5 point 4 to 8 point 5 that"
    assert speakable("holding 89-90% of searches") == "holding 89 to 90% of searches"
    assert speakable("PHP 5.4.8. Pi is 3.14 in 2026, from 1914–1918.") == "PHP 5 point 4 point 8. Pi is 3 point 1 4 in 2026, from 1914 to 1918."
    spoken, pairs = speakable_map("in versions 5.4–8.5. World War II ended")
    w = [[t, k, k + 1] for k, t in enumerate(spoken.replace(".", " .").split())]
    w = [x for x in w if x[0] != "."]; w[8][0] += "."  # tokens as Kokoro gives them: punctuation on the word
    got = restore_written(w, pairs)
    assert [x[0] for x in got] == ["in", "versions", "5.4–8.5.", "World", "War", "II", "ended"], got
    assert got[2][1:] == [2, 9] and got[5][1:] == [12, 13]
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
    r = sub.add_parser("run", help="make a video from anything: a question, topic, paragraph or link (none = next idea)")
    r.add_argument("input", nargs="?"); r.add_argument("--url", dest="input", help=argparse.SUPPRESS)
    r.add_argument("--force", action="store_true")
    r.add_argument("--upload", action="store_true"); r.add_argument("--publish-at")
    a = sub.add_parser("add", help="save ideas: questions, topics, paragraphs or links")
    a.add_argument("items", nargs="+"); a.add_argument("--force", action="store_true")
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
    rr.add_argument("--repick-images", action="store_true", help="choose new images from what the script mentions")
    rr.add_argument("--recording", action="store_const", const="recording", dest="narration", help="use your recorded narration")
    rr.add_argument("--ai-voice", action="store_const", const="ai", dest="narration", help="go back to the AI voice")
    dc = sub.add_parser("discover"); dc.add_argument("--count", type=int, default=5)
    dc.add_argument("--topic"); dc.add_argument("--degree", choices=["1", "2", "3", "any"], default="any")
    dc.add_argument("--source", choices=["wikipedia", "link", "hackernews", "rss", "arxiv", "apod"], default="wikipedia")
    dc.add_argument("--feed", help="RSS/Atom feed URL (for --source rss)")
    dc.add_argument("--link", help="any link (for --source link): feed, site, subreddit, Wikipedia category/list, ...")
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
        for item in args.items:
            add_input(con, item, args.force)
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
        rerender(con, args.ep, args.repick_images, args.narration)
    elif args.cmd == "discover" and args.source != "wikipedia":
        discover_source(con, args.source, args.count, {"rss": args.feed, "link": args.link}.get(args.source, args.topic))
    elif args.cmd == "discover":
        import sources
        if args.topic and sources.looks_like_link(args.topic):
            discover_source(con, "link", args.count, args.topic)
        elif args.topic:
            brainstorm(con, args.topic, args.degree, args.count)
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
        if args.input:
            pageid = add_input(con, args.input, args.force, make_now=True)
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
