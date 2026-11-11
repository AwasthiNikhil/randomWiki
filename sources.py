"""Content sources beyond Wikipedia. Each source can discover candidates and fetch one into an Article:

  {"origin", "key", "title", "description", "text", "url", "credit", "source_label", "images", "popularity"}

key is a stable unique id ("hackernews:8863"); credit goes in the YouTube description; source_label is the
video footer; images are free-licensed picks only (never a web page's own images: they are copyrighted);
popularity (0-1, optional) feeds the potential score. Wikipedia itself stays on rw.py's optimised path.
"""
import hashlib, html, math, re, urllib.parse, urllib.robotparser
import xml.etree.ElementTree as ET

from rw import fetch, get_json

ORIGINS = ("web", "hackernews", "arxiv", "apod")  # + "wikipedia" (rw.py) and "rss" (discovery only, items are web pages)
MIN_TEXT = 500  # characters; less than this is not enough to write a fact-based short


def synthetic_id(key):
    """Stable integer id for non-Wikipedia items (DB primary key, work/ folder). Wikipedia ids are < 1e9."""
    return 10 ** 12 + int(hashlib.sha1(key.encode()).hexdigest()[:12], 16) % (9 * 10 ** 12)


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# ---------- which source does a link belong to ----------
def detect(url):
    """Link -> (origin, ref) where ref is what fetch() needs."""
    u = url.strip()
    if re.search(r"(^|//)([a-z]+\.)?wikipedia\.org/wiki/", u) or not re.match(r"https?://", u):
        return "wikipedia", u  # bare titles are Wikipedia titles
    if m := re.search(r"news\.ycombinator\.com/item\?id=(\d+)", u):
        return "hackernews", m[1]
    if m := re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", u):
        return "arxiv", m[1]
    if re.search(r"apod\.nasa\.gov/apod/ap\d{6}\.html|science\.nasa\.gov/image-article/apod-", u):
        return "apod", u
    return "web", u


# ---------- any web page ----------
BOILERPLATE = re.compile(r"^(contribute to|sign up|log in|subscribe|read more|click here)", re.I)


def clean_title(title, site):
    """'GitHub - owner/repo: blurb' -> 'owner/repo'; 'Story title | Site' -> 'Story title'."""
    parts = [x for x in re.split(r"\s+[|–—-]\s+", title.strip()) if x]
    keep = [x for x in parts if x.lower() not in (site.lower(), "github", "home")] or parts
    best = max(keep, key=len)
    return re.sub(r"^([\w.-]+/[\w.-]+):\s.*$", r"\1", best)  # GitHub "owner/repo: description"



def allowed_by_robots(url):
    p = urllib.parse.urlsplit(url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        rp.parse(fetch(f"{p.scheme}://{p.netloc}/robots.txt").decode("utf-8", "ignore").splitlines())
    except Exception:
        return True  # no robots.txt (or unreachable): allowed
    return rp.can_fetch("ShortsMachine", url)


def fetch_web(url, extra_credit=""):
    import trafilatura
    url = urllib.parse.urldefrag(url)[0]
    if not allowed_by_robots(url):
        raise SystemExit(f"robots.txt asks bots not to fetch {url}")
    doc = trafilatura.bare_extraction(fetch(url).decode("utf-8", "ignore"), url=url, with_metadata=True)
    d = doc if isinstance(doc, dict) else (doc.as_dict() if doc else {})
    text, title = (d.get("text") or "").strip(), (d.get("title") or url).strip()
    if len(text) < MIN_TEXT:
        raise SystemExit(f"not enough readable text at {url} ({len(text)} characters)")
    site = d.get("sitename") or urllib.parse.urlsplit(url).netloc.removeprefix("www.")
    title = clean_title(title, site)
    desc = "" if BOILERPLATE.match(d.get("description") or "") else (d.get("description") or "")[:160]
    return dict(origin="web", key=f"web:{url.rstrip('/')}", title=title, description=desc,
                text=text, url=url, credit=f'Based on "{title}" ({site}): {url}{extra_credit}',
                source_label=f"Source: {site}", images=[], popularity=None)


# ---------- RSS / Atom feeds (items are web pages) ----------
def discover_rss(feed_url, limit):
    root = ET.fromstring(fetch(feed_url))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    items = [(i.findtext("title"), i.findtext("link"), i.findtext("description")) for i in root.iter("item")]
    items += [(e.findtext("a:title", namespaces=ns), (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else None),
               e.findtext("a:summary", namespaces=ns)) for e in root.iter("{http://www.w3.org/2005/Atom}entry")]
    return [dict(origin="web", ref=link.strip(), title=strip_tags(t), summary=strip_tags(s)[:400])
            for t, link, s in items[:limit] if link and t]


# ---------- Hacker News ----------
HN = "https://hacker-news.firebaseio.com/v0"


def discover_hackernews(limit):
    ids = get_json(f"{HN}/topstories.json")[:limit]
    return [dict(origin="hackernews", ref=str(i), title=it.get("title", ""), summary="")
            for i in ids if (it := get_json(f"{HN}/item/{i}.json") or {}).get("type") == "story"]


def fetch_hackernews(item_id):
    it = get_json(f"{HN}/item/{item_id}.json")
    discussion = f"https://news.ycombinator.com/item?id={item_id}"
    pop = min(1, math.log10(max(1, it.get("score", 1))) / 3)  # 1000 points = full marks
    if it.get("url"):
        a = fetch_web(it["url"], extra_credit=f" (via Hacker News: {discussion})")
    else:  # Ask HN / text post
        text = strip_tags(it.get("text"))
        if len(text) < MIN_TEXT:
            raise SystemExit("this Hacker News post has no linked article and too little text")
        a = dict(origin="web", title=it.get("title", ""), description="", text=text, url=discussion, images=[],
                 credit=f'Based on the Hacker News post "{it.get("title", "")}" by {it.get("by", "unknown")}: {discussion}',
                 source_label="Source: Hacker News")
    return a | dict(origin="hackernews", key=f"hackernews:{item_id}", popularity=pop)


# ---------- arXiv (abstracts; metadata is CC0) ----------
ARXIV = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def _arxiv_entries(**params):
    root = ET.fromstring(fetch(ARXIV + "?" + urllib.parse.urlencode(params)))
    for e in root.findall("a:entry", ATOM):
        aid = e.findtext("a:id", namespaces=ATOM).rsplit("/abs/", 1)[-1]
        yield dict(id=re.sub(r"v\d+$", "", aid), title=strip_tags(e.findtext("a:title", namespaces=ATOM)),
                   summary=strip_tags(e.findtext("a:summary", namespaces=ATOM)),
                   authors=[a.findtext("a:name", namespaces=ATOM) for a in e.findall("a:author", ATOM)],
                   published=(e.findtext("a:published", namespaces=ATOM) or "")[:10])


def discover_arxiv(topic, limit):
    return [dict(origin="arxiv", ref=e["id"], title=e["title"], summary=e["summary"][:400])
            for e in _arxiv_entries(search_query=f'all:"{topic}"', max_results=limit, sortBy="relevance")]


def fetch_arxiv(aid):
    e = next(_arxiv_entries(id_list=aid), None)
    if not e:
        raise SystemExit(f"arXiv paper {aid} not found")
    who = ", ".join(e["authors"][:3]) + (" et al." if len(e["authors"]) > 3 else "")
    url = f"https://arxiv.org/abs/{aid}"
    return dict(origin="arxiv", key=f"arxiv:{aid}", title=e["title"], description=f"arXiv paper, {e['published']}",
                text=e["summary"], url=url, credit=f'Based on the arXiv paper "{e["title"]}" by {who}: {url}',
                source_label="Source: arXiv", images=[], popularity=None)


# ---------- NASA Astronomy Picture of the Day ----------
# The APOD API now returns a placeholder for every date and apod.nasa.gov redirects to science.nasa.gov
# image articles, so APOD is discovered from NASA's RSS feed and read like any web page.
NASA_FEED = "https://www.nasa.gov/feed/"


def discover_apod(limit):
    return [c | dict(origin="apod") for c in discover_rss(NASA_FEED, 100) if c["title"].startswith("APOD")][:limit]


def fetch_apod(url):
    a = fetch_web(url)
    title = re.sub(r"^APOD:\s*\d{4} \w+ \d{1,2}\s*[-–]\s*|\s*-\s*NASA Science$", "", a["title"])
    # images are not reused automatically: many APODs are copyrighted by their photographers
    # key stays the page's web key, so the same APOD found via an RSS feed is not queued twice
    return a | dict(origin="apod", title=title, source_label="Source: NASA APOD",
                    credit=f'Based on NASA\'s Astronomy Picture of the Day "{title}": {a["url"]}')


# ---------- dispatch ----------
FETCH = {"web": fetch_web, "hackernews": fetch_hackernews, "arxiv": fetch_arxiv, "apod": fetch_apod}


def fetch_article(origin, ref):
    return FETCH[origin](ref)


def discover(origin, limit, arg=None):
    """Candidates [{origin, ref, title, summary}] from a source. arg = topic (arxiv) or feed URL (rss)."""
    if origin == "rss":
        if not arg:
            raise SystemExit("rss needs a feed URL")
        return discover_rss(arg, limit)
    if origin == "arxiv":
        if not arg:
            raise SystemExit("arxiv needs a topic")
        return discover_arxiv(arg, limit)
    return {"hackernews": discover_hackernews, "apod": discover_apod}[origin](limit)


def selftest():  # link detection and id stability (no network)
    assert detect("https://en.wikipedia.org/wiki/Emu_War") == ("wikipedia", "https://en.wikipedia.org/wiki/Emu_War")
    assert detect("Emu War")[0] == "wikipedia"
    assert detect("https://news.ycombinator.com/item?id=8863") == ("hackernews", "8863")
    assert detect("https://arxiv.org/abs/2401.01234v2") == ("arxiv", "2401.01234")
    assert detect("https://arxiv.org/pdf/astro-ph/0606678") == ("arxiv", "astro-ph/0606678")
    assert detect("https://apod.nasa.gov/apod/ap240101.html") == ("apod", "https://apod.nasa.gov/apod/ap240101.html")
    assert detect("https://example.com/story") == ("web", "https://example.com/story")
    assert synthetic_id("web:x") == synthetic_id("web:x") > 10 ** 12 > synthetic_id("web:y") - 10 ** 13
    assert strip_tags("<p>a &amp; <b>b</b></p>") == "a & b"
    assert clean_title("GitHub - gmkurtzer/ginux: A userspace kernel", "GitHub") == "gmkurtzer/ginux"
    assert clean_title("GitHub - gmkurtzer/ginux", "GitHub") == "gmkurtzer/ginux"
    assert clean_title("Why the sky is blue | The Atlantic", "The Atlantic") == "Why the sky is blue"
    assert clean_title("Moon to Mars | NASA's Artemis Program - NASA", "NASA") == "NASA's Artemis Program"
    print("sources selftest ok")


if __name__ == "__main__":
    selftest()
