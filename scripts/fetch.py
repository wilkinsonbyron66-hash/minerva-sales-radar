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
         r"completes|hands|awards|partners|teams up")
LEAD_PREFIX = re.compile(r"^(?:UAE'?s|Saudi'?s|Saudi Arabia'?s|Oman'?s|Dubai'?s|Abu Dhabi'?s|Gulf|"
                         r"[A-Z][a-z]+-based|Emirati|Omani|Saudi|Report:|Exclusive:|Breaking:)\s+", re.I)
COMPANY_SUFFIX = re.compile(
    r"\b((?:[A-Z][\w&'\.-]*\s){0,4}(?:LLC|L\.L\.C|Group|Company|Co\.|Holding|Holdings|Contracting|"
    r"Construction|Engineering|Energy|Hotels|Resorts|Properties|Developments?|Industries|Services|"
    r"Solutions|Technologies|Security|Petroleum|Gas|Oil|Authority|Municipality|Airport|Airports))\b")
STOPWORDS = {"The", "A", "An", "New", "How", "Why", "What", "This", "Top", "Here", "Watch", "Video"}


def _tidy_name(s):
    s = LEAD_PREFIX.sub("", s.replace("\u2019", "'").strip(" ,:;-–—'\""))
    s = re.sub(r"\s+(?:has|have|is|was|will|to|and)$", "", s)
    words = s.split()
    while words and words[0] in STOPWORDS:
        words = words[1:]
    s = " ".join(words)
    if not s or not s[0].isupper() or len(words) > 7:
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
                      r"award|awards|awarded|sign|signs|signed|ink|inks|bags)\b[^.]{0,60}?\b(?:contract|deal|order|package|EPC)\b", re.I)

# --------------------------------------------------------------------------- #
# Classification and scoring
# --------------------------------------------------------------------------- #

def classify(item, feed_section, M, cfg):
    text = f"{item['title']} {item['summary']}"
    title = item["title"]
    prod = M["product"].find(text)
    tend = M["tender"].find(title) or M["tender"].find(text)
    award = M["award"].find(text)
    proj = M["project"].find(title)
    if not award and AWARD_RE.search(title):
        award = ["contract win"]
    comp = M["compliance"].find(text)

    countries = []
    for name, m in M["countries"].items():
        n = len(m.find(text))
        if n:
            countries.append((n, name))
    countries.sort(reverse=True)
    country = countries[0][1] if countries else "Other"

    sectors = [name for name, m in M["sectors"].items() if m.find(text)]

    if not (prod or tend or award or proj):
        return None  # nothing we care about

    if tend and (prod or feed_section == "tenders"):
        section = "tenders"
    elif award or proj:
        section = "awards"
    elif tend:
        section = "tenders"
    else:
        section = feed_section or "awards"

    in_target = country != "Other"
    if tend and prod and in_target:
        score = "high"
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


def dedupe(items, threshold):
    seen_links, kept = set(), []
    rank = {"high": 0, "medium": 1, "low": 2}
    items = sorted(items, key=lambda x: (rank[x["score"]], x["published"]), reverse=False)
    for it in items:
        if it["link"] in seen_links:
            continue
        nt = it["_norm"]
        dup = False
        for k in kept:
            if difflib.SequenceMatcher(None, nt, k["_norm"]).ratio() >= threshold:
                dup = True
                # Same story from two feeds: keep the best bits of both
                # (a Bing copy brings a direct link and thumbnail to a Google News card).
                if it.get("image") and not k.get("image"):
                    k["image"] = it["image"]
                if "news.google." in k["link"] and "news.google." not in it["link"] and not k.get("direct_url"):
                    k["direct_url"] = it["link"]
                if it.get("summary") and not k.get("summary"):
                    k["summary"] = it["summary"]
                break
        if dup:
            continue
        seen_links.add(it["link"])
        kept.append(it)
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
