# Unraid template

This directory contains a native Unraid Docker XML template for the project.

Files:
- `ironwail-addons.xml`

## Template status

The template is based on the current repository's Dockerfile and app configuration. It is ready for deployment as a native Unraid custom template and it uses the GitHub Actions-published image from GHCR:

```text
ghcr.io/kdmn2/quake-mods-server:latest
```

This means the image is rebuilt and pushed automatically when the repository changes, and the `latest` tag is refreshed from the default branch build.

## Required user changes before Apply

- Change `APP_SECRET_KEY` from the placeholder value.
- Set `PUBLIC_BASE_URL` to the externally reachable URL behind your reverse proxy or direct host access.
- Confirm the host AppData paths match your actual Unraid `appdata` location.
- Ensure the host port is not already in use.

## Pinning a specific release tag

The default template points at the rolling `latest` tag. If you want a fixed version instead, change the template repository value to a specific release tag, for example:

```text
ghcr.io/kdmn2/quake-mods-server:v1.2.3
```

This is useful when you want to keep a known-good version stable while evaluating an upgrade.

## Installing as a custom template

1. In Unraid, open Docker > Add Container.
2. Import `unraid/templates/ironwail-addons.xml` as a custom template.
3. Review the default host port and data paths.
4. Set the app secret and public base URL.
5. Click Apply.

## First-run setup

1. Open the app in the browser via the WebUI URL.
2. Visit `/setup` if no admin account exists yet.
3. Create the first administrator account.
4. Sign in and upload mods.
5. Publish a mod to generate the public catalog.

## Data locations

Persistent data is expected under the host appdata root, for example:

```text
/mnt/user/appdata/ironwail-addons/
├── data/
├── public/
└── uploads/
```

The application binds to:
- `/data` for the SQLite database and metadata
- `/public` for the generated catalog and package download folder
- `/uploads` for staged uploads

## Notes

- This project does not implement PUID/PGID automatically; the container image runs as non-root `appuser`.
- Make sure the host directories are writable by the container user.
- The application does not manage TLS; use an existing reverse proxy if you want HTTPS.
- The template is for native Unraid deployment; it is not the same as a Community Applications listing.
