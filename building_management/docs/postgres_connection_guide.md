# PostgreSQL Connection & Configuration Guide

This guide shows how to connect this Django app to PostgreSQL and verify everything is working.

## 1. Prerequisites

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```
2. Confirm PostgreSQL driver is available:
```bash
python -c "import psycopg; print(psycopg.__version__)"
```

## 2. Start PostgreSQL

Choose one option.

### Option A: Local PostgreSQL via Docker Compose (recommended for local dev)

1. Start the database container:
```bash
docker compose up -d postgres
```
2. Check logs:
```bash
docker compose logs --tail=50 postgres
```
3. Expected default connection:
`postgres://building_mgmt:building_mgmt@localhost:5432/building_mgmt`

### Option B: External PostgreSQL (RDS / managed DB / remote host)

1. Create database + user on your PostgreSQL server.
2. Make sure network/firewall rules allow access from your app host.
3. Build the connection URL:
`postgres://<db_user>:<db_password>@<db_host>:5432/<db_name>`

## 3. Configure environment variables

Set at minimum `DATABASE_URL`.

```bash
export DATABASE_URL=postgres://building_mgmt:building_mgmt@localhost:5432/building_mgmt
```

Optional database tuning/env vars supported by this project:

```bash
export DJANGO_DB_CONN_MAX_AGE=60
export DJANGO_DB_CONN_HEALTH_CHECKS=true
export DJANGO_DB_SSLMODE=require
export DJANGO_DB_APP_NAME=building_mgmt_web
```

Notes:
1. `DJANGO_DB_SSLMODE` defaults to `require` for non-local hosts.
2. For localhost, SSL mode is not forced unless you set `DJANGO_DB_SSLMODE`.
3. If `DATABASE_URL` is missing, the app falls back to SQLite (`db.sqlite3`).

## 4. Apply database schema

Run migrations against PostgreSQL:

```bash
python manage.py migrate
```

## 5. Migrate data from SQLite to local PostgreSQL (Option A)

Use this when your current data is in `db.sqlite3` and you want it in local Docker PostgreSQL.

1. Make a backup of SQLite:
```bash
cp db.sqlite3 db.sqlite3.backup.$(date +%Y%m%d_%H%M%S)
```
2. Export data from SQLite to JSON fixtures.
Keep `DATABASE_URL` unset so Django reads from SQLite:
```bash
unset DATABASE_URL
python manage.py dumpdata \
  --natural-foreign \
  --natural-primary \
  --exclude contenttypes \
  --exclude auth.permission \
  --indent 2 > /tmp/sqlite_export.json
```
3. Point Django to local PostgreSQL:
```bash
export DATABASE_URL=postgres://building_mgmt:building_mgmt@localhost:5432/building_mgmt
```
4. Apply schema on PostgreSQL:
```bash
python manage.py migrate
```
5. Load exported data into PostgreSQL:
```bash
python manage.py loaddata /tmp/sqlite_export.json
```
6. Reset auto-increment sequences (safe after `loaddata`):
```bash
python manage.py sqlsequencereset core auth admin sessions | python manage.py dbshell
```
7. Verify data and schema:
```bash
python manage.py verify_pg_schema --show-counts
python manage.py shell -c "from core.models import Building, WorkOrder; print('buildings=', Building.objects.count(), 'work_orders=', WorkOrder.objects.count())"
```

Notes:
1. Attachments/files are not inside SQLite; keep/copy your `media/` directory separately.
2. If this is a fresh environment, run:
```bash
python manage.py ensure_office_building
```
3. If `loaddata` fails due to existing conflicting rows, recreate the local Postgres volume and retry:
```bash
docker compose down -v
docker compose up -d postgres
python manage.py migrate
python manage.py loaddata /tmp/sqlite_export.json
```

## 6. Verify connection

1. Check active DB config (engine/name/host):
```bash
python manage.py shell -c "from django.conf import settings; print(settings.DATABASES['default'])"
```
2. Run a quick SQL check:
```bash
python manage.py shell -c "from django.db import connection; c=connection.cursor(); c.execute('select version()'); print(c.fetchone()[0])"
```
3. Optional schema sanity check:
```bash
python manage.py verify_pg_schema --show-counts
```

4. Exact `psql` check for the `core_unit` case-insensitive uniqueness index:
```bash
python manage.py dbshell
```
```sql
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename='core_unit'
  AND indexname='unique_unit_number_ci_per_building';
```

Explanation:
1. If this query returns one row, the required uniqueness rule exists.
2. In PostgreSQL, expression-based uniqueness (like `lower(number), building_id`) is commonly stored as a unique index.
3. Older versions of `verify_pg_schema` might only check `pg_constraint`, which can report a false failure even though the unique index is present and correct.

## 7. Run the app with PostgreSQL

Development server:

```bash
python manage.py runserver
```

Gunicorn example:

```bash
gunicorn -c gunicorn.conf.py building_mgmt.wsgi:application
```

If you changed env vars in a service manager (`systemd`, Docker, etc.), restart the service/container so new DB settings are loaded.

## 8. Persist environment variables

For repeatable setup, keep DB settings in an env file (example: `.env`) and load it before starting app processes.

Example `.env`:

```dotenv
DATABASE_URL=postgres://building_mgmt:building_mgmt@localhost:5432/building_mgmt
DJANGO_DB_CONN_MAX_AGE=60
DJANGO_DB_CONN_HEALTH_CHECKS=true
DJANGO_DB_APP_NAME=building_mgmt_web
```

## 9. Common issues and fixes

1. `Unsupported DATABASE_URL scheme`:
Use `postgres://` or `postgresql://` only.
2. `DATABASE_URL must include a database name`:
Make sure URL ends with `/<db_name>`.
3. Connection refused / timeout:
Check host, port, security group/firewall, and whether Postgres is running.
4. Authentication failed:
Recheck username/password and DB user privileges.
5. SSL errors on managed DB:
Set `DJANGO_DB_SSLMODE=require` (or provider-required value).
6. App still uses SQLite:
Confirm `DATABASE_URL` is exported in the same shell/service environment as the running process.

## 10. Quick copy/paste local setup

```bash
docker compose up -d postgres
pip install -r requirements.txt
export DATABASE_URL=postgres://building_mgmt:building_mgmt@localhost:5432/building_mgmt
python manage.py migrate
python manage.py runserver
```
