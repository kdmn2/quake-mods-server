# Ironwail Add-on Manager

This project provides a lightweight self-hosted admin workflow for an Ironwail-compatible Quake add-on catalog. It keeps the public contract compatible with Ironwail’s verified source at `v0.8.2`, while adding a browser-based admin interface for uploads, metadata, publishing, and catalog generation.

Quick deploy to Unraid
- Native Unraid template: `unraid-template.xml`
- Auto-published image: `ghcr.io/kdmn2/quake-mods-server:latest`
- First-run setup: `http://<unraid-host>:<port>/setup`

Important protocol summary
- Ironwail requests `<base>/content.json`
- Each catalog entry requires `download` and `gamedir`
- The client downloads the raw static file and renames it to `<gamedir>/pak0.pak`
- The client does not unpack archives or validate checksums
- `size` is informational only
- no automatic dependency system or update API is implemented by the client

This app intentionally matches that contract exactly: administrative metadata is used to generate a static catalog and public files, and the public data is served without login.

## Architecture

- Flask app serves admin pages and public catalog/download endpoints.
- SQLite stores add-on records, metadata, and admin account state.
- On publish, the app writes a generated `content.json` and copies package files into a public directory atomically.
- Public catalog/download routes remain separate from admin routes.
- No database server, Redis, or frontend framework is required.

## Quick local dev setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export APP_SECRET_KEY=dev-secret
export PUBLIC_BASE_URL=http://localhost:8080
export PERSIST_ROOT=/tmp/quake-addons-data
export PUBLIC_ROOT=/tmp/quake-addons-public
export UPLOAD_FOLDER=/tmp/quake-addons-upload
python app.py
```

Then open:

- http://localhost:5000/setup
- then log in at http://localhost:5000/login

## Native Unraid deployment

This repo includes a native Unraid Docker template at `unraid-template.xml`.

The current image is published automatically to GitHub Container Registry:

```text
ghcr.io/kdmn2/quake-mods-server:latest
```

If GHCR still returns `unauthorized`, the package visibility may still need to be switched to public in GitHub Packages settings.

### Deploy from Unraid Docker GUI

1. Open Unraid and go to Docker > Add Container.
2. Import `unraid-template.xml` as a custom template.
3. Leave the repository as `ghcr.io/kdmn2/quake-mods-server:latest` unless you intentionally want to pin a specific release tag. If you pin, use a version tag such as `ghcr.io/kdmn2/quake-mods-server:v1.2.3`.
4. Set the host port (default `8080`) and the three persistent data paths:
   - `/mnt/user/appdata/ironwail-addons/data`
   - `/mnt/user/appdata/ironwail-addons/public`
   - `/mnt/user/appdata/ironwail-addons/uploads`
5. Set a strong `APP_SECRET_KEY`.
6. Set `PUBLIC_BASE_URL` to the externally reachable URL for your server, for example `https://addons.example.com`.
7. Keep the appdata mount writable by the container user. The database lives under that mount and startup will fail fast with a permissions diagnostic if it is not writable.
8. Click Apply.

### Pin a specific release tag

If you want a fixed version instead of the rolling `latest` image, change the repository value to a tag like:

```text
ghcr.io/kdmn2/quake-mods-server:v1.2.3
```

This is useful for stable deployments and controlled upgrade windows.

### 1) What the template deploys

The app container uses:

- image: `ghcr.io/kdmn2/quake-mods-server:latest`
- HTTP port: `5000` inside the container
- host Web UI port: configurable in the template (default `8080`)
- persistent data:
  - `/appdata` for SQLite and metadata
  - `/public` for generated catalog and downloads
  - `/uploads` for uploads and staging
- required env vars:
  - `APP_SECRET_KEY`
  - `PUBLIC_BASE_URL`
  - `PERSIST_ROOT=/appdata`
  - `PUBLIC_ROOT=/public`
  - `UPLOAD_FOLDER=/uploads`
  - `DB_PATH=/appdata/addons.sqlite3`

### 2) Create the persistent data root on Unraid

Example host path:

```text
/mnt/user/appdata/ironwail-addons
```

Create the subdirectories:

```bash
mkdir -p /mnt/user/appdata/ironwail-addons/data
mkdir -p /mnt/user/appdata/ironwail-addons/public
mkdir -p /mnt/user/appdata/ironwail-addons/uploads
```

The image runs as non-root `appuser` (uid/gid 1000 unless you override it), so the host bind paths must be writable by that user. Unraid creates shares as `nobody:users` (`99:100`); either `chown -R 99:100 /mnt/user/appdata/ironwail-addons` and add `--user 99:100` under Extra Parameters, or `chown` the directories to the uid/gid shown in the startup log. If first boot fails, the container exits with a message naming the directory, its owner and the container uid:gid; it never falls back to another location.

If you are using the native template, the appdata mount is exposed in the container as `/appdata`, not `/data`.

### 3) Unraid template import

Use the included native template as a custom template in Unraid:

1. Open Unraid Docker GUI.
2. Import the XML file from `unraid-template.xml`.
3. Select the app in the template UI.
4. Set:
   - App port (default `8080`)
   - AppData path
   - Public catalog path
   - Upload path
   - `APP_SECRET_KEY`
   - `PUBLIC_BASE_URL`
5. Click Apply.

### 4) First-run setup

After the container starts:

- open the WebUI at `http://<unraid-host>:<port>/login`
- if no admin exists yet, visit `http://<unraid-host>:<port>/setup`
- create the initial admin account
- sign in and manage add-ons

The app does not expose a public signup flow.

### Startup diagnostics

If the container fails to start, use the built-in doctor check inside the container:

```bash
python app.py doctor
```

It reports the resolved database path, runtime UID/GID, and basic read/write readiness checks without modifying data.

On normal startup, the container also prints a single JSON banner to Docker logs with the resolved paths and UID/GID to make Unraid troubleshooting easier.
If the banner still shows `/data`, that means you are running an older image or an overridden environment/config in the container settings.
Recreate the container or re-import the template if you still see `/data`; the supported persistent root is `/appdata`.

ZIP imports now also emit short `[import]` and `[zip-import]` log lines so you can tell whether the app used Python ZIP handling or the system `unzip` fallback.
The Docker image now bundles `unzip`, so ZIP compatibility does not depend on the host.

### Unraid troubleshooting

If the container fails before the Web UI appears, check the logs:

```bash
docker logs ironwail-addons
```

If the error mentions permissions on `/appdata`, confirm the bind mount is mounted read/write and owned by the same user the container runs as. The app will not silently switch to an in-memory database.

If you see a permissions failure, fix the host directory ownership/mode on the Unraid appdata path. Do not rely on the app to repair permissions automatically.

### 5) Reverse proxy and HTTPS

If you front the container with a reverse proxy, set `PUBLIC_BASE_URL` to the external HTTPS URL, for example:

```text
https://addons.example.com
```

The app itself does not terminate TLS; it expects your reverse proxy to handle it.

Upload size is limited by `MAX_CONTENT_LENGTH` (default 2 GiB). If you need larger archives, increase it in the container environment.

#### Large packages behind a reverse proxy (timeouts)

Converting or publishing a large package takes minutes, longer than most proxies keep a request open (nginx closes a request after 60 s of silence by default and answers `504 Gateway Time-out`). The app therefore never does that work inside a request: the upload POST only saves the file and starts a **background job**, and the browser polls a tiny status endpoint (`/jobs/<id>/status`, a few hundred bytes, answered instantly) to show a live progress bar. No single request is ever long-running, so no proxy setting is needed for the conversion, the optional re-encoding or the publish copy. Jobs keep running if you close the page; running jobs are listed on the dashboard.

What can still take long is the **upload itself** (a 500 MB ZIP over a slow link). Raise these limits on the proxy if uploads fail part-way:

```nginx
location / {
    proxy_pass http://ironwail-addons:5000;
    client_max_body_size      2g;        # at least MAX_CONTENT_LENGTH
    proxy_request_buffering   off;       # stream the upload to the app instead of spooling 2 GB to the proxy's disk first
    client_body_timeout       3600s;
    proxy_send_timeout        3600s;
    proxy_read_timeout        3600s;     # not needed for the conversion any more; harmless for slow uploads
}
```

* **Nginx Proxy Manager:** put the same lines (without `location` and `proxy_pass`) under *Advanced → Custom Nginx Configuration*.
* **Traefik:** raise `readTimeout`/`writeTimeout` of the entrypoint (`entryPoints.<name>.transport.respondingTimeouts`), and the `maxRequestBodyBytes` of the `buffering` middleware if you use it.
* **Caddy:** no body-size limit by default; `reverse_proxy` has no read timeout unless you set one.
* **Cloudflare (proxied):** request bodies are capped (100 MB on free plans), so large uploads cannot work through it. Use *Import from server path* on the add-on form (copy the ZIP to the host first), or upload over the LAN address.

The upload form shows real upload progress (percentage, speed, time left). Without JavaScript it still works as a plain form and the job page refreshes itself every few seconds. Set `JOB_MODE=inline` to run jobs inside the request instead (not recommended; it brings the timeout problem back).

If the container is restarted while a job is running, that job is marked failed on the next start and its temporary files are removed; nothing is half-published, because a package is only swapped into place after every step succeeded.

### 6) Portainer / Docker Compose compatibility

The repo also contains a Portainer-friendly Compose file at `docker-compose.portainer.yml` for manual deployments. It uses the same env vars and bind mounts as the Unraid template, but the image field is a placeholder that must be replaced with a real registry path before use.

If GHCR requires authentication, log in to `ghcr.io` with a GitHub PAT that has `read:packages` or switch the package visibility to public.

Example env values:

```yaml
APP_SECRET_KEY: "replace-with-long-random-secret"
PUBLIC_BASE_URL: "http://your-unraid-host:8080"
PERSIST_ROOT: "/appdata"
PUBLIC_ROOT: "/public"
UPLOAD_FOLDER: "/uploads"
PORT: "5000"
```

### 7) Update and rollback

- Keep the persistent directories unchanged across upgrades.
- Recreate the container with the newer image while keeping the same bind mounts.
- For rollback, redeploy the previous image and restore the same data directories.

## Upload, metadata, and publish workflow

1. Sign in to the admin GUI.
2. Click Upload.
3. Upload a Quake `.pak` archive or a `.zip` mod bundle.
   - ZIP releases are converted into one Quake PAK holding the effective game assets (see "ZIP release conversion" below).
4. Review the metadata:
   - ID
   - title
   - game dir
   - author
   - date
   - description
5. Save as a draft.
6. Publish when ready.
7. The app updates `content.json` and copies the package into the public add-on directory.

## Ironwail client configuration

Launch Ironwail with the exact server URL:

```bash
ironwail -addons http://<host>:8080
```

or with HTTPS if you proxy in front:

```bash
ironwail -addons https://addons.example.com
```

The catalog is available at:

```text
<base>/content.json
```

and package downloads are served at:

```text
<base>/public/addons/<addon-id>.pak
```

## Data persistence and backup

Persist and back up the following directories:

- `/mnt/user/appdata/ironwail-addons/data`
- `/mnt/user/appdata/ironwail-addons/public`
- `/mnt/user/appdata/ironwail-addons/uploads`

Recommended backup procedure:

```bash
rsync -a /mnt/user/appdata/ironwail-addons/ /backup/ironwail-addons/
```

To restore:

- stop the container
- restore the three persistent directories to the same paths
- start the container again

## Update and rollback

- Keep the persistent directories unchanged across upgrades.
- Recreate the container from a newer image while keeping the bind mounts.
- If the app schema changes, make a migration plan before deploying a new image.
- For rollback, redeploy the previous image and restore the previous persistent directories.

## Testing

Automated tests are in `tests/` and cover:

- catalog generation
- file validation
- publish/unpublish behavior
- admin auth basics
- public catalog generation

Run:

```bash
pytest -q
```

## Known limitations

- This app is not a general mod portal; it is specifically a browser-managed catalog generator for Ironwail.
- The client itself does not support automatic update detection beyond what is implemented in its static list.
- The application does not bundle or redistribute Quake base-game content.
- Archive compatibility depends on the actual Quake package layout; the app validates the archive but does not silently repack unsupported formats.
- ZIP members that Python's `zipfile` cannot decode (for example Deflate64, which many Windows archivers use for large archives) are read through the bundled system `unzip`, one member at a time, with size checks. If `unzip` is missing the upload is rejected with a clear error.
- A single PAK holds at most 2048 entries and entry names are limited to 55 bytes (Ironwail `MAX_FILES_IN_PACK` / `dpackfile_t.name`). Releases that resolve to more files, or longer names, are rejected rather than truncated. The PAK is also limited to just under 2 GiB (32-bit offsets).
- A converted release contains everything that is in the ZIP (sources, images and documentation included); nothing is silently dropped. Large releases therefore produce large downloads.
- The app’s public side is intentionally static; no database or API is exposed to the public.

## ZIP release conversion

Root inference is decided from the member paths only; file inclusion is decided separately.

- **rooted**: every top-level directory is a Quake content directory (`maps`, `progs`, `sound`, `music`, `gfx`, `models`, ...) or an auxiliary one (`docs`, `src`, `images`, ...). Paths are kept as they are. Root-level files such as `pun.txt` are kept.
- **wrapper**: exactly one other top-level directory and no root-level `pakN.pak`/`progs.dat`/content directory. That directory is stripped. Root-level documentation beside it is kept.
- **ambiguous** (rejected): several candidate mod roots, or a mod root mixed with game content beside it, or a wrapper inside a wrapper.
- `__MACOSX/` resource forks are ignored; symlinks, encrypted members, duplicate member names, `..`/absolute paths and names that differ only by case are rejected.

Which copy of a file wins follows Ironwail's own search order (`common.c`: `COM_AddGameDirectory`, `COM_FindFile`): `pakN ... pak1, pak0`, then loose files. So a higher-numbered PAK beats a lower one, any PAK beats a loose file, and the first of two identical names inside one PAK wins. ZIP member order never matters. Only `pak0.pak`, `pak1.pak`, ... at the release root count as PAKs (Ironwail stops at the first missing number, so gaps are rejected); other `.pak` files are stored as ordinary files. Output entries are sorted by path.

## Batch import

**Batch import** (top menu) brings in many packages at once, from two sources. Both feed the *same* pipeline as a single upload: format detection, ZIP/PAK validation, root inference, asset precedence, conversion, output validation and inspection. Nothing is converted or published by a second code path, and **every successful item becomes a draft**. Publishing stays an explicit click on each draft.

```text
choose sources -> validate -> queue -> acquire into staging -> existing conversion -> validate the PAK -> draft
```

### 1. Remote URLs

Paste one direct download link per line (HTTPS preferred; at most `BATCH_MAX_ITEMS` per batch). Blank lines and spaces are ignored, repeated links are imported once, and a list with any malformed line is rejected as a whole, with the line numbers, before anything is queued. Links need not end in `.zip`: a signed or extension-less download URL works, because the content, not the name, decides what the file is.

Not supported, by design: web pages (an HTML answer is refused with "a direct download link is required"), logins, cookies, custom headers, scraping, links found inside pages, and `file:`/`ftp:` or local paths. For private or LAN sources use the server-folder import below.

**Network safety.** Every link, DNS answer, redirect and downloaded byte is treated as hostile.

* Only public internet destinations: loopback, private, link-local, carrier-grade NAT, multicast, reserved and cloud-metadata ranges (IPv4 and IPv6, including `::ffff:127.0.0.1`, NAT64 and 6to4 forms) are refused. All answers for a name must be public (a mixed answer is refused). The connection is made to one of exactly those validated addresses, the name is not resolved again (no DNS-rebinding window), and the peer address is verified after connecting.
* Addresses must be written canonically (`2130706433`, `0x7f.1`, `127.1`, `0177.0.0.1` are rejected as ambiguous); credentials in URLs are rejected; there is no setting that turns these checks off.
* Every redirect target goes through the same checks. At most `BATCH_MAX_REDIRECTS`; loops and HTTPS-to-HTTP downgrades are refused. No cookies, `Authorization` or other headers are ever sent or forwarded: the request carries only fixed headers.
* TLS certificates and host names are verified; a failed verification is never retried. Proxy environment variables are ignored.
* Limits are enforced while streaming, never from `Content-Length`: bytes received (`BATCH_MAX_DOWNLOAD_BYTES`), bytes after HTTP content-decoding (a compressed bomb is cut off), connect time, silence between reads, total time per attempt, and the staging area as a whole. Downloaded bytes are streamed to disk, never held in memory. These HTTP limits are separate from the archive limits (file count and uncompressed size) applied during conversion.
* Only retryable problems are retried (timeouts, dropped connections, HTTP 408/425/429/5xx, temporary DNS failures), up to `BATCH_MAX_ATTEMPTS` with exponential backoff, honouring a valid `Retry-After` up to `BATCH_RETRY_AFTER_MAX`. Unsafe destinations, 403/404/410, failed TLS, HTML and invalid packages are never retried. Downloads are not resumed: a retry starts the file again.

**Privacy of links.** Signed links often contain secrets. Pages, job messages, errors and logs show only the host and file name (`https://cdn.example.org/…/mod.zip (signed/query link)`); the Referer header is suppressed on every page. The full URL is stored in the database (`/appdata/addons.sqlite3`, never served) only while it may be needed to retry or to import again, is cleared as soon as the item completes, and is deleted with the batch after `BATCH_RETENTION_DAYS`. Treat the database file like any other credential store. Only the host, sanitised file name, time and SHA-256 are recorded on the draft as provenance.

### 2. Import from server folder

This lists packages in a folder on the **server / container**, not on the computer running your browser. It works only on folders the administrator configured:

* **Docker / Portainer:** mount a host folder read-only at `/imports`, e.g. `- /mnt/user/mods-to-import:/imports:ro`. If `/imports` exists the app uses it automatically; `IMPORT_ROOTS` (container paths separated by `:`) is only needed for other locations.
* **Unraid:** fill in *Import folder (optional, read-only)* in the template. Leave it empty if you do not need it; nothing else changes.
* The container user (uid 1000 by default, or the `--user` you set) must be able to read the folder. The app never changes ownership or permissions, needs no privileged mode and no Docker socket.

The flow is **scan, select, confirm**. Scanning only lists files (non-recursive by default; optionally subfolders up to `IMPORT_SCAN_MAX_DEPTH`; ZIP files, case-insensitive, `.pak` only if you tick it). It does not open or hash any file. The list shows relative path, size, whether the file is eligible, whether it was imported or queued before (and whether it changed since), with select-all/clear and pagination; you choose files (or "all eligible") and confirm. Nothing is imported until then.

**Safety.** Only configured roots are reachable; a request can name a root by number and a *relative* path, never an absolute one. Paths are opened component by component without following symbolic links (a symlink anywhere, including one swapped in after the scan, is refused and cannot reach outside the folder); FIFOs, sockets and devices are never opened; empty and oversized files are marked ineligible. Source files are only ever opened read-only: they are copied into the writable staging area and are never moved, renamed, deleted or modified. Between scan and import the file's size, modification time, inode and device are compared with the listing, and again after copying, so a replaced or still-growing file is reported ("changed since the folder was scanned; scan again") instead of being imported half-way. Unreadable subfolders and unavailable mounts are reported without failing the scan.

The single-file *Import from server path* option on the add-on form is unchanged: it accepts any absolute path the application user can read and is meant for the administrator only.

### Jobs, progress and recovery

* Each package is its own item with its own state: *queued, fetching, converting, needs review, needs a decision, draft created, skipped, failed, cancelled, interrupted*. One failure never stops the others. The batch page shows source type, a safe description, progress (a percentage only where the total is known), the draft link and expandable errors, and keeps working after you close or refresh the page.
* Work runs in two fixed-size lanes shared by **every** import source (browser uploads, publishing, URL batches, folder batches): `BATCH_DOWNLOAD_WORKERS` downloads/copies at once (default 2) and one conversion/publish at a time. A slow remote therefore never blocks conversions or publishing.
* Per-item **Cancel** (and *Cancel all remaining*) removes only that item's staged files. A download that is stalled ends at the latest after `BATCH_READ_TIMEOUT` seconds of server silence. **Retry** and *Retry all failed* re-run an item from the start; a double click or a second tab cannot start it twice, and the database allows only one draft per item, so a retry (even after a crash right after the draft was saved) cannot create a duplicate.
* **After a restart**, items that were queued or in flight are marked *interrupted* and their partial staging files are removed. Nothing is retried automatically: press Retry. Items waiting for review or a decision keep their staged package and survive the restart.
* **When a package needs a person:** a release the converter cannot resolve by itself (ambiguous root, too many entries) becomes *needs review* with a link to the normal review page, which builds from the already-downloaded file. A conflict becomes *needs a decision*. Nothing is guessed or overwritten.

| Situation | What happens |
| --- | --- |
| Same link twice in one list | Imported once |
| Same bytes already imported (any name or source) | *Skipped*, linked to the existing add-on; *Import anyway* creates a separate draft |
| Different bytes convert to a package identical to an existing add-on | *Skipped* (nothing installed); *Import anyway* available |
| Add-on ID / directory already exists | *Needs a decision*: separate draft with a new ID, review as a replacement, or skip |
| Same link now serves different content | *Needs a decision*, with replacement suggested |
| Replacement | Goes through the review page; the add-on's package changes, the **published copy keeps being served until you publish again**; a failed replacement leaves the published version untouched |

Drafts get an ID, directory and title derived from the file name (`pungfx.zip` becomes `pungfx`); the add-on page's metadata suggestions can improve them.

### Configuration

All optional; defaults shown. Set them as container environment variables (Unraid shows the most useful ones under *advanced*).

| Variable | Default | Meaning |
| --- | --- | --- |
| `IMPORT_ROOTS` | `/imports` if mounted | Container paths offered for folder import, separated by `:` |
| `IMPORT_SCAN_MAX_DEPTH` / `IMPORT_SCAN_MAX_ENTRIES` / `IMPORT_SCAN_MAX_RESULTS` | 8 / 20000 / 2000 | Scan bounds (depth, entries visited, files listed) |
| `IMPORT_PAGE_SIZE` | 100 | Files per page in scan results |
| `BATCH_MAX_ITEMS` | 50 | Links or files per batch |
| `BATCH_MAX_PENDING` | 200 | Items queued or running at once, across batches |
| `BATCH_MAX_URL_LENGTH` | 2048 | Longest accepted link |
| `BATCH_MAX_DOWNLOAD_BYTES` | `MAX_CONTENT_LENGTH` (2 GiB) | Largest package per item |
| `BATCH_STAGING_LIMIT_GIB` | 20 | Total of staged packages waiting for conversion |
| `BATCH_DOWNLOAD_WORKERS` | 2 | Simultaneous downloads/copies |
| `BATCH_CONNECT_TIMEOUT` / `BATCH_READ_TIMEOUT` / `BATCH_MAX_DURATION` | 10 s / 30 s / 3600 s | Connect, silence-between-reads and per-attempt time limits |
| `BATCH_MAX_REDIRECTS` | 5 | Redirects followed |
| `BATCH_MAX_ATTEMPTS` / `BATCH_RETRY_BASE` / `BATCH_RETRY_AFTER_MAX` | 3 / 2 s / 120 s | Download attempts, backoff base, longest `Retry-After` honoured |
| `BATCH_RETENTION_DAYS` | 14 | How long finished batches (and any stored links) are kept |

### Storage, backup and limits

* Batch state (batches, items, scans, jobs) lives in the existing SQLite database in `/appdata`, so backing up `/appdata` covers it; restoring an older copy simply shows older batch history. Staged downloads and copies live under `/uploads` and are temporary: do not back them up. Leftovers from an interrupted run are removed at the next start. The database holds stored links (see *Privacy of links*).
* Not included, deliberately: scraping or following links in pages, watch folders, scheduled imports, deleting or moving source files, mounting remote shares, automatic publishing, and private/LAN URL imports (use a mounted folder for those).
* Known limits: conversions still need free space (see the preview's disk warning); a very slow trickle from a server is bounded by the time limit rather than detected as "stalled"; folder items whose files are being replaced often will report "changed" until the folder is stable.

## Conversion options (all opt-in)

Tick **Review the conversion before building** on the upload form (on by default) to see what a ZIP would produce before anything is built: root detection, file count against Ironwail's 2048-entry limit, estimated size, which files would be dropped by each option, and a free-disk-space check. Nothing below is applied unless you switch it on, and every use is recorded with the add-on (see "How this package was built"). With no options the output is byte-for-byte the plain conversion.

* **Root folder.** Pick which top-level folder is the mod when a ZIP holds several (otherwise rejected as ambiguous); everything outside it is left out and listed.
* **Leave out files Ironwail does not need.** Presets for map/QuakeC sources and editor files, operating-system leftovers, and root-level documentation, plus your own patterns (`*.map`, `mapsrc/`, `/*.txt` for root only; case-insensitive). Applied to loose files and to the contents of embedded PAKs, before duplicate resolution. Useful to get under the 2048-entry limit or to shrink downloads. The preview warns if a pattern would remove file types Ironwail reads.
* **Lossless TGA → PNG re-encoding** (textures and skybox faces separately). Ironwail loads `.png` before `.tga` and always decodes to RGBA with the same library, so identical pixels are indistinguishable in game. The TGA is decoded by a small decoder written to follow `stb_image` (not Pillow), the PNG is decoded again and compared byte for byte, and anything that cannot be proven identical or would not get smaller stays a TGA. Only 24/32-bit truecolor TGAs (raw or RLE) are converted. On the real packages tested, all 133 converted images decoded identically in Ironwail's own `stb_image`, at about 12% of the original size. Side effects: the package bytes change (and depend on the Pillow/zlib versions, which are recorded); Ironwail's `sky <TAB>` console completion lists only TGA skyboxes, so converted skies are not suggested there (they still load normally).
* **Add a mapdb.json.** Only if the release has none; contains just the episode name and directory so the installed add-on is listed with the same name.
* **Keep the uploaded ZIP.** Stored under `/appdata/sources` so the add-on page can offer **Rebuild with different options** without another upload. Removed when the add-on is deleted.

## Package inspection (advisory)

The add-on page shows a read-only analysis of the stored PAK, based on how Ironwail reads it: the maps the add-on menu will list (menu group, worldspawn title, BSP version, skybox, `.lit`, CD track), contents by type, and a record of how a ZIP was converted (source SHA-256, root mapping, wrapper, which copy of any duplicated path was kept). Findings are warnings (Ironwail will visibly ignore or reject something: an invalid `.lit`, a partial skybox, an unsupported BSP version, a `progs.dat` Ironwail refuses, an inert `descript.ion`) or notes. They never block an upload or change the package.

Besides the checks above, model and sprite files are checked for the header Ironwail needs (a wrong version stops the engine with a fatal error when the model loads) and `sound/*.wav` files for a format Ironwail can play (plain 8/16-bit mono or stereo PCM; others are silently skipped by the engine).

Title, author, date and description can be suggested from `mapdb.json` (using Ironwail's own rules, including ignoring an unmodified Copper `mapdb.json`) or from a root text file that starts with a `Title :` field. Nothing is applied until you press Apply for that field.

## File ownership and permissions

- The application runs as non-root `appuser` (uid/gid are printed in the startup log) and never chmods, chowns, relocates or recreates existing data.
- The image defaults to `PERSIST_ROOT=/appdata`, `DB_PATH=/appdata/addons.sqlite3`, `PUBLIC_ROOT=/public`, `UPLOAD_FOLDER=/uploads`; map host directories onto those three paths.
- Startup probes each location by creating a real file and reports missing path, not-a-directory, read-only mount, permission denied (with the directory's owner and the container uid:gid), no space/quota, locked database and corrupt database as separate messages, then exits non-zero. There is no fallback database location. `docker run ... python app.py doctor` prints the same diagnosis.
- A warning is logged if `PERSIST_ROOT` or `PUBLIC_ROOT` is not a mount point, because data would then be lost when the container is recreated.
- Unraid shares are normally owned by `nobody:users` (`99:100`). Either `chown 99:100` the three host directories, or run the container as that user (`--user 99:100`, in the Unraid template under Extra Parameters; `user: "99:100"` in Compose). Do not use `chmod 777` or `--privileged`.
- Do not expose the full host filesystem or the working directory as a public web root.

## Not yet implemented / intentionally out of scope

- no SSO or external auth providers
- no multi-tenant or fine-grained role system
- no automatic sidecar worker processes for long-running archive validation
- no client-side image metadata or art upload beyond the verified protocol requirement
