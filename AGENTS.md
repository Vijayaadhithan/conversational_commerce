# Deployment workflow

- For authorized deployments, make and validate changes locally, commit them, and
  push to `origin/main` before updating the server.
- On the server, use the Git checkout at
  `/opt/commerce/conversational_commerce` and `git pull --ff-only origin main`.
  Do not use copied source files or release archives for normal deployments.
- Inspect local and server worktrees before pulling. Preserve unexpected changes;
  do not force-reset or delete them to make a pull succeed.
- Keep private environment files, credentials, data, models and machine-specific
  deployment files outside Git. Never commit credentials.
- When runtime code changes, preserve the current image and required recovery
  backups, build the API from the pulled checkout, then recreate only the API.
  Verify source/commit identity and health. A Git pull alone does not update an
  already running Docker container.
- Report the commit and verify that local, origin and server heads match. For a
  push/sync-only request, avoid unnecessary model calls, tests or service restarts.
