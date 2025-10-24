# Staging Cutover Playbook

These steps assume staging already runs on PostgreSQL and describe how to refresh or recreate the environment.

## 1. Update Secrets

1. Retrieve the Postgres connection string (managed instance or container).
2. Set the following secrets/environment variables in your staging secret store:
   - `DATABASE_URL=postgres://USERNAME:PASSWORD@HOST:5432/DATABASE`
   - `DJANGO_DB_CONN_MAX_AGE=60` (optional but recommended)
   - `DJANGO_DB_CONN_HEALTH_CHECKS=true`
3. Remove any legacy volume mounts or references to `db.sqlite3`.

## 2. Restart Application Services

After updating secrets, restart the staging app (web dynos, workers, etc.) so Django reads the new Postgres configuration.

## 3. Run Database Preparation Commands

From a staging console or CI job:

```bash
python manage.py migrate
python manage.py verify_pg_schema --show-counts
```
If you need to seed data, import it using PostgreSQL-native tooling (e.g. `psql`, `pg_restore`, or fixtures) rather than SQLite dumps.

## 4. Validate

- Run the Playwright smoke suite against staging.
- Manually test critical flows (login, building/unit/work order CRUD).
- Monitor database metrics and logs for errors.

## 5. Rollback Plan

If issues occur:

1. Stop staging traffic.
2. Restore the PostgreSQL database from the most recent snapshot/backup.
3. Redeploy/restart the app.
4. Re-run the validation checklist before reopening access.
