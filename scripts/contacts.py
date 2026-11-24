"""
Contact finder for Minerva Sales Radar (free, no API keys).

For each lead company it:
  1. finds the official website via Wikidata (free public API), with a careful
     domain guess as fallback;
  2. reads the homepage + contact page (respecting robots.txt) for the public
     switchboard number and general email (info@, sales@, procurement@ ...);
  3. pulls people named in the headline/summary ("said X, CEO of Y");
  4. suggests the role to ask for and a LinkedIn search link.

Results are cached in data/companies.json so each company is looked up once a month.
Only company-level, publicly published business contacts are collected.
"""
import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timedelta, timezone

UA = "MinervaSalesRadar/1.0 (+https://github.com/wilkinsonbyron66-hash/minerva-sales-radar; daily news digest)"
TIMEOUT = 12
RECHECK_DAYS = 30

COMPANYISH = re.compile(
    r"company|business|enterprise|firm|group|conglomerate|corporation|contractor|developer|operator|"
    r"hotel|resort|chain|airline|airport|authority|utility|bank|holding|subsidiary|manufacturer|"
    r"producer|provider|organization|organisation|agency|giga-project|project|real estate|port|railway|"
    r"municipality|ministry|government", re.I)
NOT_COMPANY = re.compile(r"family name|given name|disambiguation|human|album|film|song|species|asteroid|village|scholarly article", re.I)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,10}")
PHONE_RE = re.compile(r"(?:\+|00)\s?(?:971|966|968|973|974|965|20|44|1|91|33|39|49|81|82|86|90|65|61|34|31)[\s\-.()0-9]{7,18}\d")
LOCAL_PHONE_RE = re.compile(r"(?<!\d)(?:800\s?\d{3,7}|0[2-9][\s\-]?\d{3}[\s\-]?\d{4}|9200\s?\d{5}|8001\s?\d{5,6})(?!\d)")
GOOD_PREFIX = ["procurement", "purchasing", "purchase", "sales", "business", "bd", "enquiries", "enquiry",
               "inquiries", "info", "contact", "hello", "general", "reception", "reservations", "admin"]
BAD_PREFIX = re.compile(r"^(no-?reply|donotreply|privacy|dpo|gdpr|careers?|jobs|hr|recruit|cv|resume|media|press|pr|"
                        r"investor|ir|abuse|webmaster|example|test|name|your|email|user|support@sentry)", re.I)
BAD_EMAIL_DOMAIN = re.compile(r"(sentry|wixpress|example|domain|email|yourcompany|godaddy|cloudflare|"
                              r"\.(png|jpg|jpeg|gif|svg|webp|css|js)$)", re.I)

COUNTRY_CODE = {"UAE": "971", "Saudi Arabia": "966", "Oman": "968"}

ROLE_BY_SECTOR = {
    "oil & gas": "Procurement Manager or Project Manager (site communications)",
    "hospitality": "Pre-opening General Manager, Director of Engineering or IT Manager",
    "security": "Operations Manager or Procurement Manager",
    "construction": "Procurement Manager or Project Manager",
    "utilities": "Procurement Manager or Operations / Maintenance Manager",
    "transport": "Operations Manager or Procurement Manager",
    "events": "Operations Manager or Event Production Manager",
    "retail": "Facilities Manager or Security Manager",
    "healthcare": "Facilities Manager or Security Manager",
}
TENDER_ROLE = "Procurement / Tenders department (quote the tender reference)"

PERSON_ROLE = (r"(?:Group\s+)?(?:CEO|COO|CFO|CTO|CIO|MD|Chief\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+Officer|Chief\s+Executive|"
               r"Managing\s+Director|General\s+Manager|Chairman|Chairwoman|President|Vice\s+President|"
               r"(?:Senior\s+)?Director(?:\s+of\s+[A-Z][\w&]+(?:\s+[A-Z][\w&]+){0,3})?|"
               r"Head\s+of\s+[A-Z][\w&]+(?:\s+[A-Z][\w&]+){0,3}|Founder|Partner|Executive\s+Director)")
NAME = r"(?:(?:Dr|Eng|Sheikh|H\.E\.|HE|Mr|Ms|Mrs)\.?\s+)?[A-Z][a-z'\-]+(?:\s+(?:bin|bint|al|Al|el|El|de|van|von|Abdul|Abu)?\s*[A-Z][a-z'\-]+){1,3}"
PEOPLE_PATTERNS = [
    re.compile(r"(" + NAME + r"),\s+(?:the\s+)?(" + PERSON_ROLE + r")"),
    re.compile(r"(" + PERSON_ROLE + r")\s+(" + NAME + r")"),
]
NOT_A_NAME = re.compile(r"^(The|This|That|Saudi|Abu|Dubai|Oman|UAE|Emirates|Group|Company|New|Middle|East|Gulf)\b")


# --------------------------------------------------------------------------- #
def norm_key(name):
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


class Web:
    """Polite fetcher: robots.txt aware, one UA, short timeouts, per-run budget."""

    def __init__(self, deadline, fetcher=None):
        self.deadline = deadline
        self.robots = {}
        self.fetcher = fetcher  # injected in tests

    def time_left(self):
        return time.time() < self.deadline

    def get(self, url, check_robots=True):
        if not self.time_left():
            return None, None
        if self.fetcher:
            return self.fetcher(url)
        try:
            if check_robots and not self._allowed(url):
                return None, None
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                ctype = r.headers.get("Content-Type", "")
                if "json" not in ctype and "html" not in ctype and "text" not in ctype:
                    return None, None
                return r.read(600_000).decode(r.headers.get_content_charset() or "utf-8", "replace"), r.geturl()
        except Exception:
            return None, None

    def _allowed(self, url):
        p = urllib.parse.urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if "wikidata.org" in p.netloc:
            return True
        if base not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=8) as r:
                    rp.parse(r.read(200_000).decode("utf-8", "replace").splitlines())
            except Exception:
                rp = None  # no robots.txt reachable -> allowed
            self.robots[base] = rp
        rp = self.robots[base]
        return True if rp is None else rp.can_fetch(UA, url)


# --------------------------------------------------------------------------- #
def wikidata_site(web, name):
    q = urllib.parse.urlencode({"action": "wbsearchentities", "search": name, "language": "en",
                                "format": "json", "limit": 6, "type": "item"})
    txt, _ = web.get("https://www.wikidata.org/w/api.php?" + q, check_robots=False)
    if not txt:
        return None
    try:
        hits = json.loads(txt).get("search", [])
    except ValueError:
        return None
    cands = [h for h in hits if not NOT_COMPANY.search(h.get("description", ""))]
    if not cands:
        return None
    ids = "|".join(h["id"] for h in cands)
    q = urllib.parse.urlencode({"action": "wbgetentities", "ids": ids, "props": "claims|descriptions|labels",
                                "languages": "en", "format": "json"})
    txt, _ = web.get("https://www.wikidata.org/w/api.php?" + q, check_robots=False)
    if not txt:
        return None
    try:
        ents = json.loads(txt).get("entities", {})
    except ValueError:
        return None
    nk = norm_key(name)
    best = None
    for h in cands:
        e = ents.get(h["id"], {})
        claims = e.get("claims", {})
        site = _claim(claims, "P856")
        if not site:
            continue
        desc = h.get("description", "")
        label = norm_key(h.get("label", ""))
        score = 0
        if COMPANYISH.search(desc):
            score += 2
        if label == nk:
            score += 2
        elif nk in label or label in nk:
            score += 1
        if score >= 2 and (best is None or score > best[0]):
            best = (score, {"website": site, "wikidata": h["id"], "description": desc,
                            "phone": _claim(claims, "P1329"), "email": (_claim(claims, "P968") or "").replace("mailto:", "")})
    return best[1] if best else None


def _claim(claims, prop):
    for c in claims.get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(v, str):
            return v
    return None


def guess_site(web, name):
    """Try obvious domains, accept only if the page title mentions the company."""
    words = [w for w in norm_key(name).split() if w not in {"group", "company", "co", "llc", "the", "and", "consortium", "holding", "holdings"}]
    if not words:
        return None
    slugs = {"".join(words), "-".join(words), words[0]} if len(words[0]) >= 5 else {"".join(words), "-".join(words)}
    for slug in list(slugs)[:3]:
        for tld in (".com", ".ae", ".com.sa", ".om"):
            url = f"https://www.{slug}{tld}/"
            txt, final = web.get(url)
            if not txt:
                continue
            title = re.search(r"<title[^>]*>(.*?)</title>", txt, re.I | re.S)
            t = norm_key(html.unescape(title.group(1))) if title else ""
            if all(w in t for w in words[:2]):
                return {"website": final or url, "guessed": True}
    return None


# --------------------------------------------------------------------------- #
def site_contacts(web, site, country, company=""):
    company_words = [w for w in norm_key(company).split() if len(w) >= 5]
    home, final = web.get(site)
    if not home:
        return {}
    base = final or site
    pages = [(home, base)]
    links = re.findall(r"""href=["']([^"'#]+)["']""", home, re.I)
    contact_links = []
    for l in links:
        ll = l.lower()
        if any(k in ll for k in ("contact", "get-in-touch", "reach-us", "enquir", "inquir", "our-offices", "locations")):
            u = urllib.parse.urljoin(base, l)
            if urllib.parse.urlsplit(u).netloc.split(":")[0].endswith(_root(base)) and u not in contact_links:
                contact_links.append(u)
    contact_links.sort(key=lambda u: (("contact" not in u.lower()), len(u)))
    for u in contact_links[:2]:
        t, f = web.get(u)
        if t:
            pages.append((t, f or u))

    emails, phones = [], []
    root = _root(base)
    for txt, url in pages:
        raw = html.unescape(txt)
        for m in re.findall(r"mailto:([^\"'?>\s]+)", raw, re.I):
            emails.append((urllib.parse.unquote(m).strip().lower(), url))
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
        for m in EMAIL_RE.findall(text):
            emails.append((m.lower(), url))
        for m in re.findall(r"tel:([+0-9()\-\s.]{6,25})", raw, re.I):
            phones.append((m.strip(), url))
        plain = re.sub(r"<[^>]+>", " ", text)
        for m in PHONE_RE.findall(plain) + LOCAL_PHONE_RE.findall(plain):
            phones.append((m.strip(), url))

    email, email_src = _best_email(emails, root, company_words)
    phone, phone_src = _best_phone(phones, country)
    out = {}
    if email:
        out["email"], out["email_source"] = email, email_src
    if phone:
        out["phone"], out["phone_source"] = phone, phone_src
    if len(pages) > 1:
        out["contact_page"] = pages[1][1]
    return out


def _root(url):
    host = urllib.parse.urlsplit(url).netloc.split(":")[0].lower()
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "co", "gov", "net", "org", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _best_email(found, root, company_words=()):
    best = None
    for e, src in found:
        e = e.strip(".,;:")
        if "@" not in e or BAD_EMAIL_DOMAIN.search(e) or len(e) > 60:
            continue
        local, dom = e.split("@", 1)
        if BAD_PREFIX.search(local):
            continue
        same = dom == root or dom.endswith("." + root) or any(w in dom for w in company_words)
        if not same:
            continue  # only emails on the company's own domain
        rank = next((i for i, p in enumerate(GOOD_PREFIX) if local.startswith(p)), len(GOOD_PREFIX))
        if re.search(r"[a-z]+\.[a-z]+", local) and rank == len(GOOD_PREFIX):
            continue  # looks like a personal mailbox (first.last) -> skip
        if best is None or rank < best[0]:
            best = (rank, e, src)
    return (best[1], best[2]) if best else (None, None)


def _pretty(p):
    p = re.sub(r"\s+", " ", p).strip(" -.")
    if " " not in p and p.startswith("+"):
        d = p[1:]
        for cc in ("971", "966", "968"):
            if d.startswith(cc):
                rest = d[len(cc):]
                return "+" + cc + " " + rest[:-7] + " " + rest[-7:-4] + " " + rest[-4:] if len(rest) >= 8 else "+" + cc + " " + rest
    return p


def _best_phone(found, country):
    cc = COUNTRY_CODE.get(country)
    best = None
    for p, src in found:
        digits = re.sub(r"\D", "", p)
        if len(digits) < 7 or len(digits) > 15 or len(set(digits)) < 3:
            continue
        intl = digits.startswith(("971", "966", "968", "00")) or p.strip().startswith("+")
        score = 0
        if cc and (digits.startswith(cc) or digits.startswith("00" + cc)):
            score += 3
        elif any(digits.startswith(c) or digits.startswith("00" + c) for c in COUNTRY_CODE.values()):
            score += 2
        if intl:
            score += 1
        if best is None or score > best[0]:
            best = (score, _pretty(p), src)
    return (best[1], best[2]) if best else (None, None)


# --------------------------------------------------------------------------- #
def people_in_text(text):
    out, seen = [], set()
    for i, pat in enumerate(PEOPLE_PATTERNS):
        for m in pat.finditer(text):
            name, role = (m.group(1), m.group(2)) if i == 0 else (m.group(2), m.group(1))
            name = re.sub(r"\s+", " ", name).strip()
            if NOT_A_NAME.search(name) or len(name.split()) < 2:
                continue
            k = name.lower()
            if k not in seen:
                seen.add(k)
                out.append({"name": name, "role": re.sub(r"\s+", " ", role).strip()})
    return out[:3]


def ask_for(item):
    if item.get("section") == "tenders":
        return TENDER_ROLE
    for s in item.get("sectors") or []:
        if s in ROLE_BY_SECTOR:
            return ROLE_BY_SECTOR[s]
    return "Procurement Manager or Operations Manager"


def linkedin_url(company, role):
    first_role = re.split(r",| or |/", role)[0].strip()
    first_role = re.sub(r"\(.*?\)", "", first_role).strip()
    q = f"{company} {first_role}"
    return "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(q)


# --------------------------------------------------------------------------- #
def enrich(items, cache_path, now, max_lookups=80, budget_s=600, fetcher=None, log=print):
    """Adds item['contact'] to every item with a lead. Updates the company cache file."""
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    web = Web(time.time() + budget_s, fetcher)
    lookups = 0
    stale_before = now - timedelta(days=RECHECK_DAYS)

    rank = {"high": 0, "medium": 1, "low": 2}
    for it in sorted(items, key=lambda x: rank.get(x.get("score"), 3)):
        company = it.get("lead") or it.get("client") or ""
        role = ask_for(it)
        contact = {"ask_for": role}
        people = people_in_text(f"{it.get('title', '')}. {it.get('summary', '')}")
        if people:
            contact["people"] = people
        if company:
            contact["linkedin"] = linkedin_url(company, role)
            key = norm_key(company)
            entry = cache.get(key)
            fresh = entry and entry.get("checked") and datetime.fromisoformat(entry["checked"].replace("Z", "+00:00")) > stale_before
            if not fresh and lookups < max_lookups and web.time_left():
                lookups += 1
                entry = lookup_company(web, company, it.get("country", ""))
                entry["checked"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                cache[key] = entry
            if entry:
                for k in ("website", "guessed", "phone", "phone_source", "email", "email_source", "contact_page"):
                    if entry.get(k):
                        contact[k] = entry[k]
        it["contact"] = contact

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    # keep cache tidy: drop entries not checked in 120 days
    cutoff = now - timedelta(days=120)
    cache = {k: v for k, v in cache.items()
             if v.get("checked") and datetime.fromisoformat(v["checked"].replace("Z", "+00:00")) > cutoff}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
    found = sum(1 for it in items if it["contact"].get("phone") or it["contact"].get("email"))
    log(f"Contacts: {lookups} new company lookups, {found}/{len(items)} items have a phone or email")
    return items


def lookup_company(web, company, country):
    info = wikidata_site(web, company) or guess_site(web, company) or {}
    entry = {"name": company}
    if info.get("website"):
        entry["website"] = info["website"]
        if info.get("guessed"):
            entry["guessed"] = True
        found = site_contacts(web, info["website"], country, company)
        entry.update(found)
        # Wikidata sometimes lists HQ phone/email directly
        if not entry.get("phone") and info.get("phone"):
            entry["phone"], entry["phone_source"] = info["phone"], "https://www.wikidata.org/wiki/" + info.get("wikidata", "")
        if not entry.get("email") and info.get("email"):
            entry["email"], entry["email_source"] = info["email"], "https://www.wikidata.org/wiki/" + info.get("wikidata", "")
    return entry
