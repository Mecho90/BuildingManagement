# Production Cutover Runbook

> Purpose: operate the production environment on PostgreSQL with minimal downtime and a verified rollback path.

---

## 1. Pre-Cutover Checklist

- [ ] Confirm backups: latest PostgreSQL snapshot/point-in-time restore markers available.
- [ ] Provision PostgreSQL database (PITR enabled, monitoring + alerting configured).
- [ ] Ensure `psycopg` dependency is deployed and application images are up to date.
- [ ] Create maintenance-page or read-only banner to display during cutover.
- [ ] Notify stakeholders of the downtime window and rollback plan.

## 2. Enter Maintenance Window

1. Put the application into read-only/maintenance mode (disable background jobs, pause workers).
2. Stop traffic via load balancer or maintenance page.

## 3. Database Preparation

1. Freeze writes (enforce maintenance mode).
2. Capture a fresh PostgreSQL snapshot or base backup prior to applying migrations.

## 4. Apply Migrations

1. Ensure production secrets are set:
   - `DATABASE_URL=postgres://USER:PASS@HOST:5432/DB`
   - `DJANGO_DB_CONN_MAX_AGE=300`
   - `DJANGO_DB_CONN_HEALTH_CHECKS=true`
2. Deploy/restart application components with the new secrets.
3. On the production shell:
   ```bash
   python manage.py migrate
   python manage.py verify_pg_schema --show-counts
   ```
4. Compare row counts to historical metrics/BI reports. Investigate discrepancies before resuming traffic.

## 5. Validation Suite

- Automated smoke tests (Playwright/Django) against the production base URL (read-only flows only).
- Manual checks:
  - Admin login and dashboard.
  - Building/Unit list pages render and show correct counts.
  - Create, update, and delete operations (if allowed during validation) or simulate via staging first.
  - Background jobs/cron tasks resume without errors.

Record outcomes in the PR description or operations log, including timestamps, operators, and commands executed.

## 6. Reopen Traffic

1. Remove maintenance mode.
2. Unpause workers and background jobs.
3. Monitor:
   - DB connections, slow query log, replication lag.
   - Application error rate, latency, resource usage.

## 7. Post-Cutover Actions

- [ ] Confirm PostgreSQL backups and monitoring are functioning.
- [ ] Update documentation with final configuration (`docs/staging_cutover.md`, README).
- [ ] Schedule a follow-up review to adopt PostgreSQL features (indexes, analytics).
- [ ] Ensure new backup jobs for PostgreSQL are operational.

## 8. Rollback Plan

If critical issues arise:

1. Re-enter maintenance mode.
2. Initiate PostgreSQL point-in-time recovery to the pre-cutover snapshot.
3. Redeploy/restart services against the restored database.
4. Run smoke tests to confirm system health.
