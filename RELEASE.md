# Release checklist

Use this when you want to publish a stable versioned image for Unraid or other deployments.

1. Make sure the repo is clean and the code is ready.
2. Run the test suite locally.
3. Create a git tag:
   ```bash
   git tag v1.2.3
   git push origin v1.2.3
   ```
4. GitHub Actions will build and push:
   - `ghcr.io/kdmn2/quake-mods-server:v1.2.3`
   - `ghcr.io/kdmn2/quake-mods-server:latest`
5. Update the Unraid template or your deployment if you want to pin to the specific version tag instead of `latest`.
6. Validate the app and confirm the catalog still loads from the production URL.

Recommended practice:
- keep `latest` for the current default-branch build
- use a tagged version for production stability
- only pin a version when you want full control of the deployment version
