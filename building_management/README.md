# Building Management UI

Responsive Django/Tailwind UI for managing buildings, units, and work orders.

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

Optional env vars:

- `DJANGO_DB_CONN_MAX_AGE` – persistent connection lifetime in seconds (default `60`).
- `DJANGO_DB_CONN_HEALTH_CHECKS` – defaults to `true` when pooling is enabled; override with `false` to disable.
- `DJANGO_DB_SSLMODE` – override the default SSL mode (`require` for non-local hosts, unset for localhost).
- `DJANGO_DB_APP_NAME` – registers a custom `application_name` with Postgres for monitoring.

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
