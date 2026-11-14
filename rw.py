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

# Channels: the first one lives in the project root, the others in workspaces/<slug>/. Each has its own brand.json,
# data/, work/, out/, brand/ (logo, music, voice sample) and sign-ins; tools, fonts, media/ and API keys are shared.
# RW_WORKSPACE picks one ("" = the first); the studio remembers the one it last had open.
ACTIVE_FILE = ROOT / "data" / "active_workspace"
WS_NAME = os.environ.get("RW_WORKSPACE")
if WS_NAME is None:
    WS_NAME = ACTIVE_FILE.read_text().strip() if sys.argv[1:2] == ["web"] and ACTIVE_FILE.exists() else ""
    if not (ROOT / "workspaces" / WS_NAME).is_dir():
        WS_NAME = ""  # that channel's folder is gone
if WS_NAME and not (re.fullmatch(r"[a-z0-9-]+", WS_NAME) and (ROOT / "workspaces" / WS_NAME).is_dir()):
    raise SystemExit(f"no channel called '{WS_NAME}' (see workspaces/)")
os.environ["RW_WORKSPACE"] = WS_NAME  # every command we start works on the same channel
WS = ROOT / "workspaces" / WS_NAME if WS_NAME else ROOT
BRAND_FILE = WS / "brand.json"  # this channel's settings (git-ignored); seeded from brand.example.json on first run
if not BRAND_FILE.exists():
    import shutil
    shutil.copy(ROOT / "brand.example.json", BRAND_FILE)
BRAND = json.loads(BRAND_FILE.read_text())
C = BRAND["colors"]
DB = WS / "data" / "shorts.db"
if (WS / "data" / "randomwiki.db").exists() and not DB.exists():  # the file's name from before the project was renamed
    (WS / "data" / "randomwiki.db").rename(DB)
WORK, OUT = WS / "work", WS / "out"
SECRETS, WS_SECRETS = ROOT / "secrets", WS / "secrets"  # shared keys and the YouTube client file; this channel's sign-ins
MEDIA = ROOT / "media"  # shared by every channel: your downloads, kept apart from the videos we make: media/video, media/audio, media/image
MEDIA_KINDS = ("video", "audio", "image")
LIBRARY = MEDIA / "video"
if (ROOT / "library").is_dir() and not MEDIA.exists():  # it was library/ (videos only) before audio and images
    MEDIA.mkdir()
    (ROOT / "library").rename(LIBRARY)
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
    for col in ("potential integer", "rank integer", "via text", "origin text", "ref text", "upload_rank integer", "posts text"):  # added after v1; ignore if present
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
CLAUDE_KEY_FILE = SECRETS / "anthropic_api_key"
CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
ai_down = None  # why the cloud model failed this run; the rest of the run then uses the local model
ai_used = {"requests": 0, "tokens": 0}  # this run, for the per-video count
USAGE_FILE = ROOT / "data" / "gemini_usage.json"  # {pacific date: {model: {requests, tokens}}}, Gemini and Claude


def usage_day():
    """Google resets the free daily quota at midnight Pacific time, so days are counted in that zone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def count_ai(model, tokens):
    tokens = int(tokens or 0)
    ai_used["requests"] += 1
    ai_used["tokens"] += tokens
    try:
        log = json.loads(USAGE_FILE.read_text())
    except (OSError, ValueError):
        log = {}
    day = log.setdefault(usage_day(), {}).setdefault(model, {"requests": 0, "tokens": 0})
    day["requests"] += 1
    day["tokens"] += tokens
    USAGE_FILE.write_text(json.dumps(dict(sorted(log.items())[-30:]), indent=1))  # ponytail: last 30 days is plenty


def record_usage(pageid, before):
    """Add the cloud-model requests/tokens used since `before` to the video's meta."""
    used = {k: ai_used[k] - before[k] for k in ai_used}
    mpath = WORK / str(pageid) / "meta.json"
    if not used["requests"] or not mpath.exists():
        return
    m = json.loads(mpath.read_text())
    g = m.pop("gemini", None) or m.get("ai") or {"requests": 0, "tokens": 0}  # "gemini": before Claude was an option
    m["ai"] = {k: g.get(k, 0) + used[k] for k in used}
    mpath.write_text(json.dumps(m, indent=2))
    print(f"  AI: {used['requests']} requests, {used['tokens']:,} tokens")


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
            count_ai(d.get("modelVersion") or model, (d.get("usageMetadata") or {}).get("totalTokenCount"))
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
        except (OSError, KeyError, IndexError, ValueError) as e:  # OSError: dropped connections, timeouts, DNS
            why = f"{model}: {type(e).__name__}"
    raise RuntimeError(why)


def claude_key():
    """ANTHROPIC_API_KEY from the shell, else the key saved in Connectors (secrets/)."""
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return k or (CLAUDE_KEY_FILE.read_text().strip() if CLAUDE_KEY_FILE.exists() else "")


# Other services that speak the OpenAI chat format, each with a free tier and its own key: name -> (label, address,
# model used until you pick one, where to get a key). They write; the facts still come from the web reading we do.
PROVIDERS = {"groq": ("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "https://console.groq.com/keys"),
             "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free", "https://openrouter.ai/keys"),
             "cerebras": ("Cerebras", "https://api.cerebras.ai/v1", "llama-3.3-70b", "https://cloud.cerebras.ai")}
AI_KEYS_FILE = SECRETS / "ai_keys.json"  # {"groq": key, ...}


def provider_key(name):
    """GROQ_API_KEY etc. from the shell, else the key saved in Connectors."""
    return os.environ.get(f"{name.upper()}_API_KEY", "").strip() or secret(AI_KEYS_FILE).get(name, "")


def provider_request(name, path, body=None):
    req = urllib.request.Request(PROVIDERS[name][1] + path, json.dumps(body).encode() if body else None,
                                 {"Authorization": "Bearer " + provider_key(name), "Content-Type": "application/json",
                                  "User-Agent": BRAND["user_agent"]})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            why = str((json.load(e).get("error") or {}).get("message") or "")[:160]
        except Exception:
            why = ""
        raise RuntimeError(f"{PROVIDERS[name][0]}: HTTP {e.code} {why}".strip()) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise RuntimeError(f"{PROVIDERS[name][0]}: {type(e).__name__}") from None


def provider_models(name):
    return sorted(m["id"] for m in provider_request(name, "/models").get("data") or [] if m.get("id"))


def provider_chat(name, prompt, temperature=0.7):
    model = BRAND.get(f"{name}_model") or PROVIDERS[name][2]
    r = provider_request(name, "/chat/completions", {"model": model, "temperature": temperature,
                                                     "messages": [{"role": "user", "content": prompt}]})
    count_ai(f"{name}:{model}", (r.get("usage") or {}).get("total_tokens"))
    return r["choices"][0]["message"]["content"] or ""


def pull_model(name):
    """Download a model into Ollama (it keeps models in its own folder, outside this project), showing progress."""
    req = urllib.request.Request(BRAND["ollama_url"] + "/api/pull", json.dumps({"model": name}).encode(), {"Content-Type": "application/json"})
    seen = ""
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                d = json.loads(line)
                if d.get("error"):
                    raise SystemExit(f"Ollama couldn't download {name}: {d['error']}")
                tenth = f" {10 * (10 * d['completed'] // d['total'])}%" if d.get("total") and d.get("completed") else ""
                if d.get("status", "") + tenth != seen:
                    seen = d.get("status", "") + tenth
                    print("  " + seen, flush=True)
    except urllib.error.URLError:
        raise SystemExit("Ollama isn't running; start it and try again") from None
    print(f"downloaded: {name}. Choose it in Connectors, AI model.")


def writer():
    """Which model writes: the one chosen in Connectors if it has a key, else the local one."""
    want = BRAND.get("ai") or "gemini"
    key = {"gemini": gemini_key, "claude": claude_key}.get(want) or (lambda: provider_key(want) if want in PROVIDERS else "")
    return want if key() else "local"


def claude(prompt, tools=None):
    """One Claude request -> (text, all content blocks). Resends when a long web search pauses the turn."""
    import anthropic
    model = BRAND.get("claude_model") or CLAUDE_MODELS[0]
    extra = {} if "haiku" in model else {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}  # another model answers if one declines
    messages, blocks = [{"role": "user", "content": prompt}], []
    try:
        client = anthropic.Anthropic(api_key=claude_key(), max_retries=3, timeout=300)
        for _ in range(4):
            r = client.beta.messages.create(model=model, max_tokens=16000, messages=messages, tools=tools or anthropic.omit, **extra)
            count_ai(r.model, r.usage.input_tokens + r.usage.output_tokens)
            blocks += r.content
            if r.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in blocks]}]
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"{model}: {e.message.split('. ')[0][:160]}") from None
    except anthropic.APIConnectionError as e:
        raise RuntimeError(f"{model}: {type(e).__name__}") from None
    if r.stop_reason == "refusal":
        raise RuntimeError(f"{model}: declined")
    return "".join(b.text for b in r.content if b.type == "text"), blocks


def json_in(text):
    """The JSON object in a reply that may wrap it in prose or a code fence."""
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m[0]) if m else {}


def llm(prompt, temperature=0.7, local=False):
    """JSON reply from the model chosen in Connectors (Gemini or Claude, when it has a key), else from Ollama.
    local=True keeps bulk scoring on the free local model."""
    global ai_down
    who = writer()
    if not local and not ai_down and who != "local":
        try:
            return json_in(gemini(prompt, temperature)[0] if who == "gemini" else claude(prompt)[0] if who == "claude"
                           else provider_chat(who, prompt, temperature))
        except (RuntimeError, ValueError) as e:
            ai_down = str(e)
            print(f"  {who.title()} unavailable ({ai_down}); using the local model for the rest of this run")
    r = get_json(BRAND["ollama_url"] + "/api/chat", data=json.dumps({
        "model": BRAND["llm"], "stream": False, "format": "json",
        "options": {"num_ctx": 8192, "temperature": temperature},
        "messages": [{"role": "user", "content": prompt}]}).encode())
    return json.loads(r["message"]["content"])


# The prompts a channel can rewrite in Settings → Prompts (saved as "prompts" in its brand.json):
# key -> (name, what it is for, the constant holding the default, where that constant's fixed part starts).
# The fixed part adds the material and the reply format the code reads, so it is shown but never edited.
PROMPTS = {
    "script": ("Script from an article", "how a video is written from a Wikipedia page, news article or web page", "SCRIPT_PROMPT", "Article title:"),
    "question": ("Script that answers a question", "how a video is written from a question or topic and its research; {answer} is the answer found in the research", "QUESTION_PROMPT", "Question or topic:"),
    "text": ("Script from your own text", "how a video is written from a paragraph you paste", "TEXT_PROMPT", "Title: {title}"),
    "brainstorm": ("Finding ideas in your topics", "what kind of ideas are suggested when the ideabank is empty", "BRAINSTORM_PROMPT", 'Topic: "{topic}"'),
    "trend": ("Finding ideas in the news", "how this week's headlines become ideas; {topics} is your channel topics", "TREND_PROMPT", "For each, also say"),
    "score": ("Which articles are worth a video", "how articles and links are scored 1-10 against your minimum interest score", "SCORE_PROMPT", "{context}"),
    "visual": ("Choosing what to show", "what the image search looks for, sentence by sentence", "VISUAL_PROMPT", "Main subject:"),
}


def prompt_parts(key):
    """(default instructions, fixed part) of one editable prompt."""
    text, mark = globals()[PROMPTS[key][2]], PROMPTS[key][3]
    i = text.index(mark)
    return text[:i].strip(), text[i:]


def prompt_for(key, **kw):
    """The text sent to the model: this channel's instructions (or the default), then the fixed part."""
    head, tail = prompt_parts(key)
    head = (BRAND.get("prompts") or {}).get(key) or head
    for k, v in kw.items():  # plain replace: braces the owner types can't break anything
        head = head.replace("{" + k + "}", str(v))
    return head + "\n\n" + tail.format(**kw)


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
    s = llm(prompt_for("score", title=p["title"], text=p.get("extract", "")[:3000], context=context), 0.2, local=True)
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
        s = llm(prompt_for("score", title=a["title"], text=a["text"][:3000], context=""), 0.2, local=True)
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
- State this answer clearly by the third sentence, in your own words: {answer}
- End with a satisfying one-line takeaway.
- Use ONLY facts from the research notes. Do not invent numbers, names or dates.
- Do not say "Wikipedia", "this article", "in this video", or ask viewers to like or subscribe.

Question or topic: {title}
Research notes:
{text}

Reply as JSON: {{"title": "<YouTube title, max 60 characters>", "script": "<narration>", "tags": ["<5 to 8 lowercase tags>"]}}"""

ANSWER_PROMPT = """Question: "{question}"

Research notes:
{text}

Find the sentence in the notes that answers THIS question directly: for "why", the reason or cause; for "how", the
mechanism or steps; for "what", what it is. These are NOT answers: background facts about the topic, its history,
saying it has been studied or debated, or saying the answer is unknown.
Example: for "Why do cats purr?", "Cats are small carnivores kept as pets" is not an answer; "Purring may help cats
calm themselves and heal" is.
Reply as JSON: {{"quote": "<the sentence from the notes that answers it, copied exactly, or empty>",
"answer": "<the answer in one or two plain sentences, or empty>"}}"""

IS_ANSWER_PROMPT = """Question: "{question}"
Sentence: "{quote}"

Does this sentence give an answer to the question: a reason, cause, purpose, mechanism or definition, even a partial
one? Background facts, history, or saying it is studied, debated or unknown do not count.
Reply as JSON: {{"answers": true or false}}"""

SAME_PROMPT = """Expected answer: "{answer}"

Numbered sentences:
{numbered}

Which sentences say the same thing as the expected answer, or part of it, in any words? Sentences that only describe
the topic in general do not count.
Reply as JSON: {{"sentences": [<numbers>]}}"""

ANSWER_FIX_PROMPT = """This narration for a YouTube Short doesn't answer the question "{question}".
Rewrite it so that, right after the opening question, it clearly says: {answer}
Keep it 100 to 120 words, plain spoken English, the hook first and a one-line takeaway last. Use ONLY facts from the notes.

Notes:
{text}

Narration:
{script}

Reply as JSON: {{"title": "<max 60 characters>", "script": "<narration, 100-120 words>"}}"""


def find_answer(question, text):
    """Step 1 of the answer check (local model): the answer the research actually contains, or '' if it has none.
    The model must quote the notes (checked here, so it can't invent one), then judge that quote on its own."""
    r = llm(ANSWER_PROMPT.format(question=question, text=text[:12000]), 0.1, local=True)
    answer = str(r.get("answer") or "").strip()
    return answer if answer and answers(question, str(r.get("quote") or "").strip(), text) else ""


def answers(question, quote, text):
    """True if `quote` really comes from `text` and, judged on its own, answers the question (local model)."""
    if len(quote.split()) < 4 or "?" in quote or not in_text(quote, text):
        return False
    return str(llm(IS_ANSWER_PROMPT.format(question=question, quote=quote), 0.1, local=True).get("answers")).lower() == "true"


def in_text(quote, text):
    """True if `quote` really comes from `text`."""
    norm = lambda x: re.sub(r"\W+", " ", x).lower().strip()
    # every sentence of the quote must be in the text, give or take a word: models join real sentences from different
    # places and drop or add a word when copying, but an invented sentence matches nothing this closely
    import difflib
    split = lambda x: re.split(r"(?<=[.!?])\s+", x)
    parts = [p for p in split(quote) if len(p.split()) >= 5] or [quote]
    whole, sents = norm(text), [norm(x) for x in split(text)]
    real = lambda p: norm(p)[:120] in whole or any(difflib.SequenceMatcher(None, norm(p), x).ratio() >= 0.85 for x in sents)
    return all(real(p) for p in parts)


def ensure_answered(s, question, answer, text):
    """Step 2 (local model): which narration sentences match the answer, and does one of them, judged on its own,
    answer the question? If not, rewrite with the answer spelled out; twice at most."""
    for attempt in range(3):
        sents = re.split(r"(?<=[.!?])\s+", s["script"].strip())
        picked = llm(SAME_PROMPT.format(answer=answer, numbered="\n".join(f"{k + 1}. {x}" for k, x in enumerate(sents))),
                     0.1, local=True).get("sentences") or []
        cands = [sents[int(k) - 1] for k in picked if str(k).isdigit() and 1 <= int(k) <= len(sents)]
        if any(answers(question, c, s["script"]) for c in cands):  # it matches the answer AND, on its own, answers
            return s
        if attempt == 2:
            raise RuntimeError(f"the script still doesn't answer the question; the research says: {answer[:150]}")
        print("  script doesn't answer the question; rewriting it around the answer")
        r = llm(ANSWER_FIX_PROMPT.format(question=question, answer=answer, text=text[:12000], script=s["script"]))
        if 85 <= len(str(r.get("script", "")).split()) <= 128:
            s = {**s, **{k: v for k, v in r.items() if v and k in ("title", "script")}}
            s["title"] = s["title"][:70]


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
    return researched(question, text, sources, how, "Gemini", linked=how != "Google Search")  # search links are redirects


def research_claude(question):
    """Claude researches with Anthropic's web search; sources are the pages it cites (else the results it saw)."""
    tool = "web_search_20250305" if "haiku" in (BRAND.get("claude_model") or "") else "web_search_20260209"
    text, blocks = claude(GEMINI_RESEARCH.format(question=question, how="Search the web for it."),
                          tools=[{"type": tool, "name": "web_search", "max_uses": 5}])
    cited = [(c.title or "", c.url) for b in blocks if b.type == "text" for c in (b.citations or []) if getattr(c, "url", None)]
    seen = [(x.title, x.url) for b in blocks if b.type == "web_search_tool_result" and isinstance(b.content, list) for x in b.content]
    sources = list(dict.fromkeys((urllib.parse.urlsplit(u).netloc.removeprefix("www."), u) for _, u in (cited or seen)))
    return researched(question, text, sources, "web search", "Claude", linked=True)


def researched(question, text, sources, how, who, linked):
    """Turn a model's research reply into an Article; refuse notes it didn't source."""
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
    print(f"  researched with {who} ({how}): {', '.join(sites[:6])}")
    return dict(origin="question", title=question, description="",
                text=notes, url=next((u for n, u in sources if "wikipedia.org/wiki/" in u), sources[0][1]), images=[], popularity=None,
                credit=f"Researched with {who} from: " + (" ".join(u for _, u in sources[:8]) if linked else "; ".join(sites[:8])),
                source_label="Sources: " + ", ".join(sites[:3]), titles=titles)


NOT_FOR_NOTES = re.compile(r"(^|\.)(wikipedia\.org|youtube\.com|youtu\.be|reddit\.com|quora\.com|facebook\.com|instagram\.com|"
                           r"tiktok\.com|pinterest\.com|twitter\.com|x\.com|linkedin\.com)$")  # Wikipedia has its own step


def research_web(question, pages=4):
    """Search the web, read the first few pages that can be read (robots.txt respected), and use their text as the
    notes. No key and no cloud model needed."""
    import sources
    notes, used = [], []
    for url in sources.search_web(question):
        site = urllib.parse.urlsplit(url).netloc.removeprefix("www.")
        if len(used) == pages or NOT_FOR_NOTES.search(site) or site in [s for s, _ in used]:
            continue
        try:
            a = sources.fetch_web(url)
        except (SystemExit, Exception):  # blocked, paywalled, too little text: the next result
            continue
        notes.append(f"## {a['title']} ({site})\n{a['text'][:2600]}")
        used.append((site, a["url"]))
    if len(used) < 2:  # one page is one voice: not enough to call it research
        raise RuntimeError("found fewer than two pages that could be read")
    print(f"  researching on the web: {', '.join(s for s, _ in used)}")
    return dict(origin="question", title=question, description="", text="\n\n".join(notes), url=used[0][1], images=[],
                popularity=None, credit="Researched from: " + " ".join(u for _, u in used),
                source_label="Sources: " + ", ".join(s for s, _ in used[:3]), titles=[])


def research(question):
    """Answer a question from sources actually read: the cloud model chosen in Connectors researches the web; else, or
    if that fails, we search and read the web ourselves; Wikipedia is the last resort (or the only one, if the channel
    is set to "Wikipedia only")."""
    who = writer()
    if who in ("gemini", "claude") and not ai_down:  # the two that can search the web themselves
        try:
            return (research_gemini if who == "gemini" else research_claude)(question)
        except (RuntimeError, ValueError) as e:
            print(f"  {who.title()} research failed ({e}); reading the web ourselves instead")
    if BRAND.get("research_from", "web") == "web":
        try:
            return research_web(question)
        except Exception as e:
            print(f"  web research failed ({e}); researching on Wikipedia instead")
    return research_wikipedia(question)


def research_wikipedia(question):
    """The local LLM picks the Wikipedia articles that cover the question; their text becomes the notes."""
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
    if m := re.match(r"https?://\w+\.wikipedia\.org/w/index\.php\?search=([^&]+)$", t):  # a question idea's search link
        return add_question(con, urllib.parse.unquote_plus(m[1]), force)
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
Each idea is one specific question or subject a curious viewer would click on: concrete, surprising, with a clear
payoff. Mix "how", "why" and "what happened" ideas. Only well-documented subjects. Phrase them as open questions or
subjects, never as claims, so nothing rests on a false premise.

Topic: "{topic}"
{closeness}
Suggest {n} video ideas.
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
    r = llm(prompt_for("brainstorm", topic=topic, closeness=CLOSENESS.get(degree, CLOSENESS["any"]), n=want * 2,
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


TREND_PROMPT = """You find ideas for a YouTube Shorts channel of 50-second explainers about: {topics}.
Below are headlines people are reading this week. For the most promising ones, write one open question a curious viewer
would click, about the lasting science, history or "how does that work" behind the news (e.g. "How do tornado warnings
work?"), not the news event itself. Never assume claims from the headline are true.

For each, also say what kind of story the HEADLINE is, and how well the question fits the channel's topics.

Headlines:
{headlines}

Reply as JSON: {{"ideas": [{{"n": <headline number>, "question": "<max 70 characters>",
"kind": "<one of: science, technology, nature, space, history, how-it-works, health, sports, disaster, crime, war,
politics, celebrity, business, other>", "fit": <1-10, how squarely it belongs to {topics}>}}]}}"""

TREND_SKIP = {"sports", "disaster", "crime", "war", "politics", "celebrity", "business", "other"}  # not explainer material, or tragedies

DEFAULT_TOPICS = ["science", "history", "technology", "nature"]


def channel_topics():
    return [t for t in (BRAND.get("topics") or []) if str(t).strip()] or DEFAULT_TOPICS


def headlines(topics, per_topic=12):
    """This week's news on each topic (Google News RSS), plus today's top searches (Google Trends RSS). No key needed."""
    import random, xml.etree.ElementTree as ET
    out = []
    for t in random.sample(topics, min(3, len(topics))):
        try:
            root = ET.fromstring(fetch("https://news.google.com/rss/search", {"q": f"{t} when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}))
            out += [(re.sub(r"\s+-\s+[^-]+$", "", i.findtext("title") or ""), i.findtext("link") or "") for i in root.iter("item")][:per_topic]
        except Exception as e:
            print(f"  news for '{t}' unavailable ({type(e).__name__})")
    try:
        ns = {"ht": "https://trends.google.com/trending/rss"}
        root = ET.fromstring(fetch("https://trends.google.com/trending/rss", {"geo": "US"}))
        for i in root.iter("item"):
            news = i.find("ht:news_item", ns)
            out.append((f"{i.findtext('title')}: {news.findtext('ht:news_item_title', '', ns) if news is not None else ''}",
                        news.findtext("ht:news_item_url", "", ns) if news is not None else ""))
    except Exception as e:
        print(f"  Google Trends unavailable ({type(e).__name__})")
    return [(h.strip(), u) for h, u in dict(out).items() if h.strip()]


def trending(con, want=1):
    """Ideas from what's in the news in the channel's topics, phrased as explainer questions (researched when made)."""
    topics = channel_topics()
    print(f"looking for trending ideas in: {', '.join(topics)}")
    items = headlines(topics)
    if not items:
        return []
    picks = llm(prompt_for("trend", topics=", ".join(topics), n=want + 2,  # a couple spare: some may be made already
                           headlines="\n".join(f"{k + 1}. {h}" for k, (h, _) in enumerate(items[:60]))), 0.4, local=True).get("ideas") or []
    found = []
    picks = [pk for pk in picks if str(pk.get("kind", "")).lower() not in TREND_SKIP and int(pk.get("fit") or 0) >= 7]  # checked here, not trusted
    for pk in sorted(picks, key=lambda pk: -int(pk.get("fit") or 0)):
        n, q = pk.get("n"), str(pk.get("question") or "").strip()
        if len(found) >= want or not q or not str(n).isdigit() or not 1 <= int(n) <= len(items):
            continue
        pid = add_question(con, q[:90])
        if pid:
            h, link = items[int(n) - 1]
            upsert(con, pid, source="trend", via=link or h, reason=f"trending: {h[:150]}")
            found.append(pid)
    return found


def next_ideas(con, want=1):
    """New ideas when nothing is queued, from Settings → Brand → New ideas: a mix of fresh angles on your topics and
    what's trending in them (default), only one of those, or random Wikipedia pages. Falls back down that list."""
    import random
    how = BRAND.get("idea_source") or "mix"
    order = {"mix": random.sample(["topics", "trending"], 2), "topics": ["topics", "trending"], "trending": ["trending", "topics"],
             "wikipedia": ["wikipedia"]}.get(how, ["topics", "trending"]) + ["wikipedia"]
    if how == "mix" and want > 1:  # several at once: half trending, the rest fresh angles on your topics
        got = []
        for f in (lambda: trending(con, (want + 1) // 2), lambda: brainstorm(con, random.choice(channel_topics()), "any", want - len(got))):
            try:
                got += f()
            except (SystemExit, Exception) as e:
                print(f"  ({e})")
        if got:
            return got
    for src in dict.fromkeys(order):
        try:
            got = (trending(con, want) if src == "trending" else brainstorm(con, random.choice(channel_topics()), "any", want)
                   if src == "topics" else discover(con, want))
        except (SystemExit, Exception) as e:
            print(f"  no {src} ideas ({e})")
            got = []
        if got:
            return got
    raise SystemExit("couldn't find a new idea; add some to the ideabank")


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


def write_script(title, text, prompt="script", check=True, answer=""):
    s = {}
    for _ in range(6):
        n = len(str(s.get("script", "")).split())
        if n < 40:  # nothing usable yet: fresh draft
            s = llm(prompt_for(prompt, title=title, text=text[:12000], answer=answer))
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
            return ensure_answered(s, title, answer, text) if answer else s
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


def src_cleveland(q, limit=12):
    """Cleveland Museum of Art open access: artworks and historical objects, CC0, no key."""
    d = get_json("https://openaccess-api.clevelandart.org/api/artworks/", {"q": q, "cc0": 1, "has_image": 1, "limit": limit})
    return [dict(source="cleveland", url=(x["images"].get("print") or x["images"]["web"])["url"], thumb=x["images"]["web"]["url"],
                 page=x.get("url"), name=x.get("title") or "artwork",
                 artist=re.sub(r"\s*\(.*", "", (x.get("creators") or [{}])[0].get("description") or "") or "Cleveland Museum of Art",
                 license="CC0 (Cleveland Museum of Art)") for x in d.get("data") or [] if (x.get("images") or {}).get("web")]


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


# ---------- stock libraries: Pexels and Pixabay (your own free key), WordPress Photos (CC0, no key) ----------
STOCK_FILE = SECRETS / "stock.json"  # {"pexels": key, "pixabay": key}
STOCK = {"pexels": "Pexels", "pixabay": "Pixabay", "unsplash": "Unsplash", "smithsonian": "Smithsonian", "europeana": "Europeana"}


def stock_key(name):
    """PEXELS_API_KEY / PIXABAY_API_KEY from the shell, else the key saved in Connectors."""
    return os.environ.get(f"{name.upper()}_API_KEY", "").strip() or secret(STOCK_FILE).get(name, "")


def stock_get(name, url, params, headers=None):
    k = stock_key(name)
    if not k:
        raise RuntimeError(f"add a {STOCK[name]} API key in Connectors")
    in_url = {"pixabay": "key", "smithsonian": "api_key", "europeana": "wskey"}.get(name)  # the others send it as a header
    if in_url:
        params = {**params, in_url: k}
    auth = {"pexels": k, "unsplash": "Client-ID " + k}.get(name)
    return http_json(urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                            headers={"User-Agent": BRAND["user_agent"], **({"Authorization": auth} if auth else {})}))


def src_unsplash(q, limit=24):
    d = stock_get("unsplash", "https://api.unsplash.com/search/photos", {"query": q, "per_page": min(limit, 30)})
    return [dict(source="unsplash", url=x["urls"]["regular"], thumb=x["urls"]["small"], page=x["links"]["html"],
                 name=x.get("alt_description") or x.get("description") or "Unsplash photo", artist=(x.get("user") or {}).get("name", ""),
                 license="Unsplash License", tags=x.get("alt_description") or "") for x in d.get("results", [])]


def src_smithsonian(q, limit=12):
    """Smithsonian Open Access: museum objects, specimens and photographs released as CC0."""
    d = stock_get("smithsonian", "https://api.si.edu/openaccess/api/v1.0/search",
                  {"q": f"{q} AND online_visual_material:true", "rows": limit * 2})
    out = []
    for r in (d.get("response") or {}).get("rows") or []:
        dn = (r.get("content") or {}).get("descriptiveNonRepeating") or {}
        m = next((m for m in (dn.get("online_media") or {}).get("media") or []
                  if m.get("type") == "Images" and (m.get("usage") or {}).get("access") == "CC0" and m.get("content")), None)
        if m:
            out.append(dict(source="smithsonian", url=m["content"] + "&max=1600", thumb=(m.get("thumbnail") or m["content"]) + "&max=400", name=r.get("title") or "object",
                            page=dn.get("record_link") or "https://collections.si.edu/search/detail/" + urllib.parse.quote(r.get("id", "")),
                            artist=dn.get("data_source") or "Smithsonian", license="CC0 (Smithsonian Open Access)"))
    return out[:limit]


EUROPEANA_RIGHTS = [("publicdomain/zero", "CC0"), ("publicdomain/mark", "Public domain"), ("licenses/by-sa", "CC BY-SA"), ("licenses/by/", "CC BY")]


def src_europeana(q, limit=12):
    """Europe's museums, libraries and archives; only items whose rights allow reuse (public domain, CC0, BY, BY-SA)."""
    d = stock_get("europeana", "https://api.europeana.eu/record/v2/search.json",
                  {"query": q, "reusability": "open", "media": "true", "qf": "TYPE:IMAGE", "rows": limit * 2, "profile": "rich"})
    out = []
    for x in d.get("items") or []:
        rights = (x.get("rights") or [""])[0]
        lic = next((name for part, name in EUROPEANA_RIGHTS if part in rights), None)
        if lic and x.get("edmIsShownBy"):  # anything else (ND, NC, in copyright) is left out even if the search returned it
            out.append(dict(source="europeana", url=x["edmIsShownBy"][0], thumb=(x.get("edmPreview") or x["edmIsShownBy"])[0],
                            page=(x.get("guid") or "").split("?")[0], name=(x.get("title") or ["item"])[0],
                            artist=(x.get("dcCreator") or x.get("dataProvider") or ["unknown"])[0], license=lic))
    return out[:limit]


def src_archive_video(q, limit=6):
    """Public-domain films from the Prelinger Archives at the Internet Archive (old newsreels, ads, educational films)."""
    docs = get_json("https://archive.org/advancedsearch.php?fl[]=identifier&fl[]=title&" + urllib.parse.urlencode(
        {"q": f"({q}) AND collection:prelinger AND mediatype:movies", "rows": limit * 2, "output": "json"}))["response"]["docs"]
    out = []
    for doc in docs:
        i = doc["identifier"]
        files = [f for f in get_json(f"https://archive.org/metadata/{i}/files").get("result") or [] if f.get("name", "").endswith(".mp4")]
        if files:
            f = min(files, key=lambda f: int(f.get("size") or 10 ** 12))  # the small rendition: we use a few seconds at 720 px
            out.append(dict(source="archive", kind="video", url=f"https://archive.org/download/{i}/{urllib.parse.quote(f['name'])}",
                            thumb=f"https://archive.org/services/img/{i}", page=f"https://archive.org/details/{i}",
                            name=doc.get("title") or i, artist="Prelinger Archives", license="Public domain (Prelinger Archives)"))
        if len(out) >= limit:
            break
    return out


def src_pexels(q, limit=24):
    d = stock_get("pexels", "https://api.pexels.com/v1/search", {"query": q, "per_page": min(limit, 40)})
    return [dict(source="pexels", url=x["src"]["large2x"], thumb=x["src"]["medium"], page=x["url"], name=x.get("alt") or "Pexels photo",
                 artist=x.get("photographer") or "", license="Pexels License", tags=x.get("alt") or "") for x in d.get("photos", [])]


def src_pexels_video(q, limit=12):
    d = stock_get("pexels", "https://api.pexels.com/videos/search", {"query": q, "per_page": min(limit, 40)})
    out = []
    for v in d.get("videos", []):
        files = sorted((f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= 720),
                       key=lambda f: f["height"])  # smallest HD rendition: we only use a few seconds at 1000 px
        if files:
            out.append(dict(source="pexels", kind="video", url=files[0]["link"], thumb=v.get("image", ""), page=v["url"],
                            name=re.sub(r"[-/]", " ", v["url"].rstrip("/").rsplit("/", 1)[-1]).strip() or "Pexels video",
                            artist=(v.get("user") or {}).get("name", ""), license="Pexels License"))
    return out


def src_pixabay(q, limit=24):
    d = stock_get("pixabay", "https://pixabay.com/api/", {"q": q[:100], "image_type": "photo", "safesearch": "true", "per_page": max(3, min(limit, 200))})
    return [dict(source="pixabay", url=x["largeImageURL"], thumb=x["webformatURL"], page=x["pageURL"], name=x.get("tags") or "Pixabay image",
                 artist=x.get("user", ""), license="Pixabay Content License", tags=x.get("tags", "")) for x in d.get("hits", [])]


def src_pixabay_video(q, limit=12):
    d = stock_get("pixabay", "https://pixabay.com/api/videos/", {"q": q[:100], "safesearch": "true", "per_page": max(3, min(limit, 200))})
    out = []
    for x in d.get("hits", []):
        v = (x.get("videos") or {}).get("medium") or (x.get("videos") or {}).get("small") or {}
        if v.get("url"):
            out.append(dict(source="pixabay", kind="video", url=v["url"], thumb=v.get("thumbnail", ""), page=x["pageURL"],
                            name=x.get("tags") or "Pixabay video", artist=x.get("user", ""), license="Pixabay Content License", tags=x.get("tags", "")))
    return out


WP_PHOTOS = "https://wordpress.org/photos/wp-json/wp/v2/"


def src_wordpress(q, limit=24):
    """WordPress Photo Directory: CC0 photos. Its text search matches inside words ("cat" finds "delicate"),
    so search by tag first, else keep only whole-word matches."""
    ql = q.lower().strip()
    tags = get_json(WP_PHOTOS + "photo-tags", {"search": q, "per_page": 20, "_fields": "id,name"})
    ids = [t["id"] for t in tags if t["name"].lower() == ql] or [t["id"] for t in tags if ql in t["name"].lower().split()]
    params = {"per_page": min(limit, 50), "_fields": "link,content,photo-thumbnail-url"}
    photos = get_json(WP_PHOTOS + "photos", params | ({"photo-tags": ",".join(map(str, ids[:5]))} if ids else {"search": q}))
    out = []
    for x in photos:
        desc, thumb = strip_html(x["content"]["rendered"]), x.get("photo-thumbnail-url") or ""
        if thumb and (ids or re.search(rf"\b{re.escape(ql)}\b", desc.lower())):
            out.append(dict(source="wordpress", url=re.sub(r"-\d+x\d+(?=\.\w+$)", "", thumb), thumb=thumb, page=x["link"],
                            name=desc[:90] or "WordPress photo", artist="", license="CC0", tags=desc))
    return out


IMAGE_SOURCES = {"article": src_article, "commons": src_commons, "openverse": src_openverse, "nasa": src_nasa, "met": src_met, "cleveland": src_cleveland,
                 "commons_video": src_commons_video, "nasa_video": src_nasa_video, "wordpress": src_wordpress,
                 "pexels": src_pexels, "pexels_video": src_pexels_video, "pixabay": src_pixabay, "pixabay_video": src_pixabay_video,
                 "unsplash": src_unsplash, "smithsonian": src_smithsonian, "europeana": src_europeana, "archive_video": src_archive_video}


def src_audio(q, kind="music", limit=20):
    """Free music (Jamendo, ccMixter, …) or sound effects (Freesound) through Openverse. Only licences that allow
    putting it under a narration: CC0, CC BY, CC BY-SA, public domain (never ND or NC)."""
    params = {"q": q, "license": "cc0,by,by-sa,pdm", "page_size": min(limit, 20)}
    params |= {"category": "music"} if kind == "music" else {"source": "freesound", "length": "shortest"}
    r = get_json("https://api.openverse.org/v1/audio/", params)
    return [dict(source=x.get("source", ""), url=x["url"], page=x.get("foreign_landing_url") or x["url"], name=x.get("title") or "untitled",
                 artist=x.get("creator") or "unknown", seconds=round((x.get("duration") or 0) / 1000, 1),
                 license="Public domain" if x["license"] in ("cc0", "pdm") else f"CC {x['license'].upper()} {x.get('license_version', '')}".strip())
            for x in r.get("results", []) if x.get("url")]


def download_image(img, dest):
    """Download one candidate into the episode folder; returns it with 'path' set, or None."""
    from PIL import Image
    import hashlib, io
    if img.get("kind") == "video":
        return download_clip(img, dest)
    stem = dest / f"img_{hashlib.sha1(img['url'].encode()).hexdigest()[:10]}"
    path = next((p for p in (stem.with_suffix(".png"), stem.with_suffix(".jpg")) if p.exists()), None)
    if not path and img["url"].startswith("media:"):  # one of your downloaded images
        import shutil
        src = MEDIA / "image" / Path(img["url"][6:]).name
        if not src.is_file():
            raise RuntimeError(f"not in your media any more: {src.name}")
        path = stem.with_suffix(src.suffix)
        shutil.copy(src, path)
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


DRAW_PROMPT = """A YouTube Short is being illustrated with drawn pictures. For each numbered sentence of the narration, describe
ONE picture that shows what the sentence says: a single concrete scene with its main subject, setting and action, in 12
to 25 words. Describe only what is seen. No words, letters, numbers, logos or captions in the picture. Keep recurring
people and things looking the same from picture to picture.

Subject of the video: {title}
Sentences:
{sentences}

Reply as JSON: {{"scenes": ["<picture for sentence 1>", "<picture for sentence 2>", ...]}}"""
DRAW_STYLE = "flat vector illustration, bold simple shapes, limited colour palette, soft grain, no text"


def draw_visuals(title, script, dest, limit=8):
    """A channel whose pictures are drawn (Settings → Videos): one picture per sentence from the model on this Mac,
    all in the channel's style, each shown while its sentence is spoken."""
    import hashlib
    sents = split_sentences(script)[:limit]
    scenes = [str(x).strip() for x in llm(DRAW_PROMPT.format(title=title, sentences="\n".join(
        f"{k + 1}. {x}" for k, x in enumerate(sents))), 0.4).get("scenes") or []]
    style, out = BRAND.get("picture_style") or DRAW_STYLE, []
    for k, scene in enumerate(scenes[:len(sents)]):
        if not scene:
            continue
        path = dest / f"img_{hashlib.sha1(f'{scene}{style}'.encode()).hexdigest()[:10]}.png"
        print(f"  sentence {k + 1}: drawing {scene[:90]!r}", flush=True)
        if not path.exists():
            generate_image(f"{scene}. {style}", path)
        out.append(dict(source="ai", url="ai:" + path.stem, name=scene[:150], page="", artist="FLUX.1 schnell, on this Mac",
                        license="AI-generated", path=str(path), sentence=k, query=scene))
    if len(out) < 2:
        raise RuntimeError("the picture model drew fewer than two pictures")
    return out


def pick_visuals(title, script, dest, own=None, pageimage=None, limit=6, topics=()):
    if BRAND.get("pictures") == "ai" and IMAGE_READY.exists():
        return draw_visuals(title, script, dest)
    """One image per sentence where possible, for the concrete thing that sentence mentions, from Wikipedia entity
    images, then Commons, then Openverse. Each image remembers its sentence so it appears when that is spoken."""
    sents = split_sentences(script)
    try:
        qs = [str(q).strip() for q in llm(prompt_for("visual", title=title, sentences="\n".join(
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
        fallbacks = [lambda: [c for c in src_wordpress(q, 12) if mentions(q, c, every=True)][:3]]
        fallbacks += [lambda f=f: [c for c in f(q, 12) if mentions(q, c, every=True)][:3]
                      for name, f in (("pexels", src_pexels), ("pixabay", src_pixabay)) if stock_key(name)]
        for k2, search in enumerate([lambda: [c for c in src_commons(q, 12) if mentions(q, c, every=True)][:4],
                                     lambda: [c for c in src_commons_video(q, 12) if mentions(q, c, every=True)][:2],
                                     lambda: [c for c in src_openverse(q, 12) if mentions(q, c, every=True)][:3]] + fallbacks):
            if k2 >= 3 and cands:
                break  # WordPress and stock only when the free encyclopedic libraries found nothing
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
        skip = 0 if local else min(dur * 0.25, 120) if dur > 20 else 0  # long clips usually open with a title slate
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *input_params, "-ss", f"{skip:.1f}", "-i", src,
                        "-t", str(seconds), "-an", "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                        "-pix_fmt", "yuv420p", str(path)], timeout=600)

    local = clip["url"].startswith("library:")
    if local and not ok():  # your own download: its start is where you chose to cut it
        src = LIBRARY / Path(clip["url"][8:]).name
        if not src.is_file():
            raise RuntimeError(f"not in your media any more: {src.name}")
        convert(str(src))
    if not ok() and not local:
        try:  # MP4 can be read straight from the server, fetching only the part we use
            convert(clip["url"], ["-user_agent", BRAND["user_agent"]])
        except Exception:
            pass
    if not ok() and not local:  # WebM/Ogg often can't seek over HTTP: download the file (Commons clips are capped at 150 MB)
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
    return {"clone": tts_clone, "edge": tts_edge}.get(BRAND.get("voice_engine"), tts_kokoro)(text, dest)


def edge_voices():
    """Microsoft Edge's online voices as [{name, gender, locale, label}], listed once a week (data/edge_voices.json)."""
    cache = ROOT / "data" / "edge_voices.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        return json.loads(cache.read_text())
    import asyncio, edge_tts
    vs = [dict(name=v["ShortName"], gender=v["Gender"], locale=v["Locale"],
               label=re.sub(r"^Microsoft | Online \(Natural\)", "", v["FriendlyName"])) for v in asyncio.run(edge_tts.list_voices())]
    cache.write_text(json.dumps(vs))
    return vs


def tts_edge(text, dest):
    """An online voice from Microsoft Edge's read-aloud service (free, many languages, needs the internet; not an
    official API, so it can change). It reports each word's timing itself, so captions work in any language."""
    import asyncio, subprocess, edge_tts, imageio_ffmpeg, soundfile as sf
    spoken, _ = speakable_map(text)
    voice, speed = BRAND.get("edge_voice") or "en-US-AndrewNeural", BRAND["voice_speed"]

    async def say(rate):
        heard, mp3 = [], Path(str(dest) + ".mp3")
        with open(mp3, "wb") as f:
            async for ch in edge_tts.Communicate(spoken, voice, rate=f"{round((rate - 1) * 100):+d}%", boundary="WordBoundary").stream():
                if ch["type"] == "audio":
                    f.write(ch["data"])
                elif ch["type"] == "WordBoundary":
                    heard.append((ch["text"], ch["offset"] / 1e7, (ch["offset"] + ch["duration"]) / 1e7))
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(mp3), "-af", "apad=pad_dur=0.5",
                        "-ar", "24000", "-ac", "1", str(dest)], check=True)
        mp3.unlink()
        return heard, sf.info(str(dest)).duration

    try:
        heard, dur = asyncio.run(say(speed))
        if dur > BRAND["max_seconds"] and speed < 1.25:
            speed = min(1.25, speed * dur / (BRAND["max_seconds"] - 1))
            print(f"  narration {dur:.1f}s too long, re-voicing at speed {speed:.2f}")
            heard, dur = asyncio.run(say(speed))
    except Exception as e:
        raise RuntimeError(f"the online voice {voice} didn't answer ({type(e).__name__}: {str(e)[:100]}); check the internet, or pick another voice in Settings") from None
    return align_words(text, heard or transcribe_words(dest), dur), dur


def tts_clone(text, dest):
    """Your cloned voice (Chatterbox, in .venv-clone), then timings from the audio itself."""
    import subprocess, numpy as np, soundfile as sf
    sample = WS / BRAND.get("voice_sample", "brand/voice_sample.wav")
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
    cache = ROOT / "data" / "google_fonts.json"
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


def font(kind, size, weight=None):
    from PIL import ImageFont
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


def backdrop():
    """A blank frame in the channel's colours with its mark and name at the top. -> (image, draw)"""
    from PIL import Image, ImageDraw
    bg = Image.new("RGB", (W, H), C["bg"])
    d = ImageDraw.Draw(bg)
    d.rounded_rectangle((60, 66, 150, 156), 20, fill=C["accent"])
    logo, mark = WS / BRAND.get("logo", "brand/logo.png"), BRAND.get("logo_text", "?")[:2]
    if BRAND.get("logo") and logo.exists():  # uploaded logo image, fitted into the mark
        lm = Image.open(logo).convert("RGBA")
        lm.thumbnail((78, 78), Image.LANCZOS)
        bg.paste(lm, (105 - lm.width // 2, 111 - lm.height // 2), lm)
    else:
        d.text((105, 113), mark, font=font("display", 70 if len(mark) < 2 else 46), fill=C["bg"], anchor="mm")
    d.text((176, 111), BRAND["channel"].upper(), font=font("display", 62), fill=C["text"], anchor="lm")
    return bg, d


def render(meta, words, audio_path, duration, out_path, frame_at=None):
    """Draw the branded video and encode it to out_path. With frame_at=seconds, save just that frame as an image."""
    import subprocess, imageio_ffmpeg
    from PIL import Image, ImageDraw, ImageFont
    ensure_fonts()

    # static layer: header, title, footer
    bg, d = backdrop()
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
                srcs.append((Clip(img["path"]), img))  # credits go in the YouTube description, not on screen
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
                srcs.append((card, img))
                cues.append(img.get("sentence"))
                continue
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                flat = Image.new("RGB", im.size, C["panel"])
                flat.paste(im, mask=im.split()[-1])
                im = flat
            im = im.convert("RGB")
            s = max(PW * 1.15 / im.width, PH * 1.15 / im.height)
            srcs.append((im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS), img))
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
        srcs.append((card, {}))
        cues.append(None)
        # ponytail: one text card when no free images exist; add AI image gen (mflux) if this happens often

    def kb(i, p, start=False):  # Ken Burns crop of source i at progress p in [0,1]; clips play instead
        if isinstance(srcs[i][0], Clip):
            return srcs[i][0].first if start else srcs[i][0].next()
        im, motion = srcs[i][0], srcs[i][1].get("motion") or "auto"
        amount = 0.04 if i in still else 0.12
        z = {"in": 1 + amount * p, "out": 1 + amount * (1 - p), "left": 1 + amount, "right": 1 + amount, "still": 1}.get(
            motion, 1 + amount * (p if i % 2 == 0 else 1 - p))
        bw0 = min(im.width, im.height * PW / PH)
        bw, bh = bw0 / z, bw0 / z * PH / PW
        dx = {"left": -1, "right": 1, "in": 0, "out": 0, "still": 0}.get(motion, 1 if i % 2 == 0 else -1)
        f = 0.5 + dx * (p - 0.5) * 0.6
        cx = bw / 2 + (im.width - bw) * f
        cy = bh / 2 + (im.height - bh) * (1 - f if i % 3 == 2 and motion == "auto" else 0.5)
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

    label_cache = {}

    def overlays(im, items, t_in):
        """Callouts placed in the editor, on top of the panel: arrows draw in, circles trace round, labels pop.
        Positions are fractions of the panel; t_in is seconds since this visual appeared."""
        d = ImageDraw.Draw(im)
        for o in items:
            k = (t_in - float(o.get("delay", 0.3))) / 0.45
            if k <= 0:
                continue
            k = min(k, 1.0)
            pop = 1 + 2.70158 * (k - 1) ** 3 + 1.70158 * (k - 1) ** 2  # ease-out-back: overshoots a little, then settles
            x, y = PX + float(o["x"]) * PW, PY + float(o["y"]) * PH
            if o["type"] == "arrow":  # points at (x, y), coming from the side opposite its direction
                ux, uy = {"left": (1, 0), "right": (-1, 0), "up": (0, 1), "down": (0, -1)}.get(o.get("dir"), (1, 0))
                tx, ty = x + ux * 230, y + uy * 230
                hx, hy = tx + (x - tx) * k, ty + (y - ty) * k  # the head travels from the tail to the target
                px, py = -uy, ux
                for colour, w in (("#000000", 30), (C["accent"], 18)):
                    d.line((tx, ty, hx + ux * 30, hy + uy * 30), fill=colour, width=w)
                    head = 44 + (w - 18)
                    d.polygon([(hx, hy), (hx + ux * head * 1.3 + px * head, hy + uy * head * 1.3 + py * head),
                               (hx + ux * head * 1.3 - px * head, hy + uy * head * 1.3 - py * head)], fill=colour)
            elif o["type"] == "circle":  # traced clockwise around (x, y)
                r = float(o.get("r", 0.12)) * PW
                for colour, w in (("#000000", 22), (C["accent"], 12)):
                    d.arc((x - r, y - r * 0.85, x + r, y + r * 0.85), -90, -90 + 360 * k, fill=colour, width=w)
            elif o["type"] == "label" and str(o.get("text", "")).strip():
                text = str(o["text"]).strip()[:40]
                if text not in label_cache:
                    f = font("display", 64)
                    tw = f.getlength(text)
                    lab = Image.new("RGBA", (int(tw) + 60, 104), (0, 0, 0, 0))
                    ld = ImageDraw.Draw(lab)
                    ld.rounded_rectangle((0, 0, lab.width - 1, lab.height - 1), 26, fill=C["accent"], outline="#000000", width=4)
                    ld.text((lab.width / 2, lab.height / 2), text.upper(), font=f, fill=C["bg"], anchor="mm")
                    label_cache[text] = lab
                lab = label_cache[text]
                s_ = max(0.05, pop)
                lw, lh = max(1, int(lab.width * s_)), max(1, int(lab.height * s_))
                l2 = lab.resize((lw, lh), Image.BILINEAR)
                lx = int(min(max(x - lw / 2, 20), W - 20 - lw))  # keep it on screen
                im.paste(l2, (lx, int(y - lh / 2)), l2)

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
        if srcs[i][1].get("overlays"):
            overlays(im, srcs[i][1]["overlays"], t - starts[i])
        fd = ImageDraw.Draw(im)
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

    work = WORK / str(meta.get("pageid", ""))
    music = {"none": None, "own": work / "music.m4a"}.get(meta.get("music"), WS / BRAND["music"])
    sfx = [(work / srcs[k][1]["sfx"], starts[k]) for k in range(n) if srcs[k][1].get("sfx") and (work / srcs[k][1]["sfx"]).is_file()]
    encode(frame, duration, audio_path,
           audio_mix(music if music and music.exists() else None, meta.get("music_volume", BRAND["music_volume"]), sfx), out_path)


def encode(frame, duration, audio_path, mix, out_path):
    """Pipe frame(t) images to ffmpeg with the voice and the audio_mix() args, into out_path."""
    import subprocess, imageio_ffmpeg
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-i", str(audio_path), *mix,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ar", "48000", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for fi in range(int(duration * FPS)):
        proc.stdin.write(frame(fi / FPS).tobytes())
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError("ffmpeg failed")


def audio_mix(music, music_volume, sfx, sfx_volume=0.8):
    """ffmpeg input and mapping args after the video (input 0) and voice (input 1): music looped under the voice,
    and each sound effect [(path, start seconds)] placed at its moment."""
    args, parts, mix = [], [], ["[1:a]"]
    if music:
        args += ["-stream_loop", "-1", "-i", str(music)]
        parts.append(f"[2:a]volume={music_volume}[m]")
        mix.append("[m]")
    for k, (path, start) in enumerate(sfx):
        j = 2 + bool(music) + k
        args += ["-i", str(path)]
        parts.append(f"[{j}:a]adelay={round(start * 1000)}:all=1,volume={sfx_volume}[s{k}]")
        mix.append(f"[s{k}]")
    if len(mix) == 1:
        return args + ["-map", "0:v", "-map", "1:a"]
    parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0[a]")
    return args + ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[a]"]


# ---------- orchestration ----------
def description(m):
    lines = [m["script"].split(". ")[0].rstrip(".") + ".", "",
             *([f"Source: {m['url']}", ""] if m.get("url") else []),
             f"{BRAND['channel']}: {BRAND['tagline']}", "", "CREDITS",
             m.get("credit") or f"Text adapted from the Wikipedia article \"{m['article_title']}\", CC BY-SA 4.0: {m['url']}"]
    used = {i.get("sfx") for i in m["images"]} | ({"music.m4a"} if m.get("music") == "own" else set())
    lines += [f"{'Music' if f == 'music.m4a' else 'Sound'}: \"{c['name']}\" by {c['artist']}, {c['license']}: {c['page']}"
              for f, c in (m.get("audio_credits") or {}).items() if f in used]
    lines += [f"{'Video' if i.get('kind') == 'video' else 'Image'}: \"{i['name']}\", {credit(i).replace(' · ', ', ')}: {i['page']}"
              for i in m["images"] if credit(i)]
    narration = {"recording": f"Narration: {BRAND['channel']}.", "clone": "Narration: AI voice clone of the creator (Chatterbox).",
                 "edge": "Narration: AI voice (Microsoft Edge text to speech)."}
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


def make(con, pageid, auto=False):
    """Research, script, visuals, voice, render. In manual mode (unless auto) it stops at an editable draft."""
    before = dict(ai_used)
    row = con.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    work = WORK / str(pageid)
    work.mkdir(parents=True, exist_ok=True)
    ep = row["ep"] or (con.execute("select coalesce(max(ep), 0) + 1 from pages").fetchone()[0])
    upsert(con, pageid, ep=ep)
    try:
        origin = row["origin"] or "wikipedia"
        prompt, answer = "script", ""
        if origin == "wikipedia":
            title, desc, text, pageimage = fetch_article(pageid)
            src = dict(url=page_url(title), own=None)
        elif origin == "question":
            a = research(row["ref"])
            title, desc, text, pageimage, prompt = a["title"], a["description"], a["text"], None, "question"
            answer = find_answer(title, text)
            if not answer and not a["credit"].startswith("Researched from the Wikipedia"):  # web research missed it: try Wikipedia
                print("  the web research has no clear answer; trying Wikipedia")
                a = research_wikipedia(row["ref"])
                text = a["text"]
                answer = find_answer(title, text)
            if not answer:  # don't make a video that circles the topic without answering
                raise RuntimeError(f"the research didn't find an answer to \"{title}\"; try rewording the question")
            print(f"  answer found: {answer[:160]}")
            src = dict(url=a["url"], own=None, credit=a["credit"], source_label=a["source_label"], popularity=None,
                       topics=a["titles"])
        elif origin == "text":
            text, title, desc, pageimage, prompt = row["ref"], row["title"], "", None, "text"
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
        s = write_script(title, text, prompt, check=origin != "text", answer=answer)  # your own text is yours to vouch for
        print(f"  \"{s['title']}\"\n  fetching images…")
        images = pick_visuals(title, s["script"], work, own=src.pop("own"), pageimage=pageimage, topics=src.get("topics", ()))
        print(f"  {len(images)} images")
        m = dict(ep=ep, pageid=pageid, origin=origin, article_title=title, description=desc,
                 title=s["title"], script=s["script"], tags=s["tags"], images=images, **src)
        out = video_out(ep, title)
        if BRAND.get("mode") == "manual" and not auto:  # stop here: you edit script, visuals and sound, then render
            m["yt_description"] = description(m)
            (work / "meta.json").write_text(json.dumps(m, indent=2))
            upsert(con, pageid, status="draft", reason="")
            print(f"  draft ready: open EP {ep:03d} in the studio to edit it, then click Render")
            record_usage(pageid, before)
            return None
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


def video_out(ep, title):
    return OUT / f"{ep:03d}-{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:50]}.mp4"


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
        m["narration"] = BRAND.get("voice_engine") if BRAND.get("voice_engine") in ("clone", "edge") else "kokoro"
        print("  voicing…")
        words, dur = tts(text, work / "voice.wav")
        voice = work / "voice.wav"
    m["yt_description"] = description(m)
    (work / "meta.json").write_text(json.dumps(m, indent=2))
    print(f"  rendering {dur:.1f}s…")
    render(m, words, voice, dur, out)
    upsert(con, m["pageid"], status="rendered", video_path=str(out))
    print(f"  done: {out}")


# ---------- library: download videos to keep as references or clips ----------
def print_nothing(*_):  # yt-dlp's own messages; its errors are raised and reported once by us
    pass


def seconds(t):
    """'1:20' / '01:02:03' / '95' -> seconds."""
    n = 0.0
    for part in t.strip().split(":"):
        n = n * 60 + float(part)
    return n


def commons_file(url):
    """A Commons File: page -> (its largest transcode up to 1080p, credit details). yt-dlp can't read Commons pages."""
    title = "File:" + urllib.parse.unquote(url.split("File:", 1)[1].split("#")[0].split("?")[0]).replace("_", " ")
    p = get_json(COMMONS_API, {"action": "query", "format": "json", "formatversion": 2, "titles": title,
                               "prop": "videoinfo", "viprop": "url|extmetadata|derivatives"})["query"]["pages"][0]
    ii = (p.get("videoinfo") or [None])[0]
    if not ii:
        raise SystemExit(f"not a video on Wikimedia Commons: {title}")
    meta = {k: v.get("value") for k, v in ii.get("extmetadata", {}).items()}
    sizes = {d["transcodekey"]: d["src"] for d in ii.get("derivatives", []) if d.get("transcodekey")}  # Wikimedia asks for these
    best = next((sizes[k] for h in ("1080p", "720p", "480p", "360p") for k in (f"{h}.vp9.webm", f"{h}.mpeg4.mov") if k in sizes),
                ii["url"])
    return best, dict(title=p["title"][5:].rsplit(".", 1)[0], url=ii.get("descriptionurl") or url, site="WikimediaCommons",
                      uploader=strip_html(meta.get("Artist")), license=meta.get("LicenseShortName") or "")


def library_poster(path):
    """A still from 1 s in (or the first frame), shown before the video plays and in the visuals editor."""
    import subprocess, imageio_ffmpeg
    jpg = path.with_suffix(".jpg")
    for at in ("1", "0"):
        if not jpg.exists():
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", at, "-i", str(path), "-frames:v", "1",
                            "-vf", "scale=480:-2", str(jpg)], timeout=60)
    return jpg


_side_cache = {}  # sidecar path -> (mtime, details): listing thousands of downloads shouldn't re-read them all


def library_items(kind="video"):
    """Your downloads of one kind, newest first, as their sidecar details."""
    folder, out = MEDIA / kind, []
    for f in sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True) if folder.exists() else []:
        mt = f.stat().st_mtime
        if _side_cache.get(f, (None,))[0] != mt:
            try:
                _side_cache[f] = (mt, json.loads(f.read_text()))
            except ValueError:
                continue
        v = _side_cache[f][1]
        if (folder / v.get("file", "")).is_file():
            out.append(v | {"kind": kind})
    return out


def src_library(q, limit=30):
    """Clips from your own library/ (empty query: all of them), for the visuals editor."""
    words = q.lower().split()
    out = []
    for v in library_items():
        text = " ".join(str(v.get(k, "")) for k in ("title", "uploader", "site", "license", "file")).lower()
        if all(w in text for w in words):
            library_poster(LIBRARY / v["file"])
            out.append(dict(source="library", kind="video", url="library:" + v["file"], name=v["title"], page=v["url"],
                            artist=v.get("uploader") or "", license=v.get("license") or "",
                            thumb="/media/video/" + urllib.parse.quote(Path(v["file"]).with_suffix(".jpg").name)))
    return out[:limit]


def src_media_images(q, limit=30):
    """Images you downloaded into media/image, for the visuals editor."""
    words = q.lower().split()
    return [dict(source="media", url="media:" + v["file"], thumb="/media/image/" + urllib.parse.quote(v["file"]), name=v["title"],
                 page=v["url"], artist=v.get("uploader") or "", license=v.get("license") or "")
            for v in library_items("image")
            if all(w in " ".join(str(v.get(k, "")) for k in ("title", "uploader", "site", "license", "file")).lower() for w in words)][:limit]


# ---------- pictures drawn on this Mac (FLUX.1 schnell through mflux, in its own .venv-image) ----------
IMAGE_MODEL = "mflux-community/flux-1-schnell-mflux-q4"  # Apache-2.0, 4-bit for Apple chips, 9.6 GB in .cache/hf
IMAGE_BIN = ROOT / ".venv-image" / "bin"
IMAGE_READY = ROOT / "data" / "image_model_ready"  # written after the first picture, so a half-finished download doesn't count


def generate_image(prompt, dest, size=768, show=False):
    import subprocess
    r = subprocess.run([str(IMAGE_BIN / "mflux-generate"), "--model", IMAGE_MODEL, "--base-model", "schnell", "--steps", "4",
                        "--width", str(size), "--height", str(size), "--low-ram", "--prompt", prompt, "--output", str(dest)],
                       capture_output=not show, text=True)
    if r.returncode or not Path(dest).exists():
        raise RuntimeError("the picture model failed: " + ((r.stderr or "").strip().splitlines() or ["see the console"])[-1][:200])
    return dest


def image_model_setup():
    """Install the picture model: its runner into .venv-image, the model into .cache/hf, then draw a test picture."""
    import subprocess
    uv = str(ROOT / ".tools" / "bin" / "uv")
    if not (IMAGE_BIN / "mflux-generate").exists():
        print("installing the picture runner (mflux)…", flush=True)
        subprocess.run([uv, "venv", "--python", "3.12", str(ROOT / ".venv-image")], check=True)
        subprocess.run([uv, "pip", "install", "--python", str(IMAGE_BIN / "python"), "mflux"], check=True)
    print("downloading the model (9.6 GB the first time; it resumes if interrupted) and drawing a test picture…", flush=True)
    generate_image("a red apple on a wooden table, photograph", ROOT / "data" / "image_test.png", show=True)
    IMAGE_READY.write_text(IMAGE_MODEL)
    print("the picture model is ready: in a video's Visuals, choose \"Draw a picture with AI\" and describe it")


def src_ai_image(q, limit=1):
    """Draw the picture described (about a minute), keep it in media/image/ and offer it like any other of your images."""
    import hashlib
    from PIL import Image
    if not IMAGE_READY.exists():
        raise RuntimeError("download the picture model first: Connectors, AI model, Pictures")
    folder = MEDIA / "image"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"AI-{hashlib.sha1(f'{q}{time.time()}'.encode()).hexdigest()[:10]}.png"
    generate_image(q, path)
    im = Image.open(path)
    path.with_suffix(".json").write_text(json.dumps(dict(title=q[:150], url="", site="AI picture", uploader="FLUX.1 schnell, on this Mac",
                                                         license="AI-generated", file=path.name, width=im.width, height=im.height,
                                                         saved=time.strftime("%Y-%m-%d %H:%M")), indent=1))
    return [c for c in src_media_images("", 10 ** 6) if c["url"] == "media:" + path.name]


IMAGE_SOURCES["ai"] = src_ai_image
IMAGE_SOURCES["library"] = src_library
IMAGE_SOURCES["media_image"] = src_media_images


def media_used(path, pageid, how=None, title="", ep=None):
    """Note in a media file's sidecar that this channel's video uses it (how: "used", or "spare" for a clip kept in
    reserve), or with how=None that it no longer does. The Media tab shows it."""
    side = Path(path).with_suffix(".json")
    if not side.exists():
        return
    v = json.loads(side.read_text())
    was = v.get("used_in") or []
    now = [u for u in was if (u.get("ws"), u.get("pageid")) != (WS_NAME, pageid)]
    if how:
        now.append(dict(ws=WS_NAME, channel=BRAND["channel"], pageid=pageid, ep=ep, title=title, how=how))
    if now != was:
        side.write_text(json.dumps(v | {"used_in": now}, indent=1))


def media_forget(pageid):
    """A video is gone: nothing in media/ is used by it any more."""
    for side in MEDIA.glob("*/*.json"):
        if f'"pageid": {int(pageid)}' in side.read_text():
            media_used(side, pageid)


def clips_usage(m):
    for c in m.get("clips") or []:
        media_used(c["path"], m["pageid"], "used" if c.get("use", True) else "spare", m["title"], m["ep"])


def download(url, section=None, kind="video", by=None):
    """Save a video or its audio from almost any site (yt-dlp), or an image (direct link, Commons file page, or a page's
    main image), into media/<kind>/, with a small sidecar of where it came from."""
    if kind == "image":
        return download_picture(url)
    import yt_dlp, imageio_ffmpeg
    folder = MEDIA / kind
    folder.mkdir(parents=True, exist_ok=True)
    cut = None
    if section:
        a, _, b = section.partition("-")
        cut = (seconds(a), seconds(b))
        if not cut[0] < cut[1]:
            raise SystemExit(f"section should look like 1:20-1:45, got {section}")
    suffix = f"-{int(cut[0])}-{int(cut[1])}" if cut else ""
    known = {}
    if re.search(r"commons\.wikimedia\.org/wiki/File:", url):
        url, known = commons_file(url)  # its sound or video
    shown = {"p": -1}

    def progress(d):  # a line per 25% instead of yt-dlp's carriage-return bar, which the studio terminal can't show
        if d["status"] == "downloading" and (d.get("total_bytes") or d.get("total_bytes_estimate")):
            pct = int(100 * d.get("downloaded_bytes", 0) / (d.get("total_bytes") or d["total_bytes_estimate"]))
            if pct // 25 > shown["p"]:
                shown["p"] = pct // 25
                print(f"  {pct}%")

    name = ("WikimediaCommons-" + re.sub(r"[^\w-]+", "_", known["title"])[:80]) if known else "%(extractor_key)s-%(id)s"
    ext = "m4a" if kind == "audio" else "mp4"  # one format per kind
    opts = {"outtmpl": str(folder / f"{name}{suffix}.%(ext)s"), "noplaylist": True, "quiet": True,
            "noprogress": True, "no_warnings": True, "progress_hooks": [progress],
            "logger": type("Quiet", (), {"debug": print_nothing, "warning": print_nothing, "error": print_nothing})(),
            "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(), "cachedir": False}
    if kind == "audio":  # just the sound track (or a podcast, a SoundCloud or Bandcamp track…)
        opts |= {"format": "bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]}
    else:
        opts |= {"format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b",
                 "merge_output_format": "mp4", "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]}
    if cut:
        opts |= {"download_ranges": yt_dlp.utils.download_range_func(None, [cut]), "force_keyframes_at_cuts": True}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
            path = Path(y.prepare_filename(info)).with_suffix("." + ext)
    except yt_dlp.utils.DownloadError as e:
        raise SystemExit(re.sub(r"^ERROR:\s*|;? ?please report this issue.*", "", str(e)).split("\n")[0]) from None
    if not path.exists():  # some sites deliver another container
        path = next(p for p in folder.glob(path.stem + ".*") if p.suffix != ".json")
    side = dict(title=info.get("title") or path.stem, url=info.get("webpage_url") or url, site=info.get("extractor_key"),
                uploader=info.get("uploader") or info.get("channel") or "", license=info.get("license") or "",
                duration=round(cut[1] - cut[0] if cut else info.get("duration") or 0), section=section or "",
                file=path.name, saved=time.strftime("%Y-%m-%d %H:%M")) | {k: v for k, v in known.items() if v}
    had = path.with_suffix(".json")
    old = json.loads(had.read_text()) if had.exists() else None
    if old:  # downloaded before: keep who fetched it and which videos use it
        side |= {k: old[k] for k in ("by", "used_in", "saved") if k in old}
    elif by:  # "clips": fetched for a clip video, not something you added yourself
        side["by"] = by
    path.with_suffix(".json").write_text(json.dumps(side, indent=1))
    if kind == "video":
        library_poster(path)
    print(f"saved: {side['title']} -> media/{kind}/{path.name}" + (f"  ({section})" if section else ""))
    return path


def save_upload(kind, src, filename):
    """A file from your device into media/<kind>/: video as MP4, audio as M4A, images as JPEG/PNG, with a sidecar like
    downloads have (no source link). src is a temporary file; returns the sidecar."""
    import hashlib, subprocess, imageio_ffmpeg
    from PIL import Image
    folder = MEDIA / kind
    folder.mkdir(parents=True, exist_ok=True)
    title = re.sub(r"[_\s]+", " ", Path(filename).stem).strip()[:150] or "Upload"
    with open(src, "rb") as f:
        digest = hashlib.sha1(f.read(1 << 20)).hexdigest()[:10]  # first MB: enough to tell files apart
    stem = folder / f"Upload-{re.sub(r'[^A-Za-z0-9]+', '_', title)[:50]}-{digest}"
    side = dict(title=title, url="", site="Uploaded", uploader="", license="", saved=time.strftime("%Y-%m-%d %H:%M"))
    if kind == "image":
        try:
            im = Image.open(src)
            im.load()
        except Exception:
            raise ValueError("that isn't an image file") from None
        alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
        path = stem.with_suffix(".png" if alpha else ".jpg")
        (im.convert("RGBA") if alpha else im.convert("RGB")).save(path, "PNG" if alpha else "JPEG", quality=92)
        side |= dict(width=im.width, height=im.height)
    else:
        path = stem.with_suffix(".m4a" if kind == "audio" else ".mp4")
        ff = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src)]
        tries = ([ff + ["-vn", "-c:a", "aac", "-b:a", "192k", str(path)]] if kind == "audio" else
                 [ff + ["-c", "copy", "-movflags", "+faststart", str(path)],  # already H.264/AAC: just repackage, instantly
                  ff + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-vf", "scale='min(1920,iw)':-2", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-movflags", "+faststart", str(path)]])
        if not any(subprocess.run(t, capture_output=True).returncode == 0 and path.exists() and path.stat().st_size > 1000 for t in tries):
            path.unlink(missing_ok=True)
            raise ValueError(f"couldn't read that {kind} file")
        side["duration"] = round(video_seconds(path))
        if kind == "video":
            library_poster(path)
    side["file"] = path.name
    path.with_suffix(".json").write_text(json.dumps(side, indent=1))
    return side


def download_picture(url):
    """An image into media/image: a direct image link, a Wikimedia Commons File: page (with its licence), or any web
    page's main image (its og:image, what it shows when shared). Galleries and social posts need a gallery tool."""
    import hashlib, io, html as H
    from PIL import Image
    folder = MEDIA / "image"
    folder.mkdir(parents=True, exist_ok=True)
    side = dict(url=url, site=urllib.parse.urlsplit(url).netloc.removeprefix("www."), uploader="", license="")
    if re.search(r"commons\.wikimedia\.org/wiki/File:", url):
        title = "File:" + urllib.parse.unquote(url.split("File:", 1)[1].split("#")[0].split("?")[0]).replace("_", " ")
        p = get_json(COMMONS_API, {"action": "query", "format": "json", "formatversion": 2, "titles": title,
                                   "prop": "imageinfo", "iiprop": "url|extmetadata|mime", "iiurlwidth": 1920})["query"]["pages"][0]
        ii = (p.get("imageinfo") or [{}])[0]
        if not ii.get("url") or not str(ii.get("mime", "")).startswith("image/"):
            raise SystemExit(f"not an image on Wikimedia Commons: {title}")
        meta = {k: v.get("value") for k, v in ii.get("extmetadata", {}).items()}
        src, side = ii.get("thumburl") or ii["url"], side | dict(title=p["title"][5:].rsplit(".", 1)[0], site="WikimediaCommons",
                                                                  uploader=strip_html(meta.get("Artist")), license=meta.get("LicenseShortName") or "")
    else:  # Wikimedia refuses non-standard thumbnail widths: take the original file instead
        src = re.sub(r"(upload\.wikimedia\.org/.+?)/thumb(/.+?)/[^/]+$", r"\1\2", url)
    data = fetch(src)
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:  # a web page: take its main image
        page = data.decode("utf-8", "ignore")
        m = (re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::src)?["\'][^>]*content=["\']([^"\']+)', page, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image|twitter:image)', page, re.I))
        if not m:
            raise SystemExit("no image found there: paste a direct image link, or the page of a single photo")
        t = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)', page, re.I) or re.search(r"<title[^>]*>([^<]+)", page, re.I)
        side["title"] = H.unescape(t[1]).strip()[:150] if t else side["site"]
        data = fetch(urllib.parse.urljoin(url, H.unescape(m[1])))
        im = Image.open(io.BytesIO(data))
        im.load()
    side.setdefault("title", Path(urllib.parse.urlsplit(url).path).stem.replace("_", " ")[:150] or side["site"])
    stem = folder / f"{re.sub(r'[^A-Za-z0-9]+', '', side['site'])[:30]}-{hashlib.sha1(url.encode()).hexdigest()[:10]}"
    alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
    path = stem.with_suffix(".png" if alpha else ".jpg")
    (im.convert("RGBA") if alpha else im.convert("RGB")).save(path, "PNG" if alpha else "JPEG", quality=92)
    side |= dict(file=path.name, width=im.width, height=im.height, saved=time.strftime("%Y-%m-%d %H:%M"))
    path.with_suffix(".json").write_text(json.dumps(side, indent=1))
    print(f"saved: {side['title']} ({im.width}×{im.height}) -> media/image/{path.name}")
    return path


# ---------- clip countdowns: a video made of other videos, no narration ----------
CLIP_FILLER = {"funny", "funniest", "best", "top", "cute", "viral", "oddly", "video", "videos", "clip", "clips", "moments",
               "short", "shorts", "the", "and", "most", "crazy", "epic", "new"}


CLIP_AGE = {0: "CAM%3D", 1: "CAMSAggC", 7: "CAMSAggD", 31: "CAMSAggE"}  # YouTube search, most viewed: ever / today / this week / this month


def clip_candidates(text, limit=200):
    """Videos for a countdown, from words and/or links: YouTube Shorts under the words as a hashtag
    ("funny cats" -> #funnycats) or, on a channel with clip_find "videos", YouTube's most viewed videos for the words
    (of any length, from the last clip_days days); each link as a single
    video (always used: you picked it) or, for a profile or playlist, its latest videos.
    -> [{url, title, views, picked}], your picks first, then the most viewed."""
    import yt_dlp
    links = re.findall(r"https?://\S+", text)
    query = re.sub(r"https?://\S+", " ", text).strip()
    quiet = type("Quiet", (), {"debug": print_nothing, "warning": print_nothing, "error": print_nothing})()
    tag = re.sub(r"\W", "", query.lower())  # YouTube's search returns long videos; a hashtag's Shorts page doesn't
    # ponytail: on topic = the title names the subject ("funny dogs" -> "dog"); clips with vague titles are lost.
    # A model that watches the clip (Gemini) is the upgrade.
    topic = [w.rstrip("s") for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in CLIP_FILLER and len(w) > 2] or [""]
    found, videos = {}, BRAND.get("clip_find") == "videos"
    search = (f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
              f"&sp={CLIP_AGE.get(int(BRAND.get('clip_days') or 0), CLIP_AGE[0])}" if videos else f"https://www.youtube.com/hashtag/{tag}/shorts")
    with yt_dlp.YoutubeDL({"extract_flat": True, "quiet": True, "no_warnings": True, "playlistend": limit,
                           "logger": quiet, "cachedir": False}) as y:
        for src in links + ([search] if tag else []):
            try:
                info = y.extract_info(src, download=False)
            except yt_dlp.utils.DownloadError as e:
                print(f"  couldn't read {src[:70]}: {str(e).split(chr(10))[0][:120]}")
                continue
            many = info.get("_type") == "playlist"
            for e in (info.get("entries") or []) if many else [info]:
                url = e.get("webpage_url") or e.get("url")
                if src == search and not any(w in (e.get("title") or "").lower() for w in topic):
                    continue  # hashtag pages are full of unrelated videos tagged for reach
                if url and url not in found and not (many and not videos and (e.get("duration") or 30) > 90):  # shorts, not long videos
                    found[url] = dict(url=url, title=e.get("title") or "", views=e.get("view_count") or 0, picked=not many)
    return sorted(found.values(), key=lambda c: (not c["picked"], -c["views"]))


def has_text(path, dur):
    """True when captions or a channel stamp sit on a clip: readable text in at least 2 of 3 frames (macOS Vision,
    through the small helper ocr.swift, compiled once into .tools/ocr). Without Swift nothing is filtered."""
    import shutil, subprocess, tempfile, imageio_ffmpeg
    ocr = ROOT / ".tools" / "ocr"
    if not ocr.exists():
        if not shutil.which("swiftc"):
            return False
        ocr.parent.mkdir(exist_ok=True)
        subprocess.run(["swiftc", "-O", str(ROOT / "ocr.swift"), "-o", str(ocr)], check=True, capture_output=True)
    with tempfile.TemporaryDirectory(dir=DB.parent) as tmp:
        frames = [f"{tmp}/{k}.png" for k in range(3)]
        for k, f in enumerate(frames):
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", str(dur * (k + 1) / 4),
                            "-i", str(path), "-frames:v", "1", f], capture_output=True)
        found = subprocess.run([str(ocr), *frames], capture_output=True, text=True).stdout.split()
    return sum(int(n) >= 4 for n in found) >= 2


_demucs = None


def strip_voice(src, dest):
    """Save src's sound to dest (wav) without the human voices: Demucs splits it into voice, drums, bass and the
    rest, and we keep everything but the voice. Singing counts as voice too."""
    # ponytail: runs on the CPU (about real time); Demucs on Apple's GPU is unreliable. Revisit if it's too slow.
    import subprocess, imageio_ffmpeg, soundfile as sf, torch
    global _demucs
    try:
        from demucs.apply import apply_model
        from demucs.pretrained import get_model
    except ImportError:
        raise SystemExit("removing voices needs Demucs: run ./setup.sh once, or turn the option off in Settings") from None
    if _demucs is None:
        _demucs = get_model("htdemucs").eval()  # downloaded once into .cache/torch
    if subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "2",
                       "-ar", str(_demucs.samplerate), str(dest)]).returncode:
        raise RuntimeError(f"couldn't read the sound of {src}")
    wav, sr = sf.read(dest, dtype="float32", always_2d=True)
    mix = torch.from_numpy(wav.T.copy())
    ref = mix.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        parts = apply_model(_demucs, ((mix - mean) / std)[None], device="cpu", split=True)[0]
    keep = sum(parts[k] for k, name in enumerate(_demucs.sources) if name != "vocals") * std + mean
    sf.write(dest, keep.T.numpy(), sr)
    return dest


CLIP_SECONDS = (4, 30)  # a clip is used whole, so it has to be this short
CLIP_PART = (15, 30)  # from a longer video (clip_find "videos"): seconds used, and seconds fetched so the editor can widen it
CLIP_FITS = ("blur", "zoom", "zoom-left", "zoom-right")  # whole over a blurred copy, or filling the frame (which part is kept)


def best_part(info):
    """Where to cut into a long video: its most replayed moment (YouTube's heatmap, opening aside), else a third in."""
    heat = (info.get("heatmap") or [])[5:]  # the first seconds are always the most played
    start = max(heat, key=lambda b: b["value"])["start_time"] if heat else (info.get("duration") or 0) / 3
    return int(max(0, min(start, (info.get("duration") or 0) - CLIP_PART[1])))


CLIP_SPARES = 3  # extra clips fetched beyond the ones used, so you have something to swap in


def make_clips(con, text, count=5, draft=None):
    """Find clips (see clip_candidates) for a countdown. Each is used whole, never cut, so only short ones are taken,
    and ones with text on screen (captions, channel stamps) are dropped. Up to `count` that fit the channel's maximum
    video length are marked to use; a few more are kept as spares. Then the video is rendered (render_clips), or in
    manual mode left as a draft so you can choose and order the clips first."""
    # ponytail: "best" = most viewed, since no model here watches video; let Gemini rate the clips if that isn't enough
    import yt_dlp
    query = re.sub(r"https?://\S+", " ", text).strip()
    used_file = DB.parent / "clips_used.json"  # never show the same clip twice on a channel
    used = set(json.loads(used_file.read_text())) if used_file.exists() else set()
    print(f"looking for clips: {text[:80]}")
    cands = [c for c in clip_candidates(" ".join([text, *(BRAND.get("clip_sources") or [])])) if c["url"] not in used]
    lo, hi = CLIP_SECONDS
    videos, days, any_text = BRAND.get("clip_find") == "videos", int(BRAND.get("clip_days") or 0), bool(BRAND.get("clip_allow_text"))
    since = time.strftime("%Y%m%d", time.localtime(time.time() - days * 86400))
    print(f"  {len(cands)} to choose from; taking the most viewed" + (f" of the last {days} days" if days else "")
          + (f", {CLIP_PART[0]}s from the most replayed part of each long one" if videos else f" that are whole in {lo}-{hi}s")
          + ("" if any_text else " and have no text on screen")
          + f": {count} to use (within {BRAND['max_seconds']}s together) and {CLIP_SPARES} spare")
    clips, total, why = [], 0, {"too long or too short": 0, "too old": 0, "text on screen": 0, "couldn't be read": 0}
    quiet = type("Quiet", (), {"debug": print_nothing, "warning": print_nothing, "error": print_nothing})()
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "logger": quiet, "cachedir": False, "noplaylist": True}) as y:
        for c in cands[:80]:  # ponytail: a fixed cap on how many we look at per run; raise it if runs come up short
            if len(clips) == count + CLIP_SPARES:
                break
            try:
                info = y.extract_info(c["url"], download=False)
                dur, part = info.get("duration") or 0, None
                if days and not c["picked"] and (info.get("upload_date") or since) < since:
                    why["too old"] += 1
                    used.add(c["url"])
                    continue
                if videos and dur > hi:  # a long video: fetch the part around its best moment, use the start of that
                    a = best_part(info)
                    part, dur = f"{a}-{a + CLIP_PART[1]}", CLIP_PART[0]
                elif not lo <= dur <= hi:
                    why["too long or too short"] += 1
                    used.add(c["url"])  # its length won't change: don't look again
                    continue
                path = download(c["url"], part, by="clips")
            except (SystemExit, Exception):
                why["couldn't be read"] += 1
                continue
            if not c["picked"] and not any_text and has_text(path, dur):  # a video you pasted is yours to judge
                why["text on screen"] += 1
                used.add(c["url"])
                for f in (path, path.with_suffix(".json"), path.with_suffix(".jpg")):
                    f.unlink(missing_ok=True)
                print("  dropped it: text on screen")
                continue
            use = sum(x["use"] for x in clips) < count and total + dur <= BRAND["max_seconds"]
            total += dur * use
            clips.append(c | json.loads(path.with_suffix(".json").read_text())
                         | {"path": str(path), "found": c["url"], "seconds": dur, "start": 0, "end": dur, "use": use,
                            "views": c["views"] or info.get("view_count") or 0})
    print("  passed over: " + ", ".join(f"{n} {k}" for k, n in why.items() if n))
    used_file.write_text(json.dumps(sorted(used | {c["found"] for c in clips})))
    if sum(c["use"] for c in clips) < 2:
        raise SystemExit("found fewer than two clips that fit this channel's clip settings; try other words, paste links, "
                         "or add accounts to take clips from in Settings")
    clips.sort(key=lambda c: (not c["use"], c["views"]))  # count down: the most viewed plays last as number 1; spares after
    n = sum(c["use"] for c in clips)
    title = (f"Top {n} {query}" if query else f"Top {n} clips")[:60]
    pid = sources_id(f"clips:{title}:{time.time()}")
    ep = con.execute("select coalesce(max(ep), 0) + 1 from pages").fetchone()[0]
    (WORK / str(pid)).mkdir(parents=True, exist_ok=True)
    m = dict(ep=ep, pageid=pid, origin="clips", article_title=query or title, description="", title=title, script=title + ".",
             tags=[w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2][:5], images=[], url="",
             narration="none", music="none", clips=clips, yt_description="")
    (WORK / str(pid) / "meta.json").write_text(json.dumps(m, indent=2))
    upsert(con, pid, title=title, url="", source="curated", origin="clips", ref=text, reason="clip countdown", status="draft", ep=ep)
    clips_usage(m)
    if BRAND.get("mode") == "manual" if draft is None else draft:
        print(f"  draft ready: open EP {ep:03d} in the studio to choose and order the clips, then click Render")
        return pid
    render_clips(con, m)
    return pid


def clip_add(con, ep, url, part=None):
    """Add one video you chose (any site yt-dlp reads), or one part of it ("1:20-1:45"), to a clip countdown; it is
    used on the next render."""
    row = con.execute("select * from pages where ep=? and origin='clips'", (ep,)).fetchone()
    if not row:
        raise SystemExit(f"episode {ep} is not a clip countdown")
    meta = WORK / str(row["pageid"]) / "meta.json"
    m = json.loads(meta.read_text())
    if not part:  # look before fetching: a whole stream is no clip
        import yt_dlp
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "cachedir": False, "noplaylist": True}) as y:
                dur = y.extract_info(url, download=False).get("duration") or 0
        except yt_dlp.utils.DownloadError as e:
            raise SystemExit(str(e).split("\n")[0][:200]) from None
        if dur > 90:
            raise SystemExit(f"that video is {int(dur)}s long: say which part to use, like 1:20-1:45")
    path = download(url, part)
    side = json.loads(path.with_suffix(".json").read_text())
    if any(c["path"] == str(path) for c in m["clips"]):
        raise SystemExit("that clip is already in this video")
    if not side.get("duration") or side["duration"] > 90:
        raise SystemExit(f"that is {side.get('duration') or '?'}s long; add clips or parts under 90 seconds")
    m["clips"].append(side | {"path": str(path), "found": url, "seconds": side["duration"], "start": 0, "end": side["duration"],
                              "use": True, "views": 0, "picked": True})
    meta.write_text(json.dumps(m, indent=2))
    clips_usage(m)
    print(f"  added to EP {ep:03d}; open it to set the order, then click Render")


def render_clips(con, m):
    """Join the clips marked `use`, in their saved order (the last one is number 1), into the video. Each fills the
    frame over a blurred copy of itself, keeps its own sound (or loses its voices, if the channel says so) and
    carries its rank. A clip's `start`/`end` trim it, and its `fit` (else the channel's clip_fit) says whether it sits
    whole over a blurred copy or is zoomed to fill the frame (CLIP_FITS). Every clip is credited in the description. Pieces are kept, so a reorder re-renders quickly."""
    import subprocess, imageio_ffmpeg
    from PIL import Image, ImageDraw, ImageFont
    ensure_fonts()
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    clips = [c for c in m["clips"] if c.get("use", True) and Path(c["path"]).exists()]
    if len(clips) < 2:
        raise SystemExit("a countdown needs at least two clips ticked")
    work = WORK / str(m["pageid"])
    m["title"] = re.sub(r"^Top \d+ ", f"Top {len(clips)} ", m["title"])  # the title follows the number of clips
    title, strip = m["title"], bool(BRAND.get("clip_strip_voice"))
    big = ImageFont.truetype(str(ROOT / BRAND["fonts"]["display"]), 150)
    segs = []
    for k, c in enumerate(clips):
        label, a = f"#{len(clips) - k}", float(c.get("start") or 0)
        secs = float(c.get("end") or c.get("seconds") or c.get("duration")) - a
        fit = c.get("fit") or BRAND.get("clip_fit") or "blur"
        seg = work / f"seg-{Path(c['path']).stem}-{len(clips) - k}{'-novoice' if strip else ''}-{C['accent'][1:]}-{a:g}-{secs:g}-{fit}.mp4"
        segs.append(seg)
        print(f"  {label}: {c['title'][:60]}")
        if seg.exists():
            continue
        badge = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(badge)
        d.rounded_rectangle((40, 130, 40 + int(big.getlength(label)) + 60, 330), 28, fill=C["accent"])
        d.text((70, 230), label, font=big, fill=C["bg"], anchor="lm")
        badge.save(work / "badge.png")
        probe = subprocess.run([ff, "-hide_banner", "-i", c["path"]], capture_output=True, text=True).stderr
        sound = ["-map", "0:a"] if "Audio:" in probe else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-map", "2:a"]
        if strip and "Audio:" in probe:
            clean = work / f"novoice-{Path(c['path']).stem}.wav"
            if not clean.exists():
                print("    removing voices…")
                strip_voice(c["path"], clean)
            sound = ["-ss", str(a), "-i", str(clean), "-map", "2:a"]
        tmp = seg.with_suffix(".part.mp4")
        fill = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-ss", str(a), "-i", c["path"], "-loop", "1", "-i", str(work / "badge.png"), *sound[:-2],
                            "-filter_complex",
                            (f"{fill},boxblur=24[bg];[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                             f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0];" if fit == "blur" else
                             f"{fill}:{ {'zoom-left': '0', 'zoom-right': 'iw-ow'}.get(fit, '(iw-ow)/2')}:0[v0];") +
                            f"[v0][1:v]overlay=0:0:shortest=1,fps={FPS},format=yuv420p[v]",
                            "-map", "[v]", *sound[-2:], "-t", str(secs), "-shortest", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k", str(tmp)], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"ffmpeg failed on {c['url']}: {r.stderr[-300:]}")
        tmp.rename(seg)
    for f in work.glob("seg-*.mp4"):  # pieces no longer in the video
        if f not in segs:
            f.unlink()
    (work / "list.txt").write_text("".join(f"file '{s}'\n" for s in segs))
    row = con.execute("select video_path from pages where pageid=?", (m["pageid"],)).fetchone()
    out = Path(row["video_path"]) if row and row["video_path"] else video_out(m["ep"], title)
    OUT.mkdir(exist_ok=True)
    if subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(work / "list.txt"),
                       "-c", "copy", "-movflags", "+faststart", str(out)]).returncode:
        raise RuntimeError("ffmpeg could not join the clips")
    credits = [f"#{len(clips) - k}: \"{c['title'][:80]}\" by {c.get('uploader') or 'unknown'} ({c.get('site') or 'web'}): {c['url']}"
               for k, c in enumerate(clips)]
    m["script"] = title + "."
    m["yt_description"] = "\n".join([title + ".", "", f"{BRAND['channel']}: {BRAND['tagline']}", "", "CLIPS", *credits, "",
                                     " ".join("#" + re.sub(r"\W", "", t) for t in BRAND["hashtags"] + m["tags"][:3])])[:4900]
    (work / "meta.json").write_text(json.dumps(m, indent=2))
    clips_usage(m)
    upsert(con, m["pageid"], title=title, status="rendered", video_path=str(out))
    print(f"  done: {out}")


# ---------- quizzes: a question and three options on screen, a few seconds to think, then the answer ----------
QUIZ_PROMPT = """Write a multiple-choice quiz for a YouTube Short about: "{topic}"
Use ONLY facts stated in the notes below. Write {n} questions a curious viewer has a fair chance at: not trivial,
not obscure, each about a different fact.
Each question: at most 14 words, with exactly one correct answer, and 3 options of at most 4 words each: the correct
one and two plausible wrong ones of the same kind (three planets, three years, three people).
For each, copy the one sentence from the notes that proves the answer, word for word. The answer's own words must
appear in that sentence, and the wrong options must not.
Notes:
{text}
Reply as JSON: {{"title": "<a title of at most 60 characters that dares the viewer to try>", "questions": [{{"question":
"<question>", "options": ["<option>", "<option>", "<option>"], "answer": "<the correct option, copied exactly>",
"quote": "<the sentence from the notes>"}}]}}"""

QUIZ_CHECK = """Sentence: "{quote}"
Question: "{question}"
Options: {options}
Going only by the sentence, which option answers the question?
Reply as JSON: {{"answer": "<that option, copied exactly, or none if the sentence doesn't say>"}}"""

QUIZ_THINK = 3  # seconds the viewer gets before the answer


def quiz_questions(topic, text, count):
    """Questions for a quiz, each proven by a sentence of the research: the writer's model drafts them, and code keeps
    only those whose proof is really in the notes, names the answer and no other option, and from which the local
    model, shown nothing but that sentence, picks the same answer. -> (title, [{question, options, answer, quote}])"""
    import random
    norm = lambda x: re.sub(r"\W+", " ", str(x)).lower().strip()
    good, title, why = [], "", {"proof not in the research": 0, "proof doesn't settle it": 0, "badly formed": 0}
    for _ in range(2):
        r = llm(QUIZ_PROMPT.format(topic=topic, n=count + 3, text=text[:12000]), 0.6)
        title = title or re.sub(r".*\d.*", "", str(r.get("title") or "").strip()[:70])  # a number in it may not match what's kept
        for q in r.get("questions") or []:
            if len(good) == count:
                break
            try:
                question, answer, quote = str(q["question"]).strip(), str(q["answer"]).strip(), str(q["quote"]).strip()
                opts = [str(o).strip() for o in q["options"]]
            except (KeyError, TypeError):
                why["badly formed"] += 1
                continue
            wrong = [o for o in opts if norm(o) != norm(answer)]
            if (len(opts) != 3 or len(wrong) != 2 or norm(wrong[0]) == norm(wrong[1]) or len(question.split()) > 16
                    or any(len(o.split()) > 5 or not norm(o) for o in opts)
                    or any(norm(question) == norm(g["question"]) or norm(answer) == norm(g["answer"]) for g in good)):
                why["badly formed"] += 1
            elif not in_text(quote, text):
                why["proof not in the research"] += 1
            elif (f" {norm(answer)} " not in f" {norm(quote)} " or any(f" {norm(o)} " in f" {norm(quote)} " for o in wrong)
                  or norm(llm(QUIZ_CHECK.format(quote=quote, question=question, options="; ".join(opts)), 0.1, local=True)
                          .get("answer")) != norm(answer)):
                why["proof doesn't settle it"] += 1
            else:
                random.shuffle(opts)  # models like to put the right one first
                good.append(dict(question=question, options=opts, answer=answer, quote=quote))
        if len(good) == count:
            break
    print(f"  {len(good)} questions kept" + "".join(f"; {n} dropped ({k})" for k, n in why.items() if n))
    if len(good) < 3:
        raise RuntimeError(f"the research on \"{topic}\" didn't give three questions that could be proven from it; try another topic")
    return title, good


def make_quiz(con, text="", count=5):
    """A quiz video on a topic: research it, write questions the research proves (quiz_questions), then voice and
    render. Always made straight away; the questions can be edited in the studio afterwards."""
    before, topic = dict(ai_used), text.strip() or random_topic()
    print(f"quiz on: {topic}\n  researching…")
    a = research(topic)
    title, quiz = quiz_questions(topic, a["text"], count)
    pid, ep, _ = new_video(con, f"quiz:{topic}")
    m = dict(ep=ep, pageid=pid, origin="quiz", article_title=topic, description="", title=title or f"{len(quiz)} questions: {topic}"[:70],
             quiz=quiz, script="", images=[],
             tags=[w for w in re.findall(r"[a-z0-9]+", topic.lower()) if len(w) > 2 and w not in ("the", "and")][:5],
             url=a["url"], credit=a["credit"], source_label=a["source_label"])
    return make_format(con, m, topic, before)


def render_quiz(m, scenes, audio_path, duration, out_path, frame_at=None):
    """Draw a quiz: a title card, then per question the question with its three options, a bar and a number counting
    the thinking time down, and the right option lit up; last, a card asking for the score."""
    import math
    ensure_fonts()
    quiz, n, cards = m["quiz"], len(m["quiz"]), {}

    def lines(d, text, kind, size, y, max_lines, fill, centre=False, width=W - 120, x=60):
        f, ls = fit_lines(text, kind, size, width, max_lines)
        for l in ls:
            d.text((W // 2 if centre else x, y), l, font=f, fill=fill, anchor="mt" if centre else "lt")
            y += f.size * 1.2
        return y

    def card(kind, k):
        if (kind, k) in cards:
            return cards[kind, k]
        im, d = backdrop()
        if kind == "intro":
            y = lines(d, m["title"].upper(), "display", 130, 560, 5, C["text"], centre=True)
            lines(d, f"{n} QUESTION{'S' * (n > 1)} · {QUIZ_THINK} SECONDS EACH", "display", 60, y + 60, 1, C["accent"], centre=True)
        elif kind == "outro":
            y = lines(d, "HOW MANY DID YOU GET?", "display", 150, 640, 3, C["accent"], centre=True)
            lines(d, f"Tell us your score out of {n} in the comments", "body", 48, y + 50, 2, C["muted"], centre=True)
        else:
            q = quiz[k]
            d.text((60, 230), f"QUESTION {k + 1} OF {n}", font=font("display", 54), fill=C["accent"])
            lines(d, q["question"], "display", 96, 330, 5, C["text"])
            for j, o in enumerate(q["options"]):
                y, right = 900 + j * 215, kind == "reveal" and o == q["answer"]
                d.rounded_rectangle((60, y, W - 60, y + 180), 36, fill=C["accent"] if right else C["panel"])
                ink = C["bg"] if right else C["muted"] if kind == "reveal" else C["text"]
                d.ellipse((90, y + 40, 190, y + 140), fill=C["bg"] if right else C["accent"] if kind != "reveal" else C["bg"])
                d.text((140, y + 92), "ABC"[j], font=font("display", 64), anchor="mm",
                       fill=C["accent"] if right else C["bg"] if kind != "reveal" else C["muted"])
                f, ls = fit_lines(o, "display", 72, W - 120 - 200, 2)
                for i, l in enumerate(ls):
                    d.text((225, y + 92 + (i - (len(ls) - 1) / 2) * f.size * 1.15), l, font=f, fill=ink, anchor="lm")
        cards[kind, k] = im
        return im

    def frame(t):
        from PIL import ImageDraw
        s = next((s for s in scenes if s["start"] <= t < s["end"]), scenes[-1])
        im = card("ask" if s["kind"] == "think" else s["kind"], s["q"]).copy()
        d = ImageDraw.Draw(im)
        if s["kind"] in ("ask", "think"):  # the thinking time: a bar that empties and the seconds left
            left = 1 - (t - s["start"]) / QUIZ_THINK if s["kind"] == "think" else 1
            d.rounded_rectangle((60, 1580, W - 60, 1610), 15, fill=C["panel"])
            d.rounded_rectangle((60, 1580, 60 + max(30, int((W - 120) * left)), 1610), 15, fill=C["accent"])
            if s["kind"] == "think":
                d.text((W // 2, 1710), str(math.ceil(left * QUIZ_THINK)), font=font("display", 150), fill=C["accent"], anchor="mm")
        d.rounded_rectangle((PX, 1826, PX + PW, 1836), 5, fill=C["panel"])
        d.rounded_rectangle((PX, 1826, PX + max(10, int(PW * t / duration)), 1836), 5, fill=C["accent"])
        return im

    if frame_at is not None:
        frame(frame_at).save(out_path)
        return
    music = WS / BRAND["music"]
    encode(frame, duration, audio_path, audio_mix(music if music.exists() else None, BRAND["music_volume"], []), out_path)


def quiz_steps(m):
    n = len(m["quiz"])
    return ([("intro", None, f"{number_words(n).capitalize()} question{'s' * (n > 1)} on {m['article_title']}. "
                             f"{number_words(QUIZ_THINK).capitalize()} seconds each.", 0, None)]
            + [s for k, q in enumerate(m["quiz"]) for s in (("ask", k, q["question"], 0, None), ("think", k, None, QUIZ_THINK, "tick"),
                                                            ("reveal", k, q["answer"] + ".", 0.4, "chime"))]
            + [("outro", None, "How many did you get? " + BRAND["outro_line"], 0, None)])


# ---------- formats: videos made of voiced scenes (quiz, guess the picture, on this day) ----------
def voice_scenes(work, steps, said):
    """Voice a video scene by scene, so each has its own moment on screen. steps: [(kind, index, line or None,
    seconds of quiet after it, sound)], sound "tick" = one per second of the quiet, "chime" = one as the scene opens.
    Writes work/voice.wav -> [{kind, q, start, end}]. `said` keeps voiced lines between calls."""
    import numpy as np, soundfile as sf
    sr = said.get("sr") or 24000

    def beep(freq, secs):
        t = np.arange(int(sr * secs)) / sr
        return (0.2 * np.sin(2 * np.pi * freq * t) * np.exp(-t * 5 / secs)).astype("float32")

    audio, scenes, at = [], [], 0
    for kind, q, line, quiet, sound in steps:
        if line and line not in said:
            tts(line, work / "part.wav")
            wav, sr = sf.read(work / "part.wav", dtype="float32")
            (work / "part.wav").unlink()
            wav = wav.mean(1) if wav.ndim > 1 else wav
            loud = np.flatnonzero(np.abs(wav) > 0.01)  # engines pad the end with silence; keep a breath of it
            said[line], said["sr"] = (wav[:loud[-1] + int(sr * 0.2)] if len(loud) else wav), sr
        wav = np.concatenate([said[line] if line else np.zeros(0, "float32"), np.zeros(int(sr * max(quiet, 0.45 * (sound == "chime"))), "float32")])
        for s in range(int(quiet) if sound == "tick" else 0):
            wav[s * sr:s * sr + int(sr * 0.12)] += beep(880, 0.12)
        if sound == "chime":
            wav[:int(sr * 0.4)] += beep(1320, 0.4)
        audio.append(wav)
        scenes.append(dict(kind=kind, q=q, start=at / sr, end=(at + len(wav)) / sr))
        at += len(wav)
    sf.write(work / "voice.wav", np.concatenate(audio), sr)
    return scenes


def produce_format(con, m):
    """Voice and render a quiz, guess or on-this-day video from its meta. While it runs over the channel's maximum
    length, the last question (picture, event) is left out."""
    key, steps_for, renderer, least = FORMATS[m["origin"]][1:]
    work, said = WORK / str(m["pageid"]), {}
    m["narration"] = BRAND.get("voice_engine") if BRAND.get("voice_engine") in ("clone", "edge") else "kokoro"
    print("  voicing…")
    while True:
        steps = steps_for(m)
        scenes = voice_scenes(work, steps, said)
        if scenes[-1]["end"] <= BRAND["max_seconds"] or len(m[key]) <= least:
            break
        print(f"  {scenes[-1]['end']:.0f}s is over the channel's {BRAND['max_seconds']}s; leaving out the last one")
        m[key].pop()
    m["script"] = " ".join(line for _, _, line, _, _ in steps if line)
    if key == "items":
        m["images"] = [i["image"] for i in m["items"] if i.get("image")]
    dur = scenes[-1]["end"]
    row = con.execute("select video_path from pages where pageid=?", (m["pageid"],)).fetchone()
    out = Path(row["video_path"]) if row and row["video_path"] else video_out(m["ep"], m["article_title"])
    OUT.mkdir(exist_ok=True)
    m["yt_description"] = description(m | {"script": steps[0][2]})  # opens with the intro line
    (work / "meta.json").write_text(json.dumps(m, indent=2))
    print(f"  rendering {dur:.1f}s…")
    renderer(m, scenes, work / "voice.wav", dur, out)
    upsert(con, m["pageid"], title=m["title"], status="rendered", video_path=str(out))
    print(f"  done: {out}")


def make_format(con, m, ref, before):
    """Shared end of make_quiz / make_guess / make_onthisday: save the new video's row, voice and render it, score it."""
    print(f"[{m['ep']:03d}] {m['title']}")
    upsert(con, m["pageid"], title=m["title"], url=m.get("url", ""), source="curated", origin=m["origin"], ref=ref,
           reason=m["origin"], status="draft", ep=m["ep"])
    try:
        produce_format(con, m)
    except BaseException as e:
        upsert(con, m["pageid"], status="failed", reason=f"{type(e).__name__}: {e}"[:300])
        raise
    try:
        score_video(con, m["pageid"])
    except Exception as e:
        print(f"  could not score: {e}")
    record_usage(m["pageid"], before)
    return m["pageid"]


def new_video(con, key):
    """-> (pageid, episode number, work folder) for a video about to be made."""
    pid = sources_id(f"{key}:{time.time()}")
    (WORK / str(pid)).mkdir(parents=True, exist_ok=True)
    return pid, con.execute("select coalesce(max(ep), 0) + 1 from pages").fetchone()[0], WORK / str(pid)


def lead_picture(title):
    """The main picture of a Wikipedia article, if it is a free-licensed photograph (not a logo, map or diagram).
    -> (article title, its one-line description, image) or None"""
    p = wiki(titles=title, redirects=1, prop="description|pageimages", piprop="name")["query"]["pages"][0]
    if p.get("missing") or not p.get("pageimage"):
        return None
    img = src_article(p["title"], 1, p["pageimage"])
    if not img or img[0]["name"].replace(" ", "_") != p["pageimage"] or "fit" in img[0]:
        return None
    return p["title"], p.get("description", ""), img[0]


def cover(path, w, h, extra=1.0):
    """The picture at `path`, scaled and centre-cropped to fill w×h (×extra, to leave room for a slow zoom)."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = round(w * extra), round(h * extra)
    s = max(w / im.width, h / im.height)
    im = im.resize((max(w, round(im.width * s)), max(h, round(im.height * s))), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 3  # a little above centre: faces and tops of buildings
    return im.crop((x, y, x + w, y + h))


def text_lines(d, text, kind, size, y, max_lines, fill, centre=False, weight=None):
    f, ls = fit_lines(text, kind, size, W - 120, max_lines, weight)
    for l in ls:
        d.text((W // 2 if centre else 60, y), l, font=f, fill=fill, anchor="mt" if centre else "lt")
        y += f.size * 1.2
    return y


def render_scenes(scenes, draw, audio_path, duration, out_path, frame_at=None):
    """Encode a video whose frame at time t is draw(scene, t) with the progress bar added along the bottom."""
    from PIL import ImageDraw

    def frame(t):
        im = draw(next((s for s in scenes if s["start"] <= t < s["end"]), scenes[-1]), t)
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((PX, 1826, PX + PW, 1836), 5, fill=C["panel"])
        d.rounded_rectangle((PX, 1826, PX + max(10, int(PW * t / duration)), 1836), 5, fill=C["accent"])
        return im

    if frame_at is not None:
        return frame(frame_at).save(out_path)
    music = WS / BRAND["music"]
    encode(frame, duration, audio_path, audio_mix(music if music.exists() else None, BRAND["music_volume"], []), out_path)


def countdown(d, left, seconds):
    """The thinking time on screen: a bar that empties and the seconds left. left: 1 = all of it, 0 = none."""
    import math
    d.rounded_rectangle((60, 1580, W - 60, 1610), 15, fill=C["panel"])
    d.rounded_rectangle((60, 1580, 60 + max(30, int((W - 120) * left)), 1610), 15, fill=C["accent"])
    if left < 1:
        d.text((W // 2, 1710), str(math.ceil(left * seconds)), font=font("display", 150), fill=C["accent"], anchor="mm")


# ---------- guess the picture: a blurred photo clears while the viewer names it ----------
GUESS_PROMPT = """We are making a "name it before the picture clears" YouTube Short about: "{topic}"
List {n} examples that viewers around the world would recognise from one photograph, each as the exact title of its
English Wikipedia article. Choose ones that look clearly different from each other; no two of the same thing.
{skip}Reply as JSON: {{"items": ["<Wikipedia article title>", ...]}}"""
GUESS_THINK = 4  # seconds the picture takes to clear


def make_guess(con, text="", count=5):
    """A guessing video on a kind of thing ("famous landmarks", "dog breeds"): the writer's model names well-known
    examples, and each is used only if Wikipedia has an article on it whose main picture is a free photograph. What is
    shown as the answer is that article's own title and one-line description. A channel never repeats a subject."""
    before, topic = dict(ai_used), text.strip() or random_topic()
    used_file = DB.parent / "guess_used.json"
    used = json.loads(used_file.read_text()) if used_file.exists() else []
    print(f"guess the picture: {topic}")
    skip = f"Not these, already shown: {', '.join(used[-60:])}.\n" if used else ""
    r = llm(GUESS_PROMPT.format(topic=topic, n=count + 6, skip=skip), 0.8)
    pid, ep, work = new_video(con, f"guess:{topic}")
    items = []
    for t in [str(t) for t in r.get("items") or []]:
        got = lead_picture(t)
        if not got or got[0] in used or any(got[0] == i["title"] for i in items):
            print(f"  passed over {t}: " + ("already shown" if got else "no article with a free photograph as its main picture"))
            continue
        img = download_image(got[2], work)
        items.append(dict(title=got[0], name=re.sub(r"\s*\([^)]*\)$", "", got[0]), text=got[1], image=img))
        print(f"  {items[-1]['name']}: {got[1]}")
        if len(items) == count:
            break
    if len(items) < 3:
        raise SystemExit(f"found fewer than three well-known {topic} with a free photograph on Wikipedia; try another subject")
    used_file.write_text(json.dumps(used + [i["title"] for i in items]))
    m = dict(ep=ep, pageid=pid, origin="guess", article_title=topic, description="",
             title=f"Can You Name These {topic.title()} Before They Clear?"[:90], items=items, script="", images=[],
             tags=[w for w in re.findall(r"[a-z0-9]+", topic.lower()) if len(w) > 2 and w not in ("the", "and")][:5], url="",
             credit="Names, descriptions and pictures from English Wikipedia articles, CC BY-SA 4.0.")
    return make_format(con, m, topic, before)


def guess_steps(m):
    n = len(m["items"])
    return ([("intro", None, f"Can you name these {number_words(n)} {m['article_title']} before the picture clears?", 0, None)]
            + [s for k, i in enumerate(m["items"]) for s in (("ask", k, f"Number {number_words(k + 1)}.", 0, None),
                                                            ("think", k, None, GUESS_THINK, "tick"), ("reveal", k, i["name"] + ".", 1.2, "chime"))]
            + [("outro", None, "How many did you get? " + BRAND["outro_line"], 0, None)])


def render_guess(m, scenes, audio_path, duration, out_path, frame_at=None):
    """Each picture starts as a blur and clears over GUESS_THINK seconds with a countdown; then its name appears."""
    from PIL import Image, ImageDraw, ImageFilter
    ensure_fonts()
    items, n, cards, levels = m["items"], len(m["items"]), {}, {}
    mask = Image.new("L", (PW, PH), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, PW, PH), 36, fill=255)

    def card(kind, k):
        if (kind, k) not in cards:
            im, d = backdrop()
            if kind == "intro":
                y = text_lines(d, m["title"].upper(), "display", 130, 560, 5, C["text"], centre=True)
                text_lines(d, f"{n} PICTURES · NAME EACH BEFORE IT CLEARS", "display", 56, y + 60, 1, C["accent"], centre=True)
            elif kind == "outro":
                y = text_lines(d, "HOW MANY DID YOU GET?", "display", 150, 640, 3, C["accent"], centre=True)
                text_lines(d, f"Tell us your score out of {n} in the comments", "body", 48, y + 50, 2, C["muted"], centre=True)
            else:
                d.text((60, 205), f"NUMBER {k + 1} OF {n}", font=font("display", 54), fill=C["accent"])
                text_lines(d, items[k]["name"].upper() if kind == "reveal" else "WHAT IS THIS?", "display", 110, 290, 1,
                           C["accent"] if kind == "reveal" else C["text"])
                if kind == "reveal":
                    text_lines(d, items[k]["text"][:1].upper() + items[k]["text"][1:], "body", 50, 1500, 3, C["muted"], weight="Medium")
            cards[kind, k] = im
        return cards[kind, k]

    def picture(k, clear):  # clear: 0 = fully blurred, 1 = sharp
        if k not in levels:
            sharp = cover(items[k]["image"]["path"], PW, PH)
            levels[k] = [sharp.filter(ImageFilter.GaussianBlur(r)) for r in (70, 48, 32, 20, 11, 5)] + [sharp]
        x = min(max(clear, 0), 1) * (len(levels[k]) - 1)
        i = min(int(x), len(levels[k]) - 2)
        return Image.blend(levels[k][i], levels[k][i + 1], x - i)

    def draw(s, t):
        kind = s["kind"]
        im = card("ask" if kind == "think" else kind, s["q"]).copy()
        if s["q"] is not None:
            left = 1 - (t - s["start"]) / GUESS_THINK if kind == "think" else 1
            im.paste(picture(s["q"], 1 if kind == "reveal" else 1 - left), (PX, PY), mask)
            if kind != "reveal":
                countdown(ImageDraw.Draw(im), left, GUESS_THINK)
        return im

    render_scenes(scenes, draw, audio_path, duration, out_path, frame_at)


# ---------- on this day: a few things that happened on today's date, each with its year and a picture ----------
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
# ponytail: a word list keeps deaths and disasters out of a cheerful daily video; it also drops some harmless events
GRIM = re.compile(r"kill|dead\b|died|death|massacre|murder|assassin|crash|bomb|terror|genocide|suicide|\bshot\b|execut|disaster|"
                  r"\bwar\b|attack|invad|invasion|riot|famine|earthquake|hurricane|cyclone|tsunami|hijack|sank\b|explo", re.I)


def day_from(text):
    """(month, day) named in the text ("25 December", "December 25", "12-25" = month-day), else today's."""
    import datetime
    name = re.search("|".join(MONTH_NAMES), text, re.I)
    nums = [int(x) for x in re.findall(r"\d{1,2}", text)]
    mo, d = ((MONTH_NAMES.index(name.group().capitalize()) + 1, nums[0]) if name and nums else tuple(nums[:2]) if len(nums) >= 2
             else (datetime.date.today().month, datetime.date.today().day))
    try:
        datetime.date(2024, mo, d)  # a leap year, so 29 February counts
    except ValueError:
        raise SystemExit(f"\"{text}\" isn't a date; write it like 25 December or 12-25") from None
    return mo, d


def make_onthisday(con, text="", count=4):
    """Events from Wikipedia's "On this day" list for a date (today's unless the text names one), oldest first. An
    event is used if it isn't grim, is short enough to read on screen, and one of the articles it links to has a free
    photograph as its main picture. No model is involved: the words are Wikipedia's."""
    before = dict(ai_used)
    mo, d = day_from(text)
    date = f"{d} {MONTH_NAMES[mo - 1]}"
    print(f"on this day: {date}")
    feed = get_json(f"https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/selected/{mo:02d}/{d:02d}").get("selected") or []
    pid, ep, work = new_video(con, f"onthisday:{date}")
    items = []
    for e in feed[::3] + feed[1::3] + feed[2::3]:  # the list runs newest to oldest: take from across it, not just the top
        words = re.sub(r"\s*\([^()]*(pictured|depicted|shown)[^()]*\)", "", e.get("text") or "").strip()
        if not isinstance(e.get("year"), int) or GRIM.search(words) or not 6 <= len(words.split()) <= 30:
            continue
        got = next((g for g in (lead_picture(p["title"]) for p in (e.get("pages") or [])[::-1][:4]) if g), None)
        if not got:
            continue
        year = f"{-e['year']} BC" if e["year"] < 0 else str(e["year"])
        items.append(dict(year=e["year"], name=year, text=words, image=download_image(got[2], work)))
        print(f"  {year}: {words[:90]}")
        if len(items) == count:
            break
    if len(items) < 2:
        raise SystemExit(f"Wikipedia's list for {date} has fewer than two short events with a free photograph")
    items.sort(key=lambda i: i["year"])
    m = dict(ep=ep, pageid=pid, origin="onthisday", article_title=f"On this day: {date}", description="",
             title=f"What Happened on {MONTH_NAMES[mo - 1]} {d}? On This Day in History", items=items, script="", images=[],
             tags=["onthisday", "history", MONTH_NAMES[mo - 1].lower()], url=f"https://en.wikipedia.org/wiki/{MONTH_NAMES[mo - 1]}_{d}",
             day=[mo, d], credit=f"Events from Wikipedia's \"On this day\" for {date}, CC BY-SA 4.0: https://en.wikipedia.org/wiki/{MONTH_NAMES[mo - 1]}_{d}")
    return make_format(con, m, date, before)


def onthisday_steps(m):
    mo, d = m["day"]
    return ([("intro", None, f"On this day, {MONTH_NAMES[mo - 1]} {number_words(d, ordinal=True)}.", 0.2, None)]
            + [("event", k, f"In {i['name']}, {i['text']}", 0.5, None) for k, i in enumerate(m["items"])]
            + [("outro", None, BRAND["outro_line"], 0.3, None)])


def render_list(m, scenes, audio_path, duration, out_path, frame_at, label, cards_text, big, head=False):
    """A title card, then one screen per item: a line of small print (label), something large (big(k): a year, a
    rank), a picture that drifts closer, and the words (with the item's name as a heading when head is set).
    cards_text: (large line, accent line) for the title card; the accent line is repeated on the closing card."""
    from PIL import Image, ImageDraw
    ensure_fonts()
    items, cards, pics = m["items"], {}, {}
    bw, bh, by = PW, 760, 540  # the picture's box
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, bw, bh), 36, fill=255)

    def card(kind, k):
        if (kind, k) not in cards:
            im, d = backdrop()
            if kind == "intro":
                y = text_lines(d, cards_text[0], "display", 190, 600, 2, C["text"], centre=True)
                text_lines(d, cards_text[1], "display", 120, y + 30, 3, C["accent"], centre=True)
            elif kind == "outro":
                y = text_lines(d, cards_text[1], "display", 120, 640, 3, C["accent"], centre=True)
                text_lines(d, BRAND["outro_line"], "body", 56, y + 60, 3, C["text"], centre=True, weight="Medium")
            else:
                text_lines(d, label, "display", 54, 205, 1, C["accent"])
                d.text((60, 268), big(k), font=font("display", 200), fill=C["text"])
                y = by + bh + 44
                if head:
                    y = text_lines(d, items[k]["name"].upper(), "display", 84, y, 2, C["accent"]) + 14
                text_lines(d, items[k]["text"], "body", 56, y, 7 if head else 8, C["text"], weight="Medium")
            cards[kind, k] = im
        return cards[kind, k]

    def draw(s, t):
        im, k = card(s["kind"], s["q"]).copy(), s["q"]
        if k is not None:
            if k not in pics:
                pics[k] = cover(items[k]["image"]["path"], bw, bh, 1.12)
            z = 1.1 - 0.1 * (t - s["start"]) / max(0.1, s["end"] - s["start"])  # drifts closer through the scene
            w, h, p = bw * z, bh * z, pics[k]
            box = ((p.width - w) / 2, (p.height - h) / 2, (p.width + w) / 2, (p.height + h) / 2)
            im.paste(p.resize((bw, bh), Image.BILINEAR, box=box), (PX, by), mask)
        return im

    render_scenes(scenes, draw, audio_path, duration, out_path, frame_at)


def render_onthisday(m, scenes, audio_path, duration, out_path, frame_at=None):
    date = f"{m['day'][1]} {MONTH_NAMES[m['day'][0] - 1]}".upper()
    render_list(m, scenes, audio_path, duration, out_path, frame_at, f"ON THIS DAY · {date}", ("ON THIS DAY", date),
                lambda k: m["items"][k]["name"])


# ---------- top countdowns: ranked things or facts, each with a picture, number 1 last ----------
TOP_PROMPT = """Write a countdown for a YouTube Short: "Top {n}: {topic}".
Use ONLY the notes below. Pick the {more} best entries. Every entry is a different one of the things the topic names
(for "tallest mountains": seven different mountains, never the same one twice, never a mountain range or an event). If the topic is a ranking (tallest, fastest, oldest, largest),
order them by the figures in the notes, the top one first; otherwise put the most surprising first.
For each entry give:
- "name": what it is, in at most 5 words
- "wikipedia": the exact title of the English Wikipedia article about that thing (for its picture)
- "fact": ONE sentence of at most 22 words a narrator will read, with the figure that earns its place
- "quote": the sentence from the notes that proves the fact, copied word for word; every number in the fact must be in it
Notes:
{text}
Reply as JSON: {{"items": [{{"name": "...", "wikipedia": "...", "fact": "...", "quote": "..."}}]}} with the top one first"""


def make_top(con, text="", count=5):
    """A countdown on a topic ("tallest mountains", "facts about octopuses"): research it, let the writer's model
    choose and order entries from the notes, and keep those whose proof is really in the notes, covers every number
    the fact states, and that have a free picture (the thing's Wikipedia picture, else Wikimedia Commons).
    The order is the model's reading of the notes; nothing in code re-ranks it."""
    before, topic = dict(ai_used), re.sub(r"^top\s*\d*\s*", "", text.strip(), flags=re.I) or random_topic()
    print(f"countdown: {topic}\n  researching…")
    a = research(f"What are the top {count + 3} {topic}, in order, with the figure for each?")
    r = llm(TOP_PROMPT.format(n=count, more=count + 3, topic=topic, text=a["text"][:12000]), 0.5)
    pid, ep, work = new_video(con, f"top:{topic}")
    nums = lambda x: set(re.findall(r"\d+(?:\.\d+)?", str(x).replace(",", "")))
    items, why = [], {"proof not in the research": 0, "a number the proof doesn't have": 0, "no free picture": 0}
    for e in r.get("items") or []:
        if len(items) == count or not isinstance(e, dict):
            continue
        name, fact, quote = (str(e.get(k) or "").strip() for k in ("name", "fact", "quote"))
        if not name or not 4 <= len(fact.split()) <= 28 or not in_text(quote, a["text"]):
            why["proof not in the research"] += 1
        elif nums(fact) - nums(quote):
            why["a number the proof doesn't have"] += 1
        else:
            got = lead_picture(str(e.get("wikipedia") or name))
            img = got[2] if got else next(iter(src_commons(name, 3)), None)
            if img and any(img["url"] == i["image"]["url"] for i in items):  # the same thing again under another name
                continue
            if not img:
                why["no free picture"] += 1
                continue
            items.append(dict(name=name[:60], text=fact, quote=quote, image=download_image(img, work)))
            print(f"  {len(items)}. {name}: {fact[:80]}")
    print(f"  {len(items)} kept" + "".join(f"; {n} dropped ({k})" for k, n in why.items() if n))
    if len(items) < 3:
        raise SystemExit(f"the research on \"{topic}\" didn't give three entries that could be proven and pictured; try another topic")
    m = dict(ep=ep, pageid=pid, origin="top", article_title=topic, description="", title=f"Top {len(items)} {topic.title()}"[:90],
             items=items[::-1], script="", images=[],  # saved in playing order: number 1 last
             tags=[w for w in re.findall(r"[a-z0-9]+", topic.lower()) if len(w) > 2 and w not in ("the", "and")][:5],
             url=a["url"], credit=a["credit"], source_label=a["source_label"])
    return make_format(con, m, topic, before)


def top_steps(m):
    n = len(m["items"])
    m["title"] = re.sub(r"^Top \d+ ", f"Top {n} ", m["title"])  # the title follows the number of entries
    return ([("intro", None, f"Top {number_words(n)} {m['article_title']}.", 0.3, None)]
            + [("item", k, f"Number {number_words(n - k)}. {i['name']}. {i['text']}", 0.5, "chime") for k, i in enumerate(m["items"])]
            + [("outro", None, BRAND["outro_line"], 0.3, None)])


def render_top(m, scenes, audio_path, duration, out_path, frame_at=None):
    n = len(m["items"])
    render_list(m, scenes, audio_path, duration, out_path, frame_at, m["title"].upper(), (f"TOP {n}", m["article_title"].upper()),
                lambda k: f"#{n - k}", head=True)


# ---------- word of the day: a word, what it means, where it comes from, and how it is used ----------
WIKTIONARY = "https://en.wiktionary.org/w/api.php"
WORD_PROMPT = """Here is the English Wiktionary entry for the word "{word}":
{entry}

Write three short sentences for a "word of the day" video, in plain English a 14-year-old follows.
- "meaning": what the word means (its first, main sense), starting with the word itself. Only what the entry says.
- "origin": where the word comes from, using ONLY the entry's Etymology; name the languages or people it names.
  Empty if the entry has no etymology.
- "example": a natural everyday sentence that uses the word correctly.
Each at most 24 words, written to be read aloud: no brackets, hyphenated word parts or symbols.
Reply as JSON: {{"meaning": "...", "origin": "...", "example": "..."}}"""


def make_word(con, text="", count=3):
    """A word-of-the-day video: the word typed, else Wiktionary's word of the day. The writer's model puts the entry's
    meaning and etymology into plain sentences and writes an example; an origin that names a language, person or year
    the entry doesn't is dropped rather than shown."""
    import html
    before, word = dict(ai_used), text.strip()
    if not word:
        feed = fetch(WIKTIONARY, {"action": "featuredfeed", "feed": "wotd", "feedformat": "atom"}).decode()
        found = re.findall(r'id="WOTD-rss-title"[^>]*>(.*?)<', html.unescape(feed))
        if not found:
            raise SystemExit("couldn't read Wiktionary's word of the day; type a word instead")
        word = html.unescape(found[-1])
    print(f"word of the day: {word}")
    p = get_json(WIKTIONARY, {"action": "query", "format": "json", "formatversion": 2, "prop": "extracts", "explaintext": 1,
                              "redirects": 1, "titles": word})["query"]["pages"][0]
    english = re.search(r"== English ==\n(.*?)(?=\n== [^=]|\Z)", p.get("extract") or "", re.S)
    if not english:
        raise SystemExit(f"Wiktionary has no English entry for \"{word}\"")
    entry = re.split(r"\n=+ (Translations|Anagrams|References|Further reading|See also|Derived terms|Related terms) =+", english.group(1))[0].strip()
    pos = re.search(r"=== (Noun|Verb|Adjective|Adverb|Interjection|Proper noun|Phrase|Idiom|Proverb|Preposition|Conjunction|Pronoun) ===", english.group(1))
    r = llm(WORD_PROMPT.format(word=word, entry=entry[:5000]), 0.4)
    meaning, origin, example = (re.sub(r"\s+", " ", str(r.get(k) or "")).strip() for k in ("meaning", "origin", "example"))
    if len(meaning.split()) < 4:
        raise RuntimeError(f"couldn't write a meaning for \"{word}\" from its entry")
    low = entry.lower()
    named = [w for w in re.findall(r"(?<=[a-z,;] )[A-Z][a-z]+|\d{3,4}", origin) if w.lower() not in low]  # names and years not in the entry
    if named or "Etymology" not in entry:
        print("  no origin shown" + (f": it named {', '.join(named)}, which the entry doesn't" if named else ": the entry has no etymology"))
        origin = ""
    pid, ep, _ = new_video(con, f"word:{word}")
    items = [dict(name=n, text=t) for n, t in (("MEANING", meaning), ("ORIGIN", origin), ("EXAMPLE", example)) if t]
    url = "https://en.wiktionary.org/wiki/" + urllib.parse.quote(p["title"].replace(" ", "_"))
    m = dict(ep=ep, pageid=pid, origin="word", article_title=p["title"], description=pos.group(1).lower() if pos else "",
             title=f"Word of the Day: {p['title'].capitalize()}"[:90], items=items, script="", images=[],
             tags=["wordoftheday", "vocabulary", "english"], url=url, credit=f"Meaning and origin from Wiktionary, CC BY-SA 4.0: {url}")
    return make_format(con, m, p["title"], before)


def word_steps(m):
    return ([("intro", None, f"Today's word: {m['article_title']}.", 0.4, None)]
            + [("part", k, i["text"], 0.5, None) for k, i in enumerate(m["items"])]
            + [("outro", None, BRAND["outro_line"], 0.3, None)])


def render_word(m, scenes, audio_path, duration, out_path, frame_at=None):
    """The word large at the top; its meaning, origin and example appear one after another as they are read, the one
    being read in full colour and the earlier ones dimmed."""
    ensure_fonts()
    cards = {}

    def card(kind, k):
        shown = -1 if kind == "intro" else len(m["items"]) if kind == "outro" else k
        if shown not in cards:
            im, d = backdrop()
            d.text((60, 205), "WORD OF THE DAY", font=font("display", 54), fill=C["accent"])
            y = text_lines(d, m["article_title"].upper(), "display", 190, 290, 1, C["text"])
            if m.get("description"):
                y = text_lines(d, m["description"], "body", 46, y + 6, 1, C["muted"], weight="Medium")
            y += 70
            for j, i in enumerate(m["items"][:shown + 1]):
                d.text((60, y), i["name"].upper(), font=font("display", 50), fill=C["accent"] if j == shown else C["muted"])
                y = text_lines(d, i["text"], "body", 56, y + 84, 6, C["text"] if j == shown else C["muted"], weight="Medium") + 56
            cards[shown] = im
        return cards[shown]

    render_scenes(scenes, lambda s, t: card(s["kind"], s["q"]).copy(), audio_path, duration, out_path, frame_at)


# video_type / origin -> (make it from the Make box's text, where its parts are in meta, its scenes, its renderer, fewest parts)
FORMATS = {"quiz": (make_quiz, "quiz", quiz_steps, render_quiz, 3),
           "guess": (make_guess, "items", guess_steps, render_guess, 3),
           "onthisday": (make_onthisday, "items", onthisday_steps, render_onthisday, 2),
           "top": (make_top, "items", top_steps, render_top, 3),
           "word": (make_word, "items", word_steps, render_word, 2)}


def copy_to(con, ep, to):
    """Copy a video to another channel: same script and pictures (or clips), made again there with that channel's
    brand, voice and closing line. The work is done by `copyfrom` running as that channel."""
    import subprocess
    row = con.execute("select pageid from pages where ep=?", (ep,)).fetchone()
    if not row or not (WORK / str(row["pageid"]) / "meta.json").exists():
        raise SystemExit(f"episode {ep} has nothing saved to copy")
    if to == WS_NAME or (to and not (ROOT / "workspaces" / to / "brand.json").exists()):
        raise SystemExit("choose another channel to copy it to")
    r = subprocess.run([sys.executable, "-W", "ignore", "-u", str(ROOT / "rw.py"), "copyfrom", "--channel", WS_NAME, "--pageid", str(row["pageid"])],
                       env=os.environ | {"RW_WORKSPACE": to})
    if r.returncode:
        raise SystemExit("the copy didn't finish; see above")


def copy_from(con, channel, pageid):
    """This channel's half of copy_to: take the other channel's script and pictures and make the video as ours."""
    import shutil
    theirs = (ROOT / "workspaces" / channel if channel else ROOT)
    src, mine = theirs / "work" / str(pageid), WORK / str(pageid)
    if not (src / "meta.json").exists():
        raise SystemExit("that video isn't there any more")
    if mine.exists() or con.execute("select 1 from pages where pageid=? and ep is not null", (pageid,)).fetchone():
        raise SystemExit(f"{BRAND['channel']} already has this video")
    their = sqlite3.connect(f"file:{theirs / 'data' / 'shorts.db'}?mode=ro", uri=True)
    their.row_factory = sqlite3.Row
    row = their.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    m = json.loads((src / "meta.json").read_text())
    mine.mkdir(parents=True)
    for f in src.iterdir():  # pictures, clips, this video's own music and sounds; not their voice or rendered pieces
        if f.is_file() and not re.match(r"meta\.json|voice|seg-|novoice-|badge|list\.txt", f.name):
            shutil.copy(f, mine / f.name)
    for i in m.get("images") or []:
        i["path"] = str(mine / Path(i["path"]).name)
    for i in m.get("items") or []:
        if i.get("image"):
            i["image"]["path"] = str(mine / Path(i["image"]["path"]).name)
    ep = con.execute("select coalesce(max(ep), 0) + 1 from pages").fetchone()[0]
    m["ep"] = ep
    for k in ("narration", "yt_description", "potential", "ai", "gemini"):
        m.pop(k, None)
    print(f"[{ep:03d}] {m['title']}  (copied from {json.loads((theirs / 'brand.json').read_text())['channel']})")
    upsert(con, pageid, title=row["title"], url=row["url"], source=row["source"], origin=row["origin"], ref=row["ref"],
           reason="copied from another channel", status="draft", ep=ep)
    try:
        if m.get("origin") == "clips":
            m["yt_description"] = ""
            (mine / "meta.json").write_text(json.dumps(m, indent=2))
            render_clips(con, m)
        elif m.get("origin") in FORMATS:
            produce_format(con, m)
        else:
            produce(con, m, video_out(ep, m["article_title"]))
            try:
                score_video(con, pageid)
            except Exception as e:
                print(f"  could not score: {e}")
    except BaseException as e:
        upsert(con, pageid, status="failed", reason=f"{type(e).__name__}: {e}"[:300])
        raise


def sources_id(key):
    import sources
    return sources.synthetic_id(key)


# ---------- automation (macOS launchd) ----------
SCHEDULE_DEFAULTS = {"enabled": False, "times": ["09:00"], "days": [0, 1, 2, 3, 4, 5, 6], "upload": True,
                     "publish": "private", "publish_time": "15:00", "fallback": "wikipedia", "fallback_arg": "",
                     "pick": "order"}  # pick: "order" = top of the Queue (ready videos), "newest" = the video this run made
LAUNCH_LABEL = "com.shortsmachine.auto" + (f".{WS_NAME}" if WS_NAME else "")  # one job per channel
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
AUTO_LOG = DB.parent / "auto.log"


def clips_channel():
    return BRAND.get("video_type") == "clips"  # Settings → Ideas & videos → What this channel makes


def random_topic():
    import random
    return random.choice(channel_topics())


def schedule_settings():
    return SCHEDULE_DEFAULTS | BRAND.get("schedule", {})


def notify(msg):
    import subprocess
    safe = msg.replace("\\", "").replace('"', "'")[:200]
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "{BRAND["channel"].replace(chr(34), "")[:40]}"'], capture_output=True)


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
                       "EnvironmentVariables": {"RW_WORKSPACE": WS_NAME, "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}}, f)
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
    turn = open(ROOT / "data" / "render.lock", "w")
    fcntl.flock(turn, fcntl.LOCK_EX)  # channels scheduled for the same minute take turns instead of rendering at once
    sch = schedule_settings()
    print(f"=== automated run {time.strftime('%Y-%m-%d %H:%M')}" + (" (dry run)" if dry_run else ""))
    try:
        row = con.execute(f"select pageid, title from pages where status='queued' {QUEUE_ORDER} limit 1").fetchone()
        when = publish_time_utc(sch["publish_time"]) if sch["publish"] == "at" else None
        plan = (f"make: {row['title']}" if row else f"queue empty: find 1 idea in {sch['fallback_arg']}" if sch["fallback_arg"]
                else f"queue empty: find 1 new idea ({BRAND.get('idea_source') or 'mix'} of {', '.join(channel_topics())})")
        ready = con.execute(f"select ep, title from pages where status='rendered' {UPLOAD_ORDER} limit 1").fetchone()
        which = ("the video it makes" if sch["pick"] == "newest" or not ready
                 else f"EP {ready['ep']:03d} {ready['title']} (top of the Queue)")
        plan += f"; then {'upload ' + which + (', public at ' + when + ' UTC' if when else ', private') if sch['upload'] else 'no upload'}"
        print(plan)
        if dry_run:
            return
        if clips_channel():
            pid = make_clips(con, sch["fallback_arg"] or random_topic(), draft=False)  # nobody is there to pick clips
        elif BRAND.get("video_type") in FORMATS:
            pid = FORMATS[BRAND["video_type"]][0](con, sch["fallback_arg"])
        elif row:
            pid = row["pageid"]
        elif not sch["fallback_arg"]:  # nothing set for empty runs: your topics and what's trending in them
            pid = next_ideas(con, 1)[0]
        elif sch["fallback"] == "wikipedia":  # a topic you typed
            pid = brainstorm(con, sch["fallback_arg"], "any", 1)[0]
        else:
            pid = discover_source(con, sch["fallback"], 1, sch["fallback_arg"] or None)[0]
        if not clips_channel() and BRAND.get("video_type") not in FORMATS:
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
    if row["origin"] in FORMATS:
        return produce_format(con, json.loads((WORK / str(row["pageid"]) / "meta.json").read_text()))
    if row["origin"] == "clips":
        return render_clips(con, json.loads((WORK / str(row["pageid"]) / "meta.json").read_text()))
    m = json.loads((WORK / str(row["pageid"]) / "meta.json").read_text())
    print(f"[{ep:03d}] re-rendering {m['article_title']}")
    m.pop("potential", None)
    if narration == "recording":
        if not (WORK / str(row["pageid"]) / "voice_recorded.wav").exists():
            raise SystemExit("no recording for this episode yet: record one in the studio first")
        m["narration"] = "recording"
    elif narration == "ai":
        m["narration"] = "kokoro"
    before = dict(ai_used)
    if repick_images:
        print("  picking images from the script…")
        m["images"] = pick_visuals(m["article_title"], m["script"], WORK / str(row["pageid"]), own=[],
                                   topics=m.get("topics", ())) or m["images"]
    produce(con, m, Path(row["video_path"]) if row["video_path"] else video_out(ep, m["article_title"]))
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
    tok = WS_SECRETS / "token.json"
    creds = Credentials.from_authorized_user_file(tok) if tok.exists() else None
    if creds and not creds.has_scopes([scope]):
        print("  YouTube needs an extra permission; a browser window will ask you to allow it")
        creds = None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        cs = SECRETS / "client_secret.json"
        if not cs.exists():
            raise SystemExit("YouTube isn't set up: in the studio, Settings → YouTube, add your client file and click Connect")
        had = json.loads(tok.read_text()).get("scopes", []) if tok.exists() else []
        creds = InstalledAppFlow.from_client_secrets_file(cs, sorted({*had, scope})).run_local_server(port=0)
    tok.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def youtube_status():
    """'none' (no client file), 'client' (ready to connect) or 'connected'."""
    return ("connected" if (WS_SECRETS / "token.json").exists() else "client") if (SECRETS / "client_secret.json").exists() else "none"


def save_youtube_client(text):
    """Check and save the OAuth client JSON downloaded from Google Cloud. A new client signs the old one out."""
    try:
        c = json.loads(text)
    except ValueError:
        raise ValueError("that isn't the JSON file from Google Cloud") from None
    if "web" in c:
        raise ValueError("this is a 'Web application' client; create one of type 'Desktop app' instead")
    inst = c.get("installed") or {}
    if not (str(inst.get("client_id", "")).endswith(".apps.googleusercontent.com") and inst.get("client_secret")):
        raise ValueError("that isn't an OAuth client file: in Google Cloud, create a 'Desktop app' client and download its JSON")
    SECRETS.mkdir(exist_ok=True)
    cs = SECRETS / "client_secret.json"
    if cs.exists() and json.loads(cs.read_text()).get("installed", {}).get("client_id") != inst["client_id"]:
        youtube_disconnect()
    cs.write_text(json.dumps(c))
    cs.chmod(0o600)


def youtube_disconnect():
    """Revoke the app's access at Google and forget the sign-in."""
    tok = WS_SECRETS / "token.json"
    if tok.exists():
        t = json.loads(tok.read_text())
        try:
            urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/revoke",
                                   urllib.parse.urlencode({"token": t.get("refresh_token") or t.get("token")}).encode()), timeout=20)
        except OSError:
            pass  # already revoked or offline: forgetting it locally is what matters
        tok.unlink()


def delete_local(con, pageid, reason):
    """Delete an episode's video and work files; the page stays in the log as skipped."""
    import shutil
    row = con.execute("select video_path from pages where pageid=?", (pageid,)).fetchone()
    media_forget(pageid)
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


# ---------- publishing: YouTube, Facebook, Instagram, TikTok ----------
GRAPH = "https://graph.facebook.com/v27.0/"
META_FILE = WS_SECRETS / "meta.json"  # {token, page_id, page_name, ig_id, ig_user}: a Page token doesn't expire
TIKTOK_FILE = WS_SECRETS / "tiktok.json"  # {client_key, client_secret, access_token, expires, refresh_token, user}
TIKTOK_API = "https://open.tiktokapis.com/v2/"
TIKTOK_REDIRECT = "http://127.0.0.1:8766/callback/"  # register exactly this in the TikTok app
PLATFORMS = {"youtube": "YouTube", "facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok"}


def secret(path, data=None):
    """Read (or write, owner-only) a JSON file in secrets/."""
    if data is None:
        return json.loads(path.read_text()) if path.exists() else {}
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def http_json(req, timeout=60):
    """urlopen -> JSON; API errors become RuntimeError with the service's own message (never the request)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            d = json.load(e)
            err = d.get("error")
            msg = (err.get("message") if isinstance(err, dict) else d.get("error_description") or err) or f"HTTP {e.code}"
        except ValueError:
            msg = f"HTTP {e.code}"
        raise RuntimeError(str(msg)[:200]) from None


def graph(path, token, data=None):
    """Meta Graph API: GET without data, POST (form) with it."""
    if data is None:
        return http_json(GRAPH + path + ("&" if "?" in path else "?") + urllib.parse.urlencode({"access_token": token}))
    return http_json(urllib.request.Request(GRAPH + path, urllib.parse.urlencode({**data, "access_token": token}).encode()))


def rupload(url, token, path):
    """Meta's upload host: the whole file in one request."""
    raw = Path(path).read_bytes()
    return http_json(urllib.request.Request(url, raw, {"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(raw))}), 600)


def save_meta(token):
    """Accept a Page token or a (long-lived) user token; find the Page and the Instagram account linked to it."""
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{40,600}", token):
        raise ValueError("that doesn't look like a Meta access token")
    pages = []
    try:
        pages = graph("me/accounts?fields=id,name,access_token,instagram_business_account{id,username}", token).get("data", [])
    except RuntimeError:
        pass  # a Page token has no /me/accounts
    if pages:  # user token: its Page tokens don't expire when the user token is long-lived
        pg = next((x for x in pages if x.get("instagram_business_account")), pages[0])  # ponytail: first Page; add a picker if people run several
        token = pg["access_token"]
    else:
        try:
            pg = graph("me?fields=id,name,instagram_business_account{id,username}", token)
        except RuntimeError as e:
            raise ValueError(f"Meta rejected the token: {e}") from None
        if "instagram_business_account" not in pg and "category" not in graph(f"{pg['id']}?fields=category", token):
            raise ValueError("that token isn't for a Facebook Page: use Get Page Access Token, or a user token with Page permissions")
    ig = pg.get("instagram_business_account") or {}
    secret(META_FILE, {"token": token, "page_id": pg["id"], "page_name": pg.get("name", ""), "ig_id": ig.get("id", ""), "ig_user": ig.get("username", "")})


def post_facebook(path, m):
    c = secret(META_FILE)
    if video_seconds(path) > 90:
        raise RuntimeError("Facebook Reels must be 90 seconds or shorter")
    v = graph(f"{c['page_id']}/video_reels", c["token"], {"upload_phase": "start"})["video_id"]
    rupload(f"https://rupload.facebook.com/video-upload/v27.0/{v}", c["token"], path)
    graph(f"{c['page_id']}/video_reels", c["token"], {"upload_phase": "finish", "video_id": v, "video_state": "PUBLISHED",
                                                       "description": caption(m)})
    return {"id": v, "url": f"https://www.facebook.com/reel/{v}"}


def post_instagram(path, m):
    c = secret(META_FILE)
    cid = graph(f"{c['ig_id']}/media", c["token"], {"media_type": "REELS", "upload_type": "resumable", "caption": caption(m)})["id"]
    rupload(f"https://rupload.facebook.com/ig-api-upload/v27.0/{cid}", c["token"], path)
    for _ in range(30):  # Instagram processes the video before it can be published (Meta: poll for up to 5 minutes)
        st = graph(f"{cid}?fields=status_code", c["token"]).get("status_code")
        if st == "FINISHED":
            break
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video ({st})")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram is still processing the video; publishing again later retries it")
    mid = graph(f"{c['ig_id']}/media_publish", c["token"], {"creation_id": cid})["id"]
    return {"id": mid, "url": graph(f"{mid}?fields=permalink", c["token"]).get("permalink", "")}


def save_tiktok_app(key, secret_):
    key, secret_ = key.strip(), secret_.strip()
    if not (re.fullmatch(r"\w{8,64}", key) and re.fullmatch(r"\w{16,128}", secret_)):
        raise ValueError("paste the Client key and Client secret from your TikTok app")
    if secret(TIKTOK_FILE).get("client_key") not in (None, key):
        tiktok_disconnect()
    secret(TIKTOK_FILE, {"client_key": key, "client_secret": secret_})


def tiktok_token(c, **params):
    t = http_json(urllib.request.Request(TIKTOK_API + "oauth/token/", urllib.parse.urlencode(
        {"client_key": c["client_key"], "client_secret": c["client_secret"], **params}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"}))
    if "access_token" not in t:
        raise RuntimeError(t.get("error_description") or t.get("error") or "TikTok refused the sign-in")
    c.update(access_token=t["access_token"], refresh_token=t["refresh_token"], expires=time.time() + t["expires_in"] - 60)
    secret(TIKTOK_FILE, c)  # refresh tokens rotate: always keep the newest
    return c


def tiktok_connect():
    """Sign in to TikTok in the browser (OAuth with PKCE) and keep the tokens in secrets/."""
    import hashlib, secrets, webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    c = secret(TIKTOK_FILE)
    if not c.get("client_key"):
        raise SystemExit("TikTok isn't set up: in the studio, Connectors → TikTok, add your app's client key and secret")
    verifier, state, got = secrets.token_urlsafe(48), secrets.token_urlsafe(16), {}

    class Back(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path == "/callback/":
                got.update({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the studio.")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 8766), Back)
    srv.timeout = 5
    webbrowser.open("https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": c["client_key"], "response_type": "code", "scope": "user.info.basic,video.upload",
        "redirect_uri": TIKTOK_REDIRECT, "state": state, "code_challenge_method": "S256",
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest()}))  # TikTok desktop wants hex, not base64url
    print("  a browser window asks you to allow access on TikTok (waiting up to 5 minutes)")
    end = time.time() + 300
    while "state" not in got and time.time() < end:
        srv.handle_request()
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise SystemExit("TikTok sign-in didn't finish" + (f": {got.get('error_description') or got['error']}" if got.get("error") else ""))
    c = tiktok_token(c, grant_type="authorization_code", code=got["code"], redirect_uri=TIKTOK_REDIRECT, code_verifier=verifier)
    try:
        c["user"] = tiktok_api("user/info/?fields=display_name", None)["user"].get("display_name", "")
        secret(TIKTOK_FILE, c)
    except (RuntimeError, KeyError):
        pass
    print(f"  TikTok connected{' as ' + c['user'] if c.get('user') else ''}")


def tiktok_api(path, body):
    c = secret(TIKTOK_FILE)
    if time.time() > c.get("expires", 0):
        c = tiktok_token(c, grant_type="refresh_token", refresh_token=c["refresh_token"])
    req = urllib.request.Request(TIKTOK_API + path, None if body is None else json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {c['access_token']}", "Content-Type": "application/json; charset=UTF-8"})
    d = http_json(req)
    if (d.get("error") or {}).get("code", "ok") != "ok":
        raise RuntimeError(d["error"].get("message") or d["error"]["code"])
    return d.get("data") or {}


def post_tiktok(path, m):
    """Send the video to the creator's TikTok inbox; they add the caption and post it in the app.
    (Posting directly needs TikTok to audit the app; until then it only allows private posts on a private account.)"""
    size = Path(path).stat().st_size
    if size > 64e6:
        raise RuntimeError("video over 64 MB: TikTok needs it in chunks")  # ponytail: Shorts are ~20 MB; chunk if that changes
    d = tiktok_api("post/publish/inbox/video/init/", {"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                                                     "chunk_size": size, "total_chunk_count": 1}})
    req = urllib.request.Request(d["upload_url"], Path(path).read_bytes(), {"Content-Type": "video/mp4",
                                 "Content-Range": f"bytes 0-{size - 1}/{size}"}, method="PUT")
    with urllib.request.urlopen(req, timeout=600):
        pass
    return {"id": d["publish_id"], "url": "", "note": "in your TikTok inbox: open TikTok to post it"}


def tiktok_disconnect():
    c = secret(TIKTOK_FILE)
    if c.get("access_token"):
        try:
            urllib.request.urlopen(urllib.request.Request(TIKTOK_API + "oauth/revoke/", urllib.parse.urlencode(
                {"client_key": c["client_key"], "client_secret": c["client_secret"], "token": c["access_token"]}).encode()), timeout=20)
        except OSError:
            pass
    secret(TIKTOK_FILE, {k: c[k] for k in ("client_key", "client_secret") if k in c})


def publish_status():
    """What each platform needs: 'none' (set up), 'client' (connect) or 'connected'."""
    meta, tt = secret(META_FILE), secret(TIKTOK_FILE)
    return {"youtube": youtube_status(), "facebook": "connected" if meta.get("page_id") else "none",
            "instagram": "connected" if meta.get("ig_id") else "none",
            "tiktok": "connected" if tt.get("refresh_token") else "client" if tt.get("client_key") else "none",
            "meta": {k: meta.get(k, "") for k in ("page_name", "ig_user")}, "tiktok_user": tt.get("user", "")}


def caption(m):
    return f"{m['title']}\n\n{m['yt_description']}"[:2200]


def video_seconds(path):
    """Duration of any audio or video file, as ffmpeg reports it (0 if it can't tell)."""
    import subprocess, imageio_ffmpeg
    err = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def upload_youtube(row, m, publish_at):
    from googleapiclient.http import MediaFileUpload
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
    return resp["id"], f"https://youtube.com/shorts/{resp['id']} ({status['privacyStatus']})"


def upload(con, pageid, publish_at=None):
    """Publish to every platform ticked in Connectors. Each that succeeds is remembered, so a retry only redoes the
    ones that failed; the video counts as uploaded when all are done. publish_at schedules YouTube only."""
    row = con.execute("select * from pages where pageid=?", (pageid,)).fetchone()
    if not row or row["status"] != "rendered":
        raise SystemExit(f"page {pageid} is not in 'rendered' state")
    m = json.loads((WORK / str(pageid) / "meta.json").read_text())
    posts = json.loads(row["posts"] or "{}")
    targets = [p for p in BRAND.get("publish_to") or ["youtube"] if p in PLATFORMS and p not in posts
               and not (p == "youtube" and row["video_id"])]
    failed = []
    for p in targets:
        try:
            if p == "youtube":
                vid, where = upload_youtube(row, m, publish_at)
                upsert(con, pageid, video_id=vid)
            else:
                posts[p] = {"id": "", "url": "", **{"youtube": None, "facebook": post_facebook, "instagram": post_instagram,
                                                     "tiktok": post_tiktok}[p](row["video_path"], m)}
                upsert(con, pageid, posts=json.dumps(posts))
                where = posts[p].get("note") or posts[p]["url"]
            print(f"  {PLATFORMS[p]}: {where}")
        except (Exception, SystemExit) as e:  # keep going with the other platforms
            failed.append(p)
            print(f"  {PLATFORMS[p]} failed: {e}")
    if failed:
        print(f"  not marked uploaded yet; publishing again retries {', '.join(PLATFORMS[p] for p in failed)}")
    else:
        upsert(con, pageid, status="uploaded")


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
            print(f"  publishing failed: {e}")


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
    assert audio_mix(None, 0.1, []) == ["-map", "0:v", "-map", "1:a"]
    mx = audio_mix(Path("m.mp3"), 0.1, [(Path("a.wav"), 2.5)])
    assert mx[-5] == "[2:a]volume=0.1[m];[3:a]adelay=2500:all=1,volume=0.8[s0];[1:a][m][s0]amix=inputs=3:duration=first:normalize=0[a]", mx
    assert audio_mix(None, 0.1, [(Path("a.wav"), 1)])[3].startswith("[2:a]adelay=1000"), "sfx is input 2 without music"
    assert seconds("1:20") == 80 and seconds("01:02:03") == 3723 and seconds("95") == 95
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
    for k in PROMPTS:  # every editable prompt splits into instructions + a fixed part that still asks for JSON
        head, tail = prompt_parts(k)
        assert head and "Reply as JSON" in tail and "{" not in head.replace("{answer}", "").replace("{topics}", ""), k
    saved, BRAND["prompts"] = BRAND.get("prompts"), {"script": "Be funny about {title} }{"}
    assert prompt_for("script", title="T", text="X", answer="").startswith("Be funny about T }{\n\nArticle title: T")
    BRAND["prompts"] = saved
    assert day_from("25 December") == day_from("December 25") == day_from("12-25") == (12, 25)
    heat = [dict(start_time=k * 10.0, value=v) for k, v in enumerate([1, .9, .8, .7, .6, .2, .9, .3] + [.1] * 92)]
    assert best_part(dict(duration=1000, heatmap=heat)) == 60 and best_part(dict(duration=900)) == 300  # the peak after the opening; else a third in
    # quiz: a question is kept only if its proof is in the notes and names the right option alone
    global llm
    notes = ("The Sun holds 99.86% of the mass of the Solar System. Mars is the fourth planet from the Sun. "
             "Jupiter is the largest planet, ahead of Saturn. Neptune was first seen in 1846 by Johann Galle.")
    qs = [("How much of the mass is the Sun?", ["99.86%", "50%", "75%"], "99.86%", "The Sun holds 99.86% of the mass of the Solar System."),
          ("Which planet is fourth?", ["Venus", "Mars", "Earth"], "Mars", "Mars is the fourth planet from the Sun."),
          ("When was Neptune first seen?", ["1846", "1781", "1930"], "1846", "Neptune was first seen in 1846 by Johann Galle."),
          ("Which planet has the most moons?", ["Saturn", "Venus", "Mars"], "Saturn", "Saturn has more moons than any other planet."),  # invented proof
          ("Which planet is largest?", ["Jupiter", "Saturn", "Earth"], "Jupiter", "Jupiter is the largest planet, ahead of Saturn.")]  # names a wrong option too
    real, llm = llm, lambda prompt, *a, **k: (
        {"title": "T", "questions": [dict(question=q, options=o, answer=an, quote=pr) for q, o, an, pr in qs]} if "Notes:" in prompt
        else {"answer": next(an for q, o, an, pr in qs if pr in prompt)})
    try:
        kept = quiz_questions("the solar system", notes, 5)[1]
    finally:
        llm = real
    assert [k["answer"] for k in kept] == ["99.86%", "Mars", "1846"], kept
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="make a video from anything: a question, topic, paragraph or link (none = next idea)")
    r.add_argument("input", nargs="?"); r.add_argument("--url", dest="input", help=argparse.SUPPRESS)
    r.add_argument("--force", action="store_true")
    r.add_argument("--auto", action="store_true", help="render right away even in manual mode")
    r.add_argument("--upload", action="store_true"); r.add_argument("--publish-at")
    a = sub.add_parser("add", help="save ideas: questions, topics, paragraphs or links")
    a.add_argument("items", nargs="+"); a.add_argument("--force", action="store_true")
    u = sub.add_parser("upload"); u.add_argument("--publish-at"); u.add_argument("--ep", type=int)
    lg = sub.add_parser("log"); lg.add_argument("n", nargs="?", type=int, default=30)
    sub.add_parser("selftest")
    sub.add_parser("preview")
    sub.add_parser("imagemodel", help="install the picture model that runs on this Mac")
    ct = sub.add_parser("copyto", help="copy a video to another channel, remade with that channel's brand and voice")
    ct.add_argument("--ep", type=int, required=True); ct.add_argument("--to", required=True, help='channel folder name ("" = the first channel)')
    cf = sub.add_parser("copyfrom", help=argparse.SUPPRESS); cf.add_argument("--channel", required=True); cf.add_argument("--pageid", type=int, required=True)
    pl = sub.add_parser("pull", help="download a local model into Ollama"); pl.add_argument("model")
    ca = sub.add_parser("clipadd", help="add one video (a link) to a clip countdown")
    ca.add_argument("url"); ca.add_argument("--ep", type=int, required=True); ca.add_argument("--part", help="1:20-1:45")
    cl = sub.add_parser("clips", help="a countdown of clips: words to search YouTube for, and/or links from any site")
    cl.add_argument("items", nargs="+"); cl.add_argument("--count", type=int, default=5, choices=range(2, 11))
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
    dc.add_argument("--source", choices=["ideas", "trending", "wikipedia", "link", "hackernews", "rss", "arxiv", "apod"], default="ideas",
                    help="ideas: your topics and what's trending in them (Settings); wikipedia: random pages")
    dc.add_argument("--feed", help="RSS/Atom feed URL (for --source rss)")
    dc.add_argument("--link", help="any link (for --source link): feed, site, subreddit, Wikipedia category/list, ...")
    dl = sub.add_parser("download", help="save videos, audio or images from almost any site into media/ (references, clips)")
    dl.add_argument("urls", nargs="+"); dl.add_argument("--section", help="only part of it, e.g. 1:20-1:45")
    dl.add_argument("--kind", choices=MEDIA_KINDS, default="video", help="video (default), audio only, or an image")
    cn = sub.add_parser("connect", help="sign in to YouTube or TikTok (opens the browser)")
    cn.add_argument("platform", nargs="?", default="youtube", choices=["youtube", "tiktok"])
    sc = sub.add_parser("score"); sc.add_argument("--all", action="store_true")
    wb = sub.add_parser("web"); wb.add_argument("--port", type=int, default=8765); wb.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    for d in (ROOT / "data", DB.parent, WORK, OUT, SECRETS, WS_SECRETS, WS / "brand"):
        d.mkdir(exist_ok=True)
    con = db()
    if args.cmd == "web":
        import web
        return web.serve(args.port, not args.no_open)
    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "connect" and args.platform == "tiktok":
        return tiktok_connect()
    if args.cmd == "connect":
        youtube(YT_UPLOAD)
        return print("YouTube connected. Uploads will go to the channel you chose.")
    if args.cmd == "download":
        failed = 0
        for url in args.urls:
            try:
                download(url, args.section, args.kind)
            except (SystemExit, Exception) as e:  # one bad link doesn't stop the others
                failed += 1
                print(f"! {url}: {e}")
        if failed:
            raise SystemExit(f"{failed} of {len(args.urls)} failed")
        return
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
    elif args.cmd == "discover" and args.source in ("ideas", "trending") and not args.topic:
        trending(con, args.count) if args.source == "trending" else next_ideas(con, args.count)
    elif args.cmd == "discover" and args.source not in ("wikipedia", "ideas", "trending"):
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
    elif args.cmd == "imagemodel":
        image_model_setup()
    elif args.cmd == "copyto":
        copy_to(con, args.ep, args.to)
    elif args.cmd == "copyfrom":
        copy_from(con, args.channel, args.pageid)
    elif args.cmd == "pull":
        pull_model(args.model)
    elif args.cmd == "clipadd":
        clip_add(con, args.ep, args.url, args.part)
    elif args.cmd == "clips" or (args.cmd == "run" and clips_channel()):
        make_clips(con, " ".join(args.items) if args.cmd == "clips" else args.input or random_topic(), getattr(args, "count", 5))
        if getattr(args, "upload", False):
            upload_pending(con, args.publish_at)
    elif args.cmd == "run" and BRAND.get("video_type") in FORMATS:
        FORMATS[BRAND["video_type"]][0](con, args.input or "")
        if getattr(args, "upload", False):
            upload_pending(con, args.publish_at)
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
                pageid = next_ideas(con)[0]
        make(con, pageid, auto=args.auto)
        if args.upload:
            upload_pending(con, args.publish_at)


if __name__ == "__main__":
    main()
