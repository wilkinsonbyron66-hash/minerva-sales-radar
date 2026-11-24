"""
Article previews for Minerva Sales Radar.

For each item it finds the real publisher URL (decoding Google News links),
then reads only the page's share metadata - og:image and og:description -
the same picture and summary a site shows when its link is shared.
Nothing beyond that publisher-written summary is copied.

Results are cached in data/previews.json (keyed by item id).
"""
import base64
import difflib
import xml.etree.ElementTree as ET
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (compatible; MinervaSalesRadar/1.0; +https://github.com/wilkinsonbyron66-hash/minerva-sales-radar)"
TIMEOUT = 12
MAX_EXCERPT = 200


def _get(url, data=None, headers=None, limit=400_000):
    h = {"User-Agent": UA, "Accept-Language": "en"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read(limit)
        return raw.decode(r.headers.get_content_charset() or "utf-8", "replace"), r.geturl()


# --------------------------------------------------------------------------- #
# Google News link decoding
# --------------------------------------------------------------------------- #
def gn_id(url):
    m = re.search(r"news\.google\.com/(?:rss/)?(?:articles|read)/([A-Za-z0-9_\-]+)", url)
    return m.group(1) if m else None


def decode_old(aid):
    """Older Google News ids carry the URL inside a base64 protobuf."""
    try:
        raw = base64.urlsafe_b64decode(aid + "=" * (-len(aid) % 4))
    except Exception:
        return None
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if m and b"AU_yqL" not in raw[:12]:
        u = m.group(0).decode("ascii", "ignore")
        return u.rstrip("\x01\x02\x03\xd2")
    return None


def decode_new(aid, fetch=_get):
    page, _ = fetch(f"https://news.google.com/rss/articles/{aid}?hl=en-AE&gl=AE&ceid=AE:en")
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        return None
    inner = ('["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
             '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]') % (aid, ts.group(1), sg.group(1))
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
    txt, _ = fetch("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                   data=body.encode(), headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    m = re.search(r'\\"garturlres\\",\\"(https?://[^"\\]+)', txt)
    return m.group(1) if m else None


def real_url(url, fetch=_get):
    aid = gn_id(url)
    if not aid:
        return url
    return decode_old(aid) or decode_new(aid, fetch)


# --------------------------------------------------------------------------- #
# Bing headline lookup: find the same story on Bing News to get its direct
# link and thumbnail (Google News links can't be decoded from GitHub's servers).
# --------------------------------------------------------------------------- #
def _norm(t):
    t = re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def bing_lookup(title, source="", fetch=_get):
    q = urllib.parse.quote_plus(title[:150])
    raw, _ = fetch(f"https://www.bing.com/news/search?q={q}&format=rss&setlang=en&cc=AE")
    root = ET.fromstring(raw.encode("utf-8") if isinstance(raw, str) else raw)
    want, best = _norm(title), (0.0, None)
    for el in root.iter():
        if el.tag.split("}")[-1].lower() != "item":
            continue
        d = {}
        for ch in el:
            d[ch.tag.split("}")[-1].lower()] = (ch.text or "").strip()
        r = difflib.SequenceMatcher(None, want, _norm(html.unescape(d.get("title", "")))).ratio()
        if r > best[0]:
            best = (r, d)
    if best[0] < 0.8 or not best[1]:
        return None
    d = best[1]
    link = d.get("link", "")
    if "bing.com/news/apiclick" in link:
        link = (urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("url") or [""])[0]
    img = html.unescape(d.get("image", ""))
    if re.match(r"https?://[^/]*bing\.(?:com|net)/th", img):
        img = re.sub(r"&(?:w|h|c|pid|rs|qlt)=[^&]*", "", img) + "&w=640&h=360&c=7&rs=1&pid=News"
        img = "https://" + img.split("://", 1)[1]
    desc = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", d.get("description", "")))).strip()
    return {"url": link if link.startswith("http") else "", "thumb": img if img.startswith("http") else "", "snippet": desc}


# --------------------------------------------------------------------------- #
# Share metadata
# --------------------------------------------------------------------------- #
def _meta(page, *names):
    for n in names:
        for pat in (r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*content=["\']([^"\']+)["\']',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']%s["\']'):
            m = re.search(pat % re.escape(n), page, re.I)
            if m:
                return html.unescape(m.group(1)).strip()
    return ""


def share_meta(url, title, fetch=_get):
    page, final = fetch(url)
    head = page[: page.lower().find("</head>") + 7] if "</head>" in page.lower() else page[:200_000]
    img = _meta(head, "og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
    if img:
        img = urllib.parse.urljoin(final, img)
        if not img.startswith("http") or re.search(r"logo|favicon|default|placeholder|sprite", img, re.I):
            img = ""
    desc = _meta(head, "og:description", "twitter:description", "description")
    desc = re.sub(r"\s+", " ", desc).strip()
    if desc and _similar(desc, title):
        desc = ""
    if len(desc) > MAX_EXCERPT:
        desc = desc[:MAX_EXCERPT].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return {"url": final or url, "image": img, "excerpt": desc}


def _similar(a, b):
    na = re.sub(r"[^a-z0-9 ]", "", a.lower())
    nb = re.sub(r"[^a-z0-9 ]", "", b.lower())
    return na == nb or (len(na) < len(nb) + 10 and na[:60] == nb[:60])


# --------------------------------------------------------------------------- #
def enrich(items, cache_path, max_items=150, budget_s=300, fetch=_get, log=print):
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    deadline = time.time() + budget_s
    done = 0
    rank = {"high": 0, "medium": 1, "low": 2}
    for it in sorted(items, key=lambda x: (rank.get(x.get("score"), 3), x.get("published", "")), reverse=False):
        p = cache.get(it["id"])
        direct = it.get("direct_url") or ("" if gn_id(it["link"]) else it["link"])
        retry = p is not None and not p.get("url") and (
            p.get("tries", 0) < 2 or (direct and not p.get("d")) or (not direct and not p.get("b")))
        if (p is None or retry) and done < max_items and time.time() < deadline:
            done += 1
            tries = (p or {}).get("tries", 0) + 1
            extra = {}
            if not direct:
                try:
                    b = bing_lookup(it.get("title", ""), it.get("source", ""), fetch)
                except Exception:
                    b = None
                extra["b"] = 1
                if b:
                    direct = b["url"]
                    extra.update({k: v for k, v in (("thumb", b["thumb"]), ("snippet", b["snippet"])) if v})
                time.sleep(0.6)
            u = None
            try:
                u = direct or real_url(it["link"], fetch)
                p = share_meta(u, it.get("title", ""), fetch) if u else {"tries": tries}
            except Exception:
                p = {"url": u} if u and u != it["link"] else {"tries": tries}
            if direct:
                p["d"] = 1
            p.update(extra)
            if not p.get("url") and direct:
                p["url"] = direct
            cache[it["id"]] = p
            time.sleep(0.4)
        p = dict(p or {})
        # Fall back to what the feed itself gave us (Bing thumbnail, direct link, feed snippet)
        if not p.get("image"):
            p["image"] = it.get("image") or p.get("thumb", "")
        if not p.get("url") and direct:
            p["url"] = direct
        src_text = p.get("snippet") or (it.get("summary") if direct else "")
        if not p.get("excerpt") and src_text and not _similar(src_text, it.get("title", "")):
            ex = re.sub(r"\s+", " ", src_text).strip()
            p["excerpt"] = ex if len(ex) <= MAX_EXCERPT else ex[:MAX_EXCERPT].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
        if p:
            pv = {k: v for k, v in p.items() if v and k in ("url", "image", "excerpt")}
            if pv:
                it["preview"] = pv
    keep = {it["id"] for it in items}
    cache = {k: v for k, v in cache.items() if k in keep}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
    img = sum(1 for it in items if it.get("preview", {}).get("image"))
    exc = sum(1 for it in items if it.get("preview", {}).get("excerpt"))
    log(f"Previews: {done} new lookups, {img}/{len(items)} with image, {exc} with excerpt")
    return items
