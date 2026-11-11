"""Content sources beyond Wikipedia. Each source can discover candidates and fetch one into an Article:

  {"origin", "key", "title", "description", "text", "url", "credit", "source_label", "images", "popularity"}

key is a stable unique id ("hackernews:8863"); credit goes in the YouTube description; source_label is the
video footer; images are free-licensed picks only (never a web page's own images: they are copyrighted);
popularity (0-1, optional) feeds the potential score. Wikipedia itself stays on rw.py's optimised path.
"""
import hashlib, html, math, random, re, urllib.parse, urllib.robotparser
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


# ---------- RSS / Atom feeds ----------
REDDIT_MEDIA = re.compile(r"//(www\.|old\.)?reddit\.com/|//(i|v|preview)\.redd\.it/|//redd\.it/")


def parse_feed(data, limit):
    """RSS/Atom bytes -> candidates. Each item is classified by its link (web, arXiv, Wikipedia, ...).
    Reddit link posts use the article they point to; Reddit text/image posts are skipped (no source to credit)."""
    root = ET.fromstring(data)
    a = "{http://www.w3.org/2005/Atom}"
    items = [(i.findtext("title"), i.findtext("link"), i.findtext("description") or "") for i in root.iter("item")]
    for e in root.iter(a + "entry"):
        link = next((l.get("href") for l in e.findall(a + "link") if l.get("rel", "alternate") == "alternate"), None)
        items.append((e.findtext(a + "title"), link, e.findtext(a + "summary") or e.findtext(a + "content") or ""))
    out = []
    for title, link, body in items:
        if not (title and link):
            continue
        if REDDIT_MEDIA.search(link):  # Reddit post: follow its "[link]" to the real source
            src = re.findall(r'<a href="([^"]+)">\[link\]</a>', body)
            if not src or REDDIT_MEDIA.search(src[0]):
                continue
            link = html.unescape(src[0])
        origin, ref = detect(link.strip())
        out.append(dict(origin=origin, ref=ref, title=strip_tags(title), summary=strip_tags(body)[:400]))
        if len(out) >= limit:
            break
    return out


def discover_rss(feed_url, limit):
    return parse_feed(fetch(feed_url), limit)


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


# ---------- any link: work out what it is, then find ideas in it ----------
SKIP_PATH = re.compile(r"/(tags?|categor(y|ies)|topics?|authors?|about|contact|privacy|terms|login|sign-?in|sign-?up|"
                       r"subscribe|search|account|newsletters?|feed|rss|page/\d+)(/|$)|\.(jpe?g|png|gif|svg|pdf|zip|mp[34])$", re.I)
HN_LISTS = {"": "topstories", "news": "topstories", "best": "beststories", "newest": "newstories",
            "show": "showstories", "ask": "askstories"}


def page_links(html_text, base, limit):
    """Article-looking links on a listing page (same site, headline-length text, not navigation)."""
    host = urllib.parse.urlsplit(base).netloc.removeprefix("www.")
    seen, out = set(), []
    for href, inner in re.findall(r'<a\s[^>]*?href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html_text, re.S | re.I):
        url, text = urllib.parse.urljoin(base, html.unescape(href)), strip_tags(inner)
        u = urllib.parse.urlsplit(url)
        path = u.path.rstrip("/")
        if (u.scheme not in ("http", "https") or not u.netloc.removeprefix("www.").endswith(host) or SKIP_PATH.search(path)
                or len(text) < 25 or path.count("/") < 1 or not re.search(r"[-_]|\d", path.rsplit("/", 1)[-1]) or url in seen):
            continue
        seen.add(url)
        origin, ref = detect(url)
        out.append(dict(origin=origin, ref=ref, title=text[:200], summary=""))
        if len(out) >= limit:
            break
    return out


def discover_link(url, limit):
    """Any link -> (what it is, candidates). Candidates may be Wikipedia titles (origin "wikipedia")."""
    import trafilatura
    url = url.strip()
    u = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
    host, path = u.netloc.removeprefix("www.").lower(), u.path
    if host.endswith("wikipedia.org") and "/wiki/" in path:
        title = urllib.parse.unquote(path.split("/wiki/", 1)[1]).replace("_", " ")
        if title.startswith("Category:"):
            r = get_json("https://en.wikipedia.org/w/api.php", {"action": "query", "format": "json", "formatversion": 2,
                         "list": "categorymembers", "cmtitle": title, "cmtype": "page", "cmlimit": 500})
            members = random.sample(r["query"]["categorymembers"], len(r["query"]["categorymembers"]))  # not just A, B, C...
            return f"Wikipedia category ({title[9:]})", [dict(origin="wikipedia", ref=m["title"], title=m["title"], summary="")
                                                        for m in members[:limit]]
        if title.startswith(("List of", "Lists of", "Index of", "Outline of")):
            r = get_json("https://en.wikipedia.org/w/api.php", {"action": "query", "format": "json", "formatversion": 2,
                         "titles": title, "prop": "links", "plnamespace": 0, "pllimit": "max", "redirects": 1})
            links = [l["title"] for pg in r["query"]["pages"] for l in pg.get("links", [])]
            links = random.sample(links, len(links))
            return f"Wikipedia list ({title})", [dict(origin="wikipedia", ref=t, title=t, summary="") for t in links[:limit]]
        return "Wikipedia article: exploring around it", [dict(origin="topic", ref=title, title=title, summary="")]
    if host == "news.ycombinator.com" and "item" not in path:
        kind = HN_LISTS.get(path.strip("/"), "topstories")
        ids = get_json(f"{HN}/{kind}.json")[:limit]
        return f"Hacker News ({kind.replace('stories', '')})", [dict(origin="hackernews", ref=str(i), title=f"HN {i}", summary="") for i in ids]
    if host == "arxiv.org" and (m := re.match(r"/list/([\w.\-]+)", path)):
        return f"arXiv {m[1]}", discover_rss(f"https://rss.arxiv.org/rss/{m[1]}", limit)
    if host in ("reddit.com", "old.reddit.com") and (m := re.match(r"/r/(\w+)", path)) and "/comments/" not in path:
        return f"r/{m[1]}", discover_rss(f"https://www.reddit.com/r/{m[1]}/.rss", limit)
    data = fetch(u.geturl())
    head = data[:500].lstrip().lower()
    if head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head:
        return "feed", parse_feed(data, limit)
    page = data.decode("utf-8", "ignore")
    doc = trafilatura.bare_extraction(page, url=u.geturl())
    text = ((doc if isinstance(doc, dict) else doc.as_dict() if doc else {}).get("text") or "")
    links = page_links(page, u.geturl(), 40)
    declared = re.search(r'og:type["\']\s+content=["\']article|"@type"\s*:\s*"(News|Blog|Scholarly|Reportage)?(Article|Posting)"', page, re.I)
    if declared or (len(text) >= 1500 and path.strip("/") and len(links) < 10):  # an article, not a section/home page
        origin, ref = detect(u.geturl())
        return "single article", [dict(origin=origin, ref=ref, title=u.geturl(), summary="")]
    feed = re.search(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', page, re.I)
    href = re.search(r'href=["\']([^"\']+)', feed[0]) if feed else None
    if href:
        feed_url = urllib.parse.urljoin(u.geturl(), html.unescape(href[1]))
        return f"site feed ({feed_url})", discover_rss(feed_url, limit)
    return "links on the page", links[:limit]


# ---------- dispatch ----------
FETCH = {"web": fetch_web, "hackernews": fetch_hackernews, "arxiv": fetch_arxiv, "apod": fetch_apod}


def fetch_article(origin, ref):
    return FETCH[origin](ref)


def discover(origin, limit, arg=None):
    """Candidates [{origin, ref, title, summary}] from a source. arg = topic (arxiv) or feed URL (rss)."""
    if origin == "link":
        if not arg:
            raise SystemExit("paste a link")
        kind, cands = discover_link(arg, limit)
        print(f"link is: {kind}")
        return cands
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
    listing = ('<a href="/2026/11/why-octopuses-dream">Why octopuses dream, according to new sleep research</a>'
               '<a href="/about">About us and our long editorial history here</a><a href="/tag/science">Science</a>'
               '<a href="https://other.com/x-1">An external story that should be ignored entirely</a>')
    assert [c["ref"] for c in page_links(listing, "https://news.example.com/", 10)] == ["https://news.example.com/2026/11/why-octopuses-dream"]
    feed = (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>TIL emus won a war</title>'
            b'<link href="https://www.reddit.com/r/todayilearned/comments/abc/til/"/><content>&lt;a href="https://en.wikipedia.org/wiki/Emu_War"&gt;[link]&lt;/a&gt;</content></entry>'
            b'<entry><title>A selfie</title><link href="https://www.reddit.com/r/pics/comments/x/"/><content>&lt;a href="https://i.redd.it/x.jpg"&gt;[link]&lt;/a&gt;</content></entry></feed>')
    assert [(c["origin"], c["ref"]) for c in parse_feed(feed, 10)] == [("wikipedia", "https://en.wikipedia.org/wiki/Emu_War")]
    assert clean_title("GitHub - gmkurtzer/ginux: A userspace kernel", "GitHub") == "gmkurtzer/ginux"
    assert clean_title("GitHub - gmkurtzer/ginux", "GitHub") == "gmkurtzer/ginux"
    assert clean_title("Why the sky is blue | The Atlantic", "The Atlantic") == "Why the sky is blue"
    assert clean_title("Moon to Mars | NASA's Artemis Program - NASA", "NASA") == "NASA's Artemis Program"
    print("sources selftest ok")


if __name__ == "__main__":
    selftest()
