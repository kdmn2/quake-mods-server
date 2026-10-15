# Ironwail Add-on Manager

This project provides a lightweight self-hosted admin workflow for an Ironwail-compatible Quake add-on catalog. It keeps the public contract compatible with Ironwail’s verified source at `v0.8.2`, while adding a browser-based admin interface for uploads, metadata, publishing, and catalog generation.

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
- On publish, the app writes a generated `content.json` and copies package files into a public directory.
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

## Portainer / Unraid deployment

### 1) Build image locally or in your registry

Example local build:

```bash
docker build -t ironwail-addons:local .
```

To deploy via Portainer, push the image to a registry the Portainer node can reach, for example:

```bash
docker tag ironwail-addons:local your-registry.example.com/ironwail-addons:latest
docker push your-registry.example.com/ironwail-addons:latest
```

### 2) Create the persistent data root on Unraid

Example host path:

```text
/mnt/user/appdata/ironwail-addons
```

Create subdirectories:

```bash
mkdir -p /mnt/user/appdata/ironwail-addons/data
mkdir -p /mnt/user/appdata/ironwail-addons/public
mkdir -p /mnt/user/appdata/ironwail-addons/uploads
```

Set ownership and permissions appropriate for the container user. The image runs as non-root `appuser`, so it needs write access to the mounted directories. If you run a Docker host without UID/GID mapping, the simplest approach is to set the host directories to a UID/GID that matches the container user, or make them writable to a matching group.

Example shell commands on Unraid:

```bash
chown -R 1000:1000 /mnt/user/appdata/ironwail-addons
chmod -R 755 /mnt/user/appdata/ironwail-addons
```

If your Unraid setup supports custom `user:group` mapping, align those values to the container user. The image does not claim PUID/PGID support, so do not assume automatic user remapping.

### 3) Portainer stack

Use the included `docker-compose.portainer.yml` as a base. Update the image name and public URL, then deploy as a Portainer stack.

```yaml
version: '3.9'
services:
  ironwail-addons:
    image: your-registry.example.com/ironwail-addons:latest
    restart: unless-stopped
    ports:
      - "8080:5000"
    environment:
      APP_SECRET_KEY: "replace-with-long-random-secret"
      PUBLIC_BASE_URL: "http://your-unraid-host:8080"
      PERSIST_ROOT: "/data"
      PUBLIC_ROOT: "/public"
      UPLOAD_FOLDER: "/uploads"
      PORT: "5000"
    volumes:
      - /mnt/user/appdata/ironwail-addons/data:/data
      - /mnt/user/appdata/ironwail-addons/public:/public
      - /mnt/user/appdata/ironwail-addons/uploads:/uploads
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health').read()"]
      interval: 20s
      timeout: 5s
      retries: 5
      start_period: 15s
```

### 4) Reverse proxy and HTTPS

If you already have a reverse proxy in front of Unraid, do the following:

- proxy `https://addons.example.com` to the app port (for example `http://your-unraid-host:8080`)
- set `PUBLIC_BASE_URL` to the externally reachable https URL, e.g. `https://addons.example.com`
- the app should be configured behind a trusted proxy if your reverse proxy terminates TLS
- no TLS certificates are managed by this app

### 5) First-run admin setup

Open the app in a browser.

- first visit: `http://<host>:8080/setup`
- create the administrator account
- then sign in at `http://<host>:8080/login`

The application does not expose a public signup flow.

## Upload, metadata, and publish workflow

1. Sign in to the admin GUI.
2. Click Upload.
3. Upload a Quake archive.
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
- The app’s public side is intentionally static; no database or API is exposed to the public.

## File ownership and permissions

- The application runs as non-root `appuser` in the image.
- The mounted data directories must be writable by the container user.
- Do not expose the full host filesystem or the working directory as a public web root.

## Not yet implemented / intentionally out of scope

- full CSRF protection is not yet hardened beyond the base Flask session pattern
- no SSO or external auth providers
- no multi-tenant or fine-grained role system
- no automatic sidecar worker processes for long-running archive validation
- no client-side image metadata or art upload beyond the verified protocol requirement
