# Minerva Sales Radar

A free daily board of buying signals for Minerva Technologies: live tenders, contract awards, new projects and trade events in the **UAE, Saudi Arabia and Oman**. Each card says why it matters and who to call.

**How it works (100% free):** GitHub Actions runs `scripts/fetch.py` every day at 06:00 Dubai (02:00 UTC). The script reads RSS feeds (Google News searches, Google Alerts, news sites), scores items with keyword rules (no AI), and saves `data/items.json`. GitHub Pages shows the board.

## Files

| Path | What it is |
|---|---|
| `config.json` | **All keywords, countries, sectors, compliance words and feeds.** Edit this one. |
| `scripts/fetch.py` | The daily fetcher (Python standard library only, nothing to install). |
| `data/items.json` | Signals written by the daily run. Don't edit by hand. |
| `data/events.json` | Events calendar. Edit by hand. |
| `index.html`, `css/`, `js/` | The board. |
| `manifest.json`, `sw.js`, `icons/` | Makes it installable as a phone/desktop app. |
| `.github/workflows/daily.yml` | The daily schedule + "Run workflow" button. |
| `tests/sample_feeds/` | Sample RSS files for testing the script offline. |

## Editing keywords

Open `config.json` on GitHub, click the pencil icon, edit, then **Commit changes**. The next run uses the new words.

- `keywords.product` – our products (two-way radio, TETRA, bodycam, PTMP…).
- `keywords.tender` – tender words (tender, RFQ, RFP, bid…).
- `keywords.award` / `keywords.project` – contract wins and new projects.
- `sectors` – sectors we serve; used for the Medium score.
- `countries` – each country with the city/company names that point to it. Add a city to improve country detection.
- `compliance_terms` – any match puts a red **Check before quoting** badge on the card.
- `known_companies` – helps the "who to call" detection.

Rules: keep the quotes and commas. Short ALL-CAPS words (DMR, PTT, RFQ, PDO) match case-sensitively; everything else ignores case and matches whole words.

## Adding or removing feeds

Each entry in `feeds` is either a Google News search or a direct RSS link:

```json
{"name": "GN: Radio tenders Qatar", "section": "tenders", "query": "walkie-talkie tender Qatar"},
{"name": "My Google Alert",        "section": "awards",  "url": "https://www.google.com/alerts/feeds/123/456"}
```

- `query` is written like a normal Google search (quotes, OR, site:zawya.com all work). The script builds the Google News RSS link for you (`hl=en-AE&gl=AE&ceid=AE:en`).
- `section` is a hint: `tenders` or `awards`. The keywords decide in the end.
- **Google Alerts:** create an alert at google.com/alerts → *Show options* → *Deliver to: RSS feed* → copy the RSS icon link into a `url` entry.
- To remove a feed, delete its line (and the comma before it if it was the last one).
- A feed that fails is skipped; the run carries on. Check the run log under **Actions** to see `kept/total` per feed.

## Scoring

- **High:** tender/RFQ word + product word + UAE/Saudi/Oman.
- **Medium:** contract award or new project (or a general tender) in a target country **and** a sector we serve; also any product mention in a target country.
- **Low:** anything else that matched a keyword.
- Duplicates (same link, or titles ≥85% similar) are removed. Items older than 30 days are dropped.

## Events

Edit `data/events.json`. Fields: `name, start, end, location, venue, sectors, why, book_by, confirmed, note, url`. Past events hide themselves. Set `confirmed` to `true` once you've checked the organiser's site.

## Running it

- **Manual run:** Actions → *Daily radar update* → **Run workflow**.
- **Test locally:** `python scripts/fetch.py --sample-dir tests/sample_feeds --out /tmp/items.json --now 2026-11-24 --fresh`

**Required once:** Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save. Without it the daily run can't save `items.json`.

## On the board

- Filters: section, country, score, search; sort by newest or highest score.
- **Add to call list** → Call list tab → **Export CSV** (company, title, link, country, date) for your dialler.
- **Snooze 7 days** and **Not relevant** hide a card; tick *Show hidden* to bring it back.
- The call list, snoozes and hidden cards are saved in this browser only (localStorage).
