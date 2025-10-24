# Building Management UI

Responsive Django/Tailwind UI for managing buildings, units, and work orders.

## Development Workflow

1. **Install tooling**
   ```bash
   npm install
   npm run build:css  # generates static/css/dist.css
   ```
2. **Tailwind watch mode** (during template work)
   ```bash
   npm run watch:css
   ```
3. **Run Django**
   ```bash
   python manage.py runserver
   ```

Tailwind is configured with purge-aware `content` globs so unused utilities are removed in the production bundle.

## Database Configuration

The application now requires PostgreSQL. For local development:

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
