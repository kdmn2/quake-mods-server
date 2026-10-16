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

The image runs as non-root `appuser`, so the host bind paths must be writable by the container user. On Unraid, the simplest safe setup is to make those directories writable for the correct UID/GID, or use a mapping that matches the container runtime user. If first boot still fails because the mounted path is owned by root, fix the host permissions or pre-create the bind-mount directories with writable ownership before starting the container.

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
   - ZIP uploads are safely repacked into a Quake PAK before publication.
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
- ZIP uploads are repacked by reading each entry through Python’s ZIP reader; if Python cannot decode a ZIP compression method, the upload is rejected with a clear error.
- The app’s public side is intentionally static; no database or API is exposed to the public.

## File ownership and permissions

- The application runs as non-root `appuser` in the image.
- The mounted data directories must be writable by the container user.
- Do not expose the full host filesystem or the working directory as a public web root.

## Not yet implemented / intentionally out of scope

- no SSO or external auth providers
- no multi-tenant or fine-grained role system
- no automatic sidecar worker processes for long-running archive validation
- no client-side image metadata or art upload beyond the verified protocol requirement
