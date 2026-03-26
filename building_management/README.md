# Building Management UI

Responsive Django/Tailwind UI for managing buildings, units, and work orders.

Role-based access control (RBAC) is described in [docs/rbac.md](docs/rbac.md).
Logging and timing instrumentation guidelines live in [docs/logging.md](docs/logging.md), and the release QA checklist is tracked in [docs/qa_checklist.md](docs/qa_checklist.md).
Budget workflow product notes, API surfaces, and rollout guidance live in [docs/budgets.md](docs/budgets.md).

## System Office Building

The application now ships with a singleton **Office** building that acts as the shared workspace for administrators and backoffice employees. It is enforced in the data model through `Building.is_system_default`, cannot contain units, and its work orders are visible to every administrator/backoffice user.

- A schema/data migration auto-creates the Office record (or upgrades an existing one) and purges units accidentally linked to it.
- Use `python manage.py ensure_office_building` whenever you deploy or rotate staff. The command re-syncs the canonical owner, removes stray units, and provisions memberships for every ADMINISTRATOR/BACKOFFICE user plus owner-level (technician) memberships for all administrator accounts.
- Control the canonical owner and copy via env vars or flags: `DJANGO_OFFICE_OWNER_USERNAME` / `DJANGO_OFFICE_OWNER_EMAIL` pick the owner account, `DJANGO_OFFICE_BUILDING_NAME`, `DJANGO_OFFICE_BUILDING_ADDRESS`, and `DJANGO_OFFICE_BUILDING_DESCRIPTION` customise the record. Override on demand with `--owner`, `--name`, etc.
- Owner rotation runbook: set the desired username/email (env or flag) and run `python manage.py ensure_office_building`. The command updates the FK owner, regenerates owner memberships for every administrator, and keeps backoffice memberships in sync.
- Office work orders are always created inside the Office building and must be forwarded to a destination. The work-order form pins the Office entry at the top, disables units when Office is selected, and exposes a dedicated forwarding panel with owner previews. Forwarded tickets stay visible to the Office owner/backoffice plus the destination owner/backoffice/technicians; confidentiality (`lawyer_only`) still applies as usual.
- See [docs/office_forwarding.md](docs/office_forwarding.md) for the full forwarding runbook (how to forward, re-route, deal with deleted destinations, and recover from mis-forwarding).
- Django automatically re-syncs the Office building after migrations and whenever administrator/backoffice users load building data, so the singleton is recreated even if it’s deleted. You can still run `python manage.py ensure_office_building` manually whenever you need to rotate the owner.

## Dependencies

The Python stack stays lean but now ships with everything needed for production:
`Django`, `psycopg[binary]`, `django-markdownify`, `Whitenoise`, and `Gunicorn`.  
PostgreSQL is recommended for production, but the project falls back to SQLite when
`DATABASE_URL` is not provided so you can run management commands out of the box.

## Development Workflow

1. **Install tooling**
   ```bash
   pip install -r requirements.txt
   npm install
   npm run build:css  # generates static/css/dist.css
   ```
2. **Database / environment**
   - Copy `.env.example` to `.env` and adjust as needed.
   - Optional: run `docker compose up -d postgres` to start PostgreSQL (credentials come from the env file or defaults).
3. **Tailwind watch mode** (during template work)
   ```bash
   npm run watch:css
   ```
4. **Run Django (development)**
   ```bash
   python manage.py runserver
   ```

### Auto-fixing the Core Schema (development only)

Set `DJANGO_AUTO_FIX_CORE_SCHEMA=1` in your `.env` **only when `DEBUG=True`** to let
`EnsureCoreSchemaMiddleware` verify tables/columns on the first request and run
`python manage.py migrate` automatically if something is missing. In production
the middleware will never run migrations; instead it logs a clear error instructing
operators to run `python manage.py migrate` manually. Keeping this safeguard
dev-only prevents unintended schema changes during live traffic.

### UI tokens & components

The shared typography scale, spacing tokens, and button/panel utilities live
in `static/css/theme-overrides.css`. See `docs/ui.md` for a quick reference on
`.ui-panel`, `.ui-card`, `.status-chip`, and `btn` variants so new templates
stay consistent across the app.

## Production Quickstart

1. Install dependencies and build static assets (once per deploy):
   ```bash
   pip install -r requirements.txt
   npm install
   npm run build:css
   python manage.py collectstatic --noinput
   ```
2. Configure environment variables (examples):
   ```bash
   export DJANGO_SECRET_KEY="change-me"
   export DJANGO_ALLOWED_HOSTS="example.com"
   export DJANGO_SECURE_SSL_REDIRECT=1
   export DJANGO_CSRF_TRUSTED_ORIGINS="https://example.com,https://www.example.com"
   ```
3. Launch the application with Gunicorn (uses `gunicorn.conf.py` by default):
   ```bash
   gunicorn -c gunicorn.conf.py building_mgmt.wsgi:application
   ```

Gunicorn listens on `0.0.0.0:8000` by default; place a reverse proxy such as nginx in front of it. 
Whitenoise serves collected static files directly from Gunicorn, simplifying the deployment story
for smaller installations.

### Scheduled Jobs

- **Daily notification sync**: run `python manage.py sync_notifications` once per day (e.g., via cron `0 5 * * *`).
  This refreshes work-order deadline alerts, clears expired snoozes, prunes acknowledged alerts older than 30 days,
  and removes stale records so users continue to receive daily reminders for unfinished tasks. The command is
  idempotent and safe to run more often if desired.

Tailwind is configured with purge-aware `content` globs so unused utilities are removed in the production bundle.

## Internationalization

Django’s translation tooling is enabled for English (`en`) and Bulgarian (`bg`).

1. Add/refresh message catalogs:
   ```bash
   export DATABASE_URL=postgres://building_mgmt:building_mgmt@localhost:5432/building_mgmt  # required for settings import
   python manage.py makemessages -l en -l bg
   ```
2. Edit the generated `locale/<lang>/LC_MESSAGES/django.po` files.
3. Compile compiled catalogs:
   ```bash
   python manage.py compilemessages
   ```

The language switcher can use Django’s built-in `set_language` view (POST to `/i18n/setlang/` with `language=en` or `language=bg`).

For in-browser translation editing, install [django-rosetta](https://github.com/mbi/django-rosetta) and point staff users to `/rosetta/` (see “i18n Tooling” below).

### CI/Automation

- Lint `.po` files with `python manage.py makemessages --check` (fails on syntax errors)
- Ensure compiled catalogs are up-to-date before deployment (`python manage.py compilemessages --check`)
Add these commands to your CI job after installing dependencies.

## Database Configuration

PostgreSQL is recommended for production deployments. For local development:

1. Launch the database (uses `docker-compose.yml`):
   ```bash
   docker compose up -d postgres
   ```
   The first run provisions the `building_mgmt` user, password, and database.
2. Install Python requirements (includes the `psycopg` driver) and run migrations:
   ```bash
   pip install -r requirements.txt
   export DATABASE_URL=postgres://building_mgmt:building_mgmt@localhost:5432/building_mgmt
   python manage.py migrate
   ```
   Stop the container with `docker compose down` when you're done.

If `DATABASE_URL` is omitted the app will automatically use a local SQLite database located at
`./db.sqlite3`, which is handy for quick experiments and automated test environments.

Prefer a one-off container instead of compose? Run `docker run` with the same env vars/port mapping.

For a full step-by-step setup (local Docker and external PostgreSQL), see `docs/postgres_connection_guide.md`.

Optional env vars:

- `DJANGO_DB_CONN_MAX_AGE` – persistent connection lifetime in seconds (default `60`).
- `DJANGO_DB_CONN_HEALTH_CHECKS` – defaults to `true` when pooling is enabled; override with `false` to disable.
- `DJANGO_DB_SSLMODE` – override the default SSL mode (`require` for non-local hosts, unset for localhost).
- `DJANGO_DB_APP_NAME` – registers a custom `application_name` with Postgres for monitoring.

## File Storage & Attachments

Work order attachments persist under `MEDIA_ROOT` (default `./media/`) with URLs served from `MEDIA_URL` (default `/media/`). Local storage works out of the box; switch to S3 by installing `django-storages[boto3]` and setting `DJANGO_FILE_STORAGE=s3`.

### Local development quickstart

1. Create the media directory (`mkdir -p media`) and ensure your user can write to it.
2. Run the dev server and open any work order detail page.
3. Use the **Upload files** button to add images or documents; uploads stream via the attachment API and store under `media/work_orders/<order-id>/`.
4. Delete attachments from the detail page or admin inline to keep the directory tidy. Git should continue to ignore everything under `media/`.

When `DEFAULT_FILE_STORAGE` is set to S3 the same UI applies; credentials and the bucket policy must allow `PutObject`, `GetObject`, and `DeleteObject` for the configured prefix.

### Key environment variables

- `DJANGO_MEDIA_ROOT` / `DJANGO_MEDIA_URL` – override the media filesystem path or public URL.
- `DJANGO_FILE_STORAGE` – `local` (default) or `s3`. For S3 also set `AWS_STORAGE_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_S3_REGION_NAME`, `AWS_S3_CUSTOM_DOMAIN`, `AWS_S3_CACHE_CONTROL`, `AWS_QUERYSTRING_AUTH`, `AWS_DEFAULT_ACL`.
- `DJANGO_ATTACHMENT_MAX_BYTES` – maximum upload size (default 10 MB).
- `DJANGO_ATTACHMENT_ALLOWED_TYPES` – comma-separated MIME types allowed in addition to images (`application/pdf,application/msword,...` by default).
- `DJANGO_ATTACHMENT_ALLOWED_PREFIXES` – MIME prefixes treated as safe (defaults to `image/`).
- `DJANGO_ATTACHMENT_SCAN_HANDLER` – optional dotted path to an antivirus scan callable; it should raise `ValidationError` when a file is rejected.
- `DJANGO_X_FRAME_OPTIONS` – overrides the frame-embedding policy (defaults to `SAMEORIGIN` so PDF previews work in the inline viewer).
- `ATTACHMENTS_OFFICE_VIEWER_URL` – template used for Office document previews (defaults to Microsoft Office Online viewer; `{url}` is replaced with the encoded attachment URL). When previews are disabled (or the viewer cannot reach your host), the UI falls back to `ms-*-` protocol links that open files in the local Office installation.
- `ATTACHMENTS_OFFICE_VIEWER_ENABLED` – set to `0`/`false` if you want to skip embedding Office Online (useful for offline/dev environments).

User-facing validation strings are localized; remember to update both the English and Bulgarian `.po` catalogs after changing copy.

### Mobile & accessibility notes

- The lightbox supports pinch/zoom, drag-to-pan, and keyboard shortcuts (`+`, `-`, `0`, `Esc`).
- Screen readers announce progress updates in the upload queue and treat the spinner as `role="status"` while images load.
- Buttons stay reachable on small viewports; long filenames wrap automatically without breaking layout.
- The async uploader hides completed items after a short delay to keep the focus on the attachment grid.

### Postgres Schema Verification

After importing data into PostgreSQL you can sanity-check the schema and row counts:

```bash
DATABASE_URL=postgres://… python manage.py verify_pg_schema --show-counts
```

The command confirms that the case-insensitive unit number constraint and supporting index exist and echoes record totals for the core models so you can compare them with a pre-migration dump (e.g., `python manage.py dumpdata core --indent 2 > backup.json`).

### Staging Cutover

Follow `docs/staging_cutover.md` for the exact steps to point staging at PostgreSQL, migrate data, validate the deployment, and roll back if needed.

### Production Cutover

Use `docs/production_cutover.md` during the production switchover. It outlines the downtime playbook, final export/import flow, validation checklist, and rollback procedure.

## Playwright Smoke Tests

Lightweight responsive smoke tests live in `tests/playwright/`.

```bash
npx playwright install  # downloads browsers
npm run test:ui         # runs smoke tests against http://localhost:8000
```

If authentication is required, provide credentials via environment variables or a `.env.playwright`
file (uses the same key names). The Playwright seed command creates a default user
`playwright/playwright123`; the tests will automatically use those credentials if no overrides
are supplied.

```bash
export PLAYWRIGHT_USERNAME=your_admin
export PLAYWRIGHT_PASSWORD=secret
npm run test:ui
```

Set `BASE_URL` to point at non-default environments (e.g., `BASE_URL=https://staging.example.com npm run test:ui`).

The suite captures screenshots for mobile (iPhone 13 Mini) and desktop (Chrome) breakpoints and stores them in `tests/playwright/artifacts/`.

## Mobile UX Guidelines

- **Mobile first**: every template is designed for small screens first; wide layouts progressively enhance with larger breakpoints.
- **Accessible touch targets**: buttons and actionable elements use Tailwind utilities to keep minimum 44px height and adequate spacing.
- **Sticky filters**: search/filter toolbars on list screens stay visible on larger screens but remain collapsible on mobile.
- **Card-first data**: tabular data collapses into cards on small viewports for easy scanning; essential metadata surfaces at the top.
- **Badges for state**: priority and status indicators rely on color-coded badges paired with text to communicate state at a glance.
- **Session-safe forms**: shared `_form_layout.html` ensures consistent field spacing, error handling, and responsive alignment.

These patterns should be reused for future additions to maintain alignment with the mobile design system.

## Release Notes

- **2025‑11‑02** – Added work order attachments with inline uploads, zoomable previews, REST API endpoints, and Playwright coverage across desktop and mobile breakpoints.
