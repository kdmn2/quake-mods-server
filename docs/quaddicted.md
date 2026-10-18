# Quaddicted integration note

Written after inspecting the real upstream interface on **2026-10-17/18**. Re-verify these facts before relying on them
after upstream changes.

## What Quaddicted actually offers

1. **Official read-only JSON API** — `https://www.quaddicted.com/api/v1/?q=<Solr query>`. The admin entry pages on
   quaddicted.com link to it themselves (every `/db/v2/...` page links `View raw JSON metadata` to an `/api/v1/?q=sha256:…`
   URL), so it is the *intended* machine-readable interface, not a scrape target. Behaviours verified live:

   | Behaviour | Verified by | Result |
   |---|---|---|
   | Shape | `curl /api/v1/?q=sha256:04fb…` | JSON **array** of document objects (not wrapped in `{results: …}`) |
   | Field queries | `q=sha256:<hash>`, `q=tags:"title=Return to Earth"` | work; bare-word and `title:` queries return 0 |
   | OR groups | `q=sha256:(a OR b)` | works (used for delta sync) |
   | Paging | `rows=500&start=N`, `fl=sha256,json_timestamp` | works (Solr style) |
   | Full catalogue | `q=*:*` | 1 947 documents, ≈ 18.4 MB in one answer |

2. **Same documents in a Git mirror** — `github.com/Quaddicted/quaddicted-data` (`json/by-sha256/<2hex>/<sha256>.json`),
   README: “The data behind https://www.quaddicted.com/”. Not used at runtime (the API is equivalent); documented as the
   bulk fallback.

3. **Not used:** the 5.9 MB `/db/v2/` HTML table (browser view), ratings (site-side, not in the API), and the
   `/filebase/<name>` links (301 → category pages; still valid through the safe downloader if they are the entry's only URL).

## Document schema (verified fields of 1 947 documents)

```
sha256           64-hex — the stable entry identity (the ID *is* the content hash)
bytes            int — package size
description      string (wanted: plain; treated as untrusted text anyway)
json_timestamp   ISO 8601 — when upstream last regenerated the document (increment marker)
json_version     string ("20250720-fromsolr")
tags             list of "key=value" strings:
                 title, author (often several), filename, game, game_mode, type (map | mod | episode | speedmap |
                 mapjam | contest | demo | art | …), release_date, zipbasedir, theme, startmap, map_size,
                 depends=(copper>=1.30), dependency=<installer-name>, provides=<name=x.y>, mod=<family>, version,
                 link[:homepage|:review|:source], exceeds_quake_limits, commandline, source, nuked
urls             list of direct download URLs; www.quaddicted.com/files/by-sha256/<2hex>/<sha256>/<name> is
                 content-addressed (verified: HTTP 200, application/zip, honest Content-Length)
install          {"extract": "{base}/id1/"} — where the archive extracts
files            per-member listing (bytes, sha256, timestamp) — verified present in ~1 934/1 947 docs; kept out of the
                 local index columns except as a count (kept in full only in `tags_json`-adjacent raw form, not stored)
notes            string, sometimes HTML — stripped to plain text at parse time
```

Not available through the API (and therefore never shown): ratings/counts, per-entry screenshots *as metadata*
(screenshot URLs are derived instead, below), and there is **no documented rate limit** — access is kept conservative.

## Derived URLs (verified against live files)

* Entry page: `https://www.quaddicted.com/db/canonical/<sha256>` (200).
* Screenshot: `https://www.quaddicted.com/files/quaddicted-images/by-sha256/<2hex>/<sha256>/<zip-stem>.jpg`
  (verified for rte.zip → rte.jpg and retro_mfx_fix.zip → retro_mfx_fix.jpg; missing images simply 404 in the browser).
* Download: `urls[0]` preferring the `files/by-sha256` link, then any www.quaddicted.com link, then the first URL.

## What this app does with it

* **Local cached index.** All searching/filtering/paging happens against a bounded SQLite copy
  (`quaddicted_entries`, under the normal DB). Nothing upstream is requested while browsing. Refresh is **manual**
  (button on the page), pace-limited (`QUADDICTED_SYNC_MIN_SECONDS` default 300 s):
  the cheap listing (`fl=sha256,json_timestamp`, ~200 KB) is diffed against the local copy; full documents are fetched
  only for changed entries (chunked OR queries, ≤ `CHUNK_MAX_QUERIES × CHUNK_SIZE` entries per sync) — or a bounded paged
  refetch if too much changed. A failed/incomplete refresh keeps the previous index; the UI shows the cache age.
  Removed-upstream entries are pruned when a refresh succeeds.
* **Import.** Selecting entries hands the resolved canonical download URL to the existing `BatchService`
  (source type `quaddicted`): the shared safe downloader, duplicate decisions, inspection → conversion → validation →
  **draft** pipeline, and job progress are exactly the ones every other source uses. The downloaded package's SHA-256 is
  verified against the entry ID; a mismatch fails the item, drafts are never auto-published.
* **One route accepts individual identifiers:** `POST /quaddicted/import` takes `sha` values (or a pasted ID — the entry
  detail page works for any 64-hex ID in the index), so pasting an entry page URL or ID after a refresh works.

## Metadata mapping (verified field → app field)

Draft forms get upstream values as **editable suggestions**; nothing overwrites user-edited metadata later. Provenance
goes to the add-on's `conversion_report.provenance` (not user-editable).

| Quaddicted field | Used as | Where |
|---|---|---|
| `tags: title` | `title` suggestion | draft form |
| `tags: author` (joined `; `) | `author` suggestion | draft form |
| `tags: release_date` | `date` suggestion | draft form |
| `description` | `description` suggestion | draft form |
| `tags: filename` | add-on ID / gamedir base (`rte.zip` → `rte`), display name | batch item; editable |
| `install.extract`, `tags: zipbasedir` | shown as an extraction hint | entry page |
| `depends`, `mod`, `dependency`, `provides` | dependency warning (display-only) | entry page + browse detail |
| `exceeds_quake_limits`, `nuked` | compatibility warnings | entry page |
| `urls` | resolved by `pick_download_url` | acquisition |
| `bytes` | shown size, download budget check | browse/detail |
| `json_timestamp` | upstream update marker | provenance + cache-age note |
| `sha256` | entry identity, duplicate detection (`addons.source_sha256`), download verification | everywhere |
| (not available) | rating, rating count | **not shown** — no fabricated values |

Provenance kept per import: `source="quaddicted"`, `quaddicted_sha256`, `source_page` (canonical URL), `upstream_updated`,
the package's source SHA-256 (`addons.source_sha256`), import timestamp, type, file name. Display title, upstream ID
(sha256) and local install directory remain separate: the install directory comes from the file name, never from a
changed upstream title; re-imports never overwrite addons' user-edited metadata (the existing replacement flow reuses
existing values and requires explicit review).

## Security and limits

* Upstream titles/authors/tags/notes are stripped to plain text at parse time and escaped by Jinja; upstream HTML is
  never rendered as markup; links are filtered to absolute http(s) URLs; no general image proxy (the browser loads
  screenshots directly from quaddicted.com).
* Refreshes and downloads use the shared `netfetch` protections unchanged (SSRF filters, canonical-address rules, TLS
  verification, timeouts, streaming size caps, bounded retries); tests re-assert the private-address refusal.
* All `/quaddicted*` routes require the admin login; every POST carries the session CSRF token (covered by tests).
* Cache growth is bounded: upserts refresh in place, prunes remove upstream-removed entries, and the whole index is
  ~2 000 short rows. No other new storage.

## Configuration (all optional)

| Variable | Default | Meaning |
|---|---|---|
| `QUADDICTED_SYNC_MIN_SECONDS` | 300 (built-in constant; not yet env-overridable) | minimum seconds between manual refreshes |
| `QUADDICTED_CONNECT_TIMEOUT` / `QUADDICTED_READ_TIMEOUT` / `QUADDICTED_MAX_DURATION` | 10 / 60 / 600 s | metadata fetch limits |

No new volumes, containers or services are required: everything lives in the existing database (`/appdata`) — no change
to the Docker/Portainer/Unraid configuration is needed for this feature.

## Limitations (explicit)

* Searching is **local, not live**: the copy is only as current as the last manual refresh (the UI shows its age).
* Ratings are not available in the API and are not shown. Screenshot URLs follow a naming convention; entries that
  deviate simply have no image. Not every document has `files`/`install`/`notes`; those fields are optional in the UI.
* Live smoke test: `python tools/smoke_quaddicted.py` (listing → full sync → one small entry download + hash check).
  Suite tests never touch the network (fixtures + local test server via the injectable transport).
* Actual Ironwail runtime installation/loading of an imported package was NOT tested here (no client in the test
  environment); the acceptance tests verify the PAK structure against the converter's own readers.
