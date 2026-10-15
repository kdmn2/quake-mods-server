# Unraid template

This directory contains a native Unraid Docker XML template for the project.

Files:
- `ironwail-addons.xml`

## Template status

The template is based on the current repository's Dockerfile and app configuration. It is ready for deployment as a custom template, but it assumes you have already built and published the image, or you are installing from a registry that is reachable by the Unraid host.

This repository does not currently include a published Docker Hub or GHCR image; the `Repository` field in the template must be replaced with the actual published registry path before use if the default value is not yet available.

## Required user changes before Apply

- Replace the image repository if it is not published yet.
- Change `APP_SECRET_KEY` from the placeholder value.
- Set `PUBLIC_BASE_URL` to the externally reachable URL behind your reverse proxy or direct host access.
- Confirm the host AppData paths match your actual Unraid `appdata` location.
- Ensure the host port is not already in use.

## Installing as a custom template

1. In Unraid, go to Docker > Add Container > Template > Manage Templates.
2. Choose the local XML or add the repository containing the template.
3. Import `ironwail-addons.xml`.
4. Set the image repository and image tag.
5. Set port mapping and data paths.
6. Save and apply.

## First-run setup

1. Open the app in the browser via the WebUI URL.
2. Visit `/setup` if no user exists yet.
3. Create the first administrator account.
4. Sign in and upload mods.
5. Publish the add-on to generate the public catalog.

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
- `/public` for generated catalog and package downloads
- `/uploads` for staged uploads

## Notes

- This project does not implement PUID/PGID automatically; the container image runs as non-root `appuser`.
- Make sure the host directories are writable by the container user.
- The application does not manage TLS; use an existing reverse proxy if you want HTTPS.
