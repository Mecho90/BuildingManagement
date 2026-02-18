## QA Checklist

Run this checklist before each release or major infrastructure change. It focuses on Office forwarding workflows and the new observability hooks.

### Core regressions

1. **Work orders**
   - Create an Office work order, forward it to a destination, and confirm the destination owner/backoffice can see it.
   - Edit an existing Office ticket and ensure forwarding metadata (building, note, forwarded_by) persists.
   - Attempt to clear an existing forwarding target; confirm validation prevents it.
2. **Office visibility**
   - Sign in as admin and backoffice users; verify the Office building is pinned in selectors/dashboards.
   - Sign in as a technician without Office membership; ensure the building stays hidden unless explicitly granted.

### Observability & health

1. **Forwarding health endpoint (`/health/forwarding/`)**
   - Hit the endpoint at least 5 times in quick succession (staff login required) and confirm responses remain `200 OK`, with `cache_hit` toggling to `true` after the first call.
   - Verify rate limiting by exceeding `FORWARDING_HEALTH_RATE_LIMIT` in a controlled environment; expect a `429` and log entries without PII.
2. **Timing logs**
   - Inspect application logs and confirm `timing.*` entries hash any user identifiers (`hash:XXXX`).
   - Introduce a known error path (e.g., force a failing work-order list query) and ensure a single `status=error` entry is emitted.
   - Adjust `TIMING_LOG_MIN_DURATION_MS` (staging only) to validate that sub-threshold successful spans are suppressed.

### Load coverage

1. Execute `python manage.py test core.tests.test_load` to simulate burst traffic for `/health/forwarding/`, work-order lists, and building dashboards.
2. During staging smoke tests, run Postman/Locust (or similar) scripts to hit:
   - `/health/forwarding/` at least 200 times/minute for 1 minute.
   - `/work-orders/` and `/buildings/<id>/` concurrently for 1 minute.
   Confirm no 5xx responses and log ingestion remains stable.

Document results (success/failure, timestamps, operators) in the PR description or release tracker.
