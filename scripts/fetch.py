#!/usr/bin/env python3
"""
Minerva Sales Radar - daily RSS fetcher.

Reads config.json, pulls every RSS/Atom feed listed there, keeps items that
match our keywords, scores them with simple keyword rules (no AI), removes
duplicates and old items, and writes data/items.json for the web page.

Standard library only, so GitHub Actions needs no installs.

Usage:
  python scripts/fetch.py                      # live run (used by GitHub Actions)
  python scripts/fetch.py --sample-dir tests/sample_feeds --out /tmp/items.json --now 2026-11-24
"""
import argparse
import difflib
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "Mozilla/5.0 (compatible; MinervaSalesRadar/1.0; +https://github.com/)"

# --------------------------------------------------------------------------- #
# Keyword matching
# --------------------------------------------------------------------------- #

def _pattern(term):
    """Word-boundary regex. Short ALL-CAPS terms (DMR, PTT, RFQ, PDO...) are
    matched case-sensitively so 'sec' or 'oq' inside normal text don't fire."""
    esc = re.escape(term).replace(r"\ ", r"[\s\-]+")
    pre = r"(?<![A-Za-z0-9])"
    post = r"(?![A-Za-z0-9])" if term[-1].isalnum() else ""
    case_sensitive = term.isupper() and len(term.replace(".", "")) <= 5
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(pre + esc + post, flags)


class Matcher:
    def __init__(self, terms):
        self.terms = [(t, _pattern(t)) for t in terms]

    def find(self, text):
        return [t for t, p in self.terms if p.search(text)]


# --------------------------------------------------------------------------- #
# Feed fetching and parsing
# --------------------------------------------------------------------------- #

def build_url(feed, settings):
    if feed.get("url"):
        return feed["url"]
    q = urllib.parse.quote_plus(feed["query"])
    return f"https://news.google.com/rss/search?q={q}&{settings['google_news_params']}"


def build_bing_url(feed, settings):
    """Same search on Bing News. Bing links go straight to the article and carry a thumbnail."""
    q = urllib.parse.quote_plus(feed["query"])
    return f"https://www.bing.com/news/search?q={q}&format=rss&{settings.get('bing_news_params', 'setlang=en&cc=AE')}"


def unwrap_link(link):
    """Bing wraps article links in apiclick.aspx?...&url=<real url>. Return the real URL."""
    if "bing.com/news/apiclick" in link:
        u = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("url")
        if u and u[0].startswith("http"):
            return u[0]
    return link


def bing_thumb(u):
    """Ask Bing's thumbnail service for a card-sized, smart-cropped image."""
    if re.match(r"https?://[^/]*bing\.(?:com|net)/th", u or ""):
        u = re.sub(r"&(?:w|h|c|pid|rs|qlt)=[^&]*", "", u) + "&w=640&h=360&c=7&rs=1&pid=News"
    if u and u.startswith("http://"):
        u = "https://" + u[7:]
    return u


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _strip_ns(tag):
    return tag.split("}", 1)[-1].lower()


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def clean_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def parse_feed(raw):
    """Return list of dicts: title, link, published, source, summary. Handles RSS 2.0 and Atom."""
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        tag = _strip_ns(el.tag)
        if tag not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "published": "", "source": "", "summary": "", "image": ""}
        for ch in el:
            t = _strip_ns(ch.tag)
            if t == "title":
                d["title"] = clean_html(_text(ch))
            elif t == "link":
                d["link"] = (ch.get("href") or _text(ch)).strip() or d["link"]
            elif t in ("pubdate", "published", "updated", "date") and not d["published"]:
                d["published"] = _text(ch)
            elif t == "source":
                d["source"] = _text(ch)
            elif t in ("description", "summary", "content") and not d["summary"]:
                d["summary"] = clean_html(_text(ch))[:400]
            elif t == "image" and not d["image"]:
                # Bing: <News:Image>url</News:Image>
                d["image"] = (_text(ch) or "").strip()
            elif t == "thumbnail" and not d["image"]:
                d["image"] = (ch.get("url") or "").strip()
            elif t == "content" and ch.get("medium") == "image" and not d["image"]:
                d["image"] = (ch.get("url") or "").strip()
        d["link"] = unwrap_link(d["link"])
        if d["image"]:
            d["image"] = bing_thumb(html.unescape(d["image"]))
            if not d["image"].startswith("http"):
                d["image"] = ""
        if d["title"] and d["link"]:
            out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Lead (who to call) detection
# --------------------------------------------------------------------------- #

VERBS = (r"wins|won|bags|secures|lands|clinches|grabs|is awarded|awarded|signs|inks|"
         r"appointed|selected|picked|to build|to develop|to open|opens|set to open|launches|unveils|"
         r"invites|issues|floats|seeks|tenders|tender|RFQ|announces|plans|begins|starts|breaks ground|"
         r"completes|hands|awards|partners with|teams up")
LEAD_PREFIX = re.compile(r"^(?:Saudi Arabia['’]?s?|Abu Dhabi['’]?s?|UAE['’]?s?|Saudi['’]?s?|Oman['’]?s?|Dubai['’]?s?|Gulf|"
                         r"[A-Z][a-z]+-based|Emirati|Omani|Report:|Exclusive:|Breaking:)\s+")
PLACES = {"dubai", "uae", "saudi", "saudi arabia", "arabia", "oman", "abu dhabi", "riyadh", "muscat", "jeddah",
          "sharjah", "neom", "gulf", "gcc", "middle east", "mena", "ras al khaimah", "ajman", "fujairah", "kingdom"}
CONNECT = {"and", "&", "of", "de", "al", "el", "by", "the", "bin", "for"}
TRAILING_JUNK = {"Scheduled", "Set", "Announces", "Gets", "Tops", "Unveils", "Debuts", "Reveals"}
COMPANY_SUFFIX = re.compile(
    r"\b((?:[A-Z][\w&'\.-]*\s){0,4}(?:LLC|L\.L\.C|Group|Company|Co\.|Holding|Holdings|Contracting|"
    r"Construction|Engineering|Energy|Hotels|Resorts|Properties|Developments?|Industries|Services|"
    r"Solutions|Technologies|Security|Petroleum|Gas|Oil|Authority|Municipality|Airport|Airports))\b")
STOPWORDS = {"The", "A", "An", "New", "How", "Why", "What", "This", "Top", "Here", "Watch", "Video"}


def _tidy_name(s):
    s = s.replace("\u2019", "'").strip(" ,:;-–—'\"")
    s = re.split(r"\s*[:|]\s*", s)[-1]                     # "UAE Business: Capella" -> "Capella"
    s = s.split(",")[0]                                       # "Six Senses The Palm, Dubai" -> "Six Senses The Palm"
    s = LEAD_PREFIX.sub("", s)
    s = re.sub(r"\s+(?:has|have|is|was|will|to|and)$", "", s)
    words = s.split()
    while words and words[0] in STOPWORDS:
        words = words[1:]
    while words and words[-1] in TRAILING_JUNK:
        words = words[:-1]
    # Drop a lowercase descriptor: "Luxury dining guide GAYA" -> "GAYA", "Egyptian group Madkour" -> "Madkour"
    low = [i for i, w in enumerate(words) if w[:1].islower() and w.lower() not in CONNECT]
    if low:
        tail = words[low[-1] + 1:]
        head = words[:low[0]]
        words = tail if tail else head
    s = " ".join(words)
    if not s or not s[0].isupper() or len(words) > 7 or s.lower() in PLACES:
        return ""
    return s


def detect_leads(title, known):
    """Returns (lead, client). lead = winner/owner to call; client = awarding party if seen."""
    lead, client = "", ""
    m = re.search(r"\b(?:awards?|awarded|gives|hands)\b.*?\b(?:contract|deal|order|project|job)s?\b.*?\bto\s+([A-Z][\w&'\.\- ]{1,60}?)(?:\s+(?:for|in|at|on|worth|as)\b|[,:;]|$)", title)
    if m:
        lead = _tidy_name(m.group(1))
        cm = re.match(r"^(.{2,60}?)\s+(?:awards?|awarded|gives|hands)\b", title)
        if cm:
            client = _tidy_name(cm.group(1))
    if not lead:
        m = re.search(r"\b(?:contract|deal|order)s?\s+(?:awarded|goes|go)\s+to\s+([A-Z][\w&'\.\- ]{1,60}?)(?:\s+(?:for|in|at|on|worth)\b|[,:;]|$)", title)
        if m:
            lead = _tidy_name(m.group(1))
    if not lead:
        m = re.match(r"^(.{2,70}?)\s+(?:" + VERBS + r")\b", title, re.I)
        if m:
            lead = _tidy_name(m.group(1))
    if not lead:
        hits = [k for k in known if re.search(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])", title)]
        if hits:
            lead = max(hits, key=len)
    if not lead:
        m = re.search(r"\b(?:at|for)\s+(?:the\s+)?([A-Z][\w&'\.-]*(?:\s+[A-Z0-9][\w&'\.-]*){0,4})", title)
        if m and m.group(1) not in STOPWORDS:
            lead = _tidy_name(m.group(1))
    if not lead:
        m = COMPANY_SUFFIX.search(title)
        if m:
            lead = _tidy_name(m.group(1))
    if lead and not client:
        for k in sorted(known, key=len, reverse=True):
            if k != lead and k not in lead and re.search(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])", title):
                client = k
                break
    return lead, client


AWARD_RE = re.compile(r"\b(?:win|wins|won|secure|secures|secured|bag|bags|land|lands|clinch|clinches|"
                      r"award|awards|awarded|sign|signs|signed|ink|inks|bags)\b[^.]{0,70}?\b(?:contracts?|deals?|orders?|packages?|EPC)\b", re.I)

# --------------------------------------------------------------------------- #
# Classification and scoring
# --------------------------------------------------------------------------- #

TENDER_CTX = re.compile(r"\b(?:bidders?|bids? (?:invited|for (?:the |a )?(?:contract|tender|project|package|works|scheme))|"
                        r"bid (?:deadline|submission|bond|opening)|lowest bid|submit(?:ted)? bids?|bidding (?:process|round))\b", re.I)
NOT_TENDER = re.compile(r"\btender offer\b|\blegal tender\b", re.I)
APPOINT_RE = re.compile(r"\b(?:appointed|selected|chosen|hired)\b[^.]{0,30}\b(?:contractor|consultant|developer|operator|"
                        r"EPC|MEP|to (?:build|deliver|design|construct|supply|install|operate|manage))", re.I)
LAUNCH_RE = re.compile(r"\blaunch(?:es|ed)?\b[^.]{0,40}\b(?:project|development|tower|residences|community|district|phase|hotel|mall)\b", re.I)
DEV_LAUNCH_RE = re.compile(r"\b(?:Developments?|Properties|Real Estate|Developer)\b[^.]{0,20}\blaunch(?:es|ed)?\b")
NEW_BUILD_RE = re.compile(r"\bnew\b[^.]{0,25}\b(?:facilit(?:y|ies)|hospitals?|hotels?|malls?|towers?|campus|plants?|terminals?|stadiums?|schools?|factory|warehouse)\b|"
                          r"\b(?:metro|rail|railway|road|airport|line|tram)\b[^.]{0,25}\b(?:to extend|extension|expansion)\b", re.I)
PEOPLE_RE = re.compile(r"\b(?:appoints?|appointed|names|named|promoted|joins|returns to|power list)\b[^.]{0,80}"
                       r"\b(?:GM|general manager|director|chef|CEO|COO|CFO|manager|head of|VP|president|chairman)\b|power list", re.I)
PRIZE_RE = re.compile(r"\b(?:honou?red|award(?:s)? 20\d\d|awards? ceremony|cargo awards|wins? ['‘\"]|named best|ranked)\b", re.I)
CANCEL_RE = re.compile(r"\b(?:cancels?|cancelled|scraps?|scrapped|terminates?|terminated|halts?|suspends?)\b", re.I)
OPENS_NOT_SITE = re.compile(r"\bopens? (?:bookings|subscriptions?|registrations?|applications|nominations|session)|session opens\b", re.I)
POSSESSIVE_ORIGIN = re.compile(r"\b(?:UAE|Dubai|Abu Dhabi|Saudi Arabia|Saudi|Oman|Sharjah)['’]s\s+(?=[A-Z])")


def detect_country(title, text, M):
    """Count target-country mentions. A Gulf name used only as the company's origin ("Dubai's Jumeirah ...
    Lake Como") does not count when the headline names a place outside our market."""
    foreign = M["other"].find(title)
    scan = (POSSESSIVE_ORIGIN.sub(" ", title) if foreign else text)
    hits = []
    for name, m in M["countries"].items():
        n = len(m.find(scan))
        if n:
            hits.append((n, name))
    hits.sort(reverse=True)
    return (hits[0][1] if hits else "Other"), foreign


def classify(item, feed_section, M, cfg):
    text = f"{item['title']} {item['summary']}"
    title = item["title"]
    if PEOPLE_RE.search(title):
        return None  # staff moves and GM lists are not buying signals
    prod = M["product"].find(text)
    tend = [] if NOT_TENDER.search(title) else (M["tender"].find(title) or M["tender"].find(text))
    if not tend and TENDER_CTX.search(text):
        tend = ["bids"]
    award = M["award"].find(title) or (M["award"].find(text) if not PRIZE_RE.search(title) else [])
    if not award and (AWARD_RE.search(title) or APPOINT_RE.search(title)):
        award = ["contract win"]
    proj = M["project"].find(title)
    if OPENS_NOT_SITE.search(title):
        proj = [p for p in proj if p not in ("opens", "opening")]
    if not proj and (LAUNCH_RE.search(title) or DEV_LAUNCH_RE.search(title)):
        proj = ["project launch"]
    if not proj and NEW_BUILD_RE.search(title):
        proj = ["new build"]
    if PRIZE_RE.search(title) and not re.search(r"\bcontract|\bdeal\b|\border\b", title, re.I):
        award, proj = [], []
    comp = M["compliance"].find(text)

    country, foreign = detect_country(title, text, M)
    in_target = country != "Other"
    sectors = [name for name, m in M["sectors"].items() if m.find(text)]

    if not (tend or award or proj or (prod and in_target)):
        return None  # nothing we care about (e.g. Motorola share-price news, US contracts)
    if not in_target and foreign:
        return None  # clearly outside UAE / Saudi / Oman

    section = "tenders" if tend else "awards"

    if CANCEL_RE.search(title):
        score = "low"
    elif in_target and prod and (tend or award or proj):
        score = "high"   # our product category named in a live tender / contract / project in our market
    elif in_target and sectors and (award or proj or tend):
        score = "medium"
    elif in_target and prod:
        score = "medium"  # our product named in a target country, even without a trigger word
    else:
        score = "low"

    lead, client = detect_leads(title, cfg["known_companies"])
    why = why_it_matters(section, score, prod, tend, award, proj, sectors, country, lead)

    return {
        "section": section,
        "score": score,
        "country": country,
        "sectors": sectors,
        "matched": {"product": prod, "trigger": sorted(set(tend + award + proj))},
        "compliance": sorted(set(comp)),
        "lead": lead,
        "client": client,
        "why": why,
    }


SECTOR_PITCH = {
    "oil & gas": "site crews need ATEX/intrinsically safe radios and PTP links for remote sites",
    "hospitality": "hotel ops, security and housekeeping teams run on two-way radios",
    "security": "guards need radios and body cameras on day one",
    "construction": "site teams need radios, repeaters for coverage and PTP links for site offices/CCTV",
    "utilities": "field crews and plants need radio coverage and wireless backhaul",
    "transport": "operations and maintenance teams need radio/TETRA coverage and wireless links",
    "events": "event operations, stewards and security need radio fleets (rental or purchase)",
    "retail": "mall security, facilities and car-park teams use radios and bodycams",
    "healthcare": "hospital security, porters and facilities teams use radios",
}


def why_it_matters(section, score, prod, tend, award, proj, sectors, country, lead):
    where = country if country != "Other" else "the region"
    who = lead or "the company"
    if section == "tenders" and prod:
        p = ", ".join(prod[:2])
        kind = "RFQ/RFP" if any(t.upper() in ("RFQ", "RFP") for t in tend) else "tender"
        return f"Live {kind} mentioning {p} in {where} - get the documents and quote before the deadline."
    if section == "tenders":
        s = SECTOR_PITCH.get(sectors[0], "the project will need site communications") if sectors else "check if comms/radios are in scope"
        return f"Tender in {where}: {s}. Ask who handles communications packages."
    if award:
        s = SECTOR_PITCH.get(sectors[0], "a new contract means new teams to equip") if sectors else "a new contract means new teams to equip"
        return f"{who} just won work in {where}: {s}. Call before they lock in suppliers."
    s = SECTOR_PITCH.get(sectors[0], "a new site will need communications") if sectors else "a new site will need communications"
    return f"New project in {where}: {s}. Reach {who} while they are still buying."


# --------------------------------------------------------------------------- #
# Dedupe, merge, write
# --------------------------------------------------------------------------- #

def norm_title(t):
    t = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,60}$", "", t)  # drop " - Source"
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def split_source(title, source):
    """Google News titles end with ' - Publisher'. Move it to source."""
    m = re.match(r"^(.*\S)\s+[-–—]\s+([^-–—]{2,60})$", title)
    if m and (not source or m.group(2).strip().lower() == source.strip().lower()):
        return m.group(1).strip(), (source or m.group(2).strip())
    return title, source


GENERIC = set("""dubai abu dhabi uae saudi arabia arabias oman omans riyadh muscat jeddah sharjah the a an of in on at to for
and with by from as its new first hotel hotels group resort resorts opens open opening to set announces announced official
date luxury middle east red sea business news contract contracts awards award awarded wins win secures signs deal project
projects phase company holding holdings international global gulf kingdom q1 q2 q3 q4 bn bln mln mn million billion aed sar
usd details inside update world key major top more than over""".split())


def story_tokens(title):
    """Distinctive name words in a headline (Capella, Mazoon, Hongda, Six Senses, Palm...)."""
    t = title.replace("\u2019", "'")
    shouty = sum(1 for w in t.split() if w.isupper() and len(w) > 2) > len(t.split()) / 2
    out = set()
    for w in re.findall(r"[A-Za-z][A-Za-z0-9'\-]+", t):
        if not shouty and not w[0].isupper():
            continue
        k = re.sub(r"'s$", "", w.lower())
        if k not in GENERIC and len(k) > 2:
            out.add(k)
    return out


def _numbers(title):
    return set(re.findall(r"\d[\d,.]*", title))


def same_story(a, b, threshold):
    if difflib.SequenceMatcher(None, a["_norm"], b["_norm"]).ratio() >= threshold:
        return True
    ta, tb = a["_tok"], b["_tok"]
    if not ta or not tb:
        return False
    shared = ta & tb
    if len(shared) < 1 or len(shared) / min(len(ta), len(tb)) < 0.6:
        return False
    if len(shared) == 1 and min(len(ta), len(tb)) > 1:
        return False
    # Different money figures on both sides usually mean two different deals
    na, nb = _numbers(a["title"]), _numbers(b["title"])
    money = re.compile(r"[$₹]|\b(?:AED|SAR|USD|INR|mln|bln|million|billion|crore)\b", re.I)
    if len(shared) <= 2 and na and nb and not (na & nb) and money.search(a["title"]) and money.search(b["title"]):
        return False
    try:
        gap = abs((parse_date(a["published"]) - parse_date(b["published"])).days)
    except TypeError:
        gap = 0
    return gap <= 14


def _richness(it):
    p = it.get("preview") or {}
    return (1 if (p.get("image") or it.get("image")) else 0) + (1 if p.get("excerpt") or it.get("summary") else 0) + \
           (1 if "news.google." not in it["link"] or it.get("direct_url") else 0)


def dedupe(items, threshold):
    """One card per story. Keeps the highest score, then the richest card (picture, excerpt, direct link),
    and copies over anything useful from the copies it drops."""
    seen_links, kept = set(), []
    rank = {"high": 0, "medium": 1, "low": 2}
    for it in items:
        it["_tok"] = story_tokens(it["title"])
    shouty = lambda t: sum(1 for w in t.split() if w.isupper() and len(w) > 2) > len(t.split()) / 2
    items = sorted(items, key=lambda x: (rank[x["score"]], -_richness(x), shouty(x["title"]), -(parse_date(x["published"]) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))
    for it in items:
        if it["link"] in seen_links:
            continue
        k = next((k for k in kept if same_story(it, k, threshold)), None)
        if k is not None:
            if it.get("image") and not k.get("image"):
                k["image"] = it["image"]
            if "news.google." in k["link"] and "news.google." not in it["link"] and not k.get("direct_url"):
                k["direct_url"] = it["link"]
            if it.get("summary") and not k.get("summary"):
                k["summary"] = it["summary"]
            if not k.get("lead") and it.get("lead"):
                k["lead"] = it["lead"]
            if it.get("first_seen", "9") < k.get("first_seen", "9"):
                k["first_seen"] = it["first_seen"]
            k["also"] = k.get("also", 0) + 1
            seen_links.add(it["link"])
            continue
        seen_links.add(it["link"])
        kept.append(it)
    for it in kept:
        it.pop("_tok", None)
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "items.json"))
    ap.add_argument("--sample-dir", help="Read *.xml files from this folder instead of the internet (for testing). "
                                         "File names starting with 'tenders_' or 'awards_' set the section hint.")
    ap.add_argument("--now", help="Pretend today is YYYY-MM-DD (testing).")
    ap.add_argument("--fresh", action="store_true", help="Ignore existing items.json instead of merging.")
    ap.add_argument("--no-contacts", action="store_true", help="Skip the company contact lookup.")
    ap.add_argument("--no-previews", action="store_true", help="Skip article image/excerpt lookup.")
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    st = cfg["settings"]
    now = datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc) if args.now else datetime.now(timezone.utc)
    cutoff = now - timedelta(days=st["max_age_days"])

    kw = cfg["keywords"]
    M = {
        "product": Matcher(kw["product"]),
        "tender": Matcher(kw["tender"]),
        "award": Matcher(kw["award"]),
        "project": Matcher(kw["project"]),
        "compliance": Matcher(cfg["compliance_terms"]),
        "countries": {k: Matcher(v) for k, v in cfg["countries"].items()},
        "other": Matcher(cfg.get("other_places", [])),
        "sectors": {k: Matcher(v) for k, v in cfg["sectors"].items()},
    }

    # Gather (feed_name, section_hint, raw_xml)
    sources = []
    if args.sample_dir:
        for fn in sorted(os.listdir(args.sample_dir)):
            if fn.endswith(".xml"):
                hint = fn.split("_", 1)[0] if fn.split("_", 1)[0] in ("tenders", "awards") else ""
                sources.append((fn, hint, open(os.path.join(args.sample_dir, fn), "rb").read()))
    else:
        for f in cfg["feeds"]:
            url = build_url(f, st)
            try:
                sources.append((f["name"], f.get("section", ""), fetch(url)))
            except Exception as e:  # one bad feed must never stop the run
                print(f"  ! {f['name']}: {e}", file=sys.stderr)
            time.sleep(1.0)  # be polite to Google News
            if st.get("bing_news", True) and f.get("query"):
                bname = "Bing: " + re.sub(r"^GN(?: via)?:?\s*", "", f["name"])
                try:
                    sources.append((bname, f.get("section", ""), fetch(build_bing_url(f, st))))
                except Exception as e:
                    print(f"  ! {bname}: {e}", file=sys.stderr)
                time.sleep(1.0)

    report = []
    fresh = []
    for name, hint, raw in sources:
        try:
            entries = parse_feed(raw)
        except ET.ParseError as e:
            print(f"  ! {name}: not valid RSS ({e})", file=sys.stderr)
            report.append((name, 0, 0))
            continue
        kept = 0
        for e in entries:
            pub = parse_date(e["published"])
            if pub is None or pub < cutoff or pub > now + timedelta(days=1):
                continue
            title, source = split_source(e["title"], e["source"])
            e["title"] = title
            c = classify(e, hint, M, cfg)
            if not c:
                continue
            kept += 1
            fresh.append({
                "id": hashlib.sha1(e["link"].encode()).hexdigest()[:12],
                "title": title,
                "link": e["link"],
                "source": source or name,
                "feed": name,
                "summary": "" if norm_title(e["summary"]).startswith(norm_title(title)[:80]) else e["summary"][:300],
                **({"image": e["image"]} if e.get("image") else {}),
                "published": pub.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "first_seen": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                **c,
                "_norm": norm_title(title),
            })
        report.append((name, len(entries), kept))

    # Merge with previous run so items survive feed churn (still dropped after 30 days)
    old = []
    if not args.fresh and os.path.exists(args.out):
        try:
            prev = json.load(open(args.out, encoding="utf-8"))
            first_seen = {}
            for it in prev.get("items", []):
                first_seen[it["link"]] = it.get("first_seen")
                if parse_date(it["published"]) and parse_date(it["published"]) >= cutoff:
                    # Re-score with today's rules so rule changes apply to older cards too
                    c = classify({"title": it["title"], "summary": it.get("summary", "")}, "", M, cfg)
                    if not c:
                        continue
                    it.update(c)
                    it.pop("also", None)
                    it["_norm"] = norm_title(it["title"])
                    old.append(it)
            for it in fresh:
                if first_seen.get(it["link"]):
                    it["first_seen"] = first_seen[it["link"]]
        except (ValueError, KeyError):
            pass

    fresh_links = {i["link"] for i in fresh}
    combined = fresh + [o for o in old if o["link"] not in fresh_links]
    items = dedupe(combined, st["similar_title_threshold"])
    items.sort(key=lambda x: x["published"], reverse=True)
    items = items[: st["max_items"]]
    for it in items:
        it.pop("_norm", None)

    if not args.no_previews and not args.sample_dir:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import preview
        preview.enrich(items, os.path.join(os.path.dirname(args.out), "previews.json"),
                       max_items=st.get("preview_lookups_per_run", 150), budget_s=st.get("preview_time_budget_s", 300))
        preview.enrich_events(os.path.join(os.path.dirname(args.out), "events.json"),
                              os.path.join(os.path.dirname(args.out), "event_previews.json"))
    if not args.no_contacts and not args.sample_dir:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import contacts
        contacts.enrich(items, os.path.join(os.path.dirname(args.out), "companies.json"), now,
                        max_lookups=st.get("contact_lookups_per_run", 80), budget_s=st.get("contact_time_budget_s", 600))

    out = {
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "feeds_checked": len(report),
        "items": items,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"Feeds read: {len(report)}")
    for name, n, k in report:
        print(f"  {k:>3}/{n:<3} kept  {name}")
    by = {}
    for it in items:
        by[(it["section"], it["score"])] = by.get((it["section"], it["score"]), 0) + 1
    print(f"Saved {len(items)} items -> {args.out}  " + ", ".join(f"{s}/{p}:{n}" for (s, p), n in sorted(by.items())))


if __name__ == "__main__":
    main()
