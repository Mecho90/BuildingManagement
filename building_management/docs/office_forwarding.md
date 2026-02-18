## Office Forwarding Workflow

The Office building acts as an intake queue for administrator/backoffice staff.
Every work order created against the Office must be forwarded to a destination
building so local owners and technicians can take over. The workflow is:

1. **Create in Office** – choose *Office* from the building selector (or use the
   “Add work order” button on the Office dashboard). The form shows a dedicated
   **Forward to building/owner** panel that requires a destination building and
   optional forwarding note.
2. **Auto-notify stakeholders** – when you save the work order, the platform
   logs a forwarding audit entry, notifies the Office owner, and subscribes the
   destination owner/backoffice/technicians so everyone follows the ticket.
3. **Re-route any time** – edit the work order (or use the “Re-route” shortcut
   on the detail page) and select a different destination. A fresh forwarding
   history row plus notifications are issued automatically.

### Ownership & visibility

* Administrators/backoffice users automatically see the Office building and all
  forwarded tickets without joining individual buildings.
* Destination owners/backoffice/technicians gain visibility when a ticket is
  forwarded to their building. Confidential (`lawyer_only`) tickets still
  require the lawyer/backoffice capabilities—forwarding alone does not bypass
  confidentiality.
* If the Office owner rotates, run `python manage.py ensure_office_building`
  with the new username/email; the command updates the FK owner **and** grants
  owner-level memberships to every administrator account.

### Handling mistakes

* **Mis-forwarded ticket** – edit the work order, pick the correct building,
  and save. The re-route is logged, new recipients are notified, and the Office
  staff retain their original visibility.
* **Destination building deleted** – the `forwarded_to_building` field becomes
  `NULL`. The ticket automatically falls back to Office-only visibility. Re-run
  `ensure_office_building` (optional) and re-route the ticket to a valid
  building so that field users regain access.
* **Owner changes downstream** – once the building’s FK owner changes, run
  `ensure_office_building` or add/update memberships for the destination
  building so the new owner receives notifications. Forwarded tickets do not
  need to be re-created.

Keep this document with the rest of the production runbooks. It doubles as the
forwarding playbook for operations teams.

### Monitoring & Health Checks

Operations teams can monitor the Office queue via the
`/health/forwarding/` endpoint. Access is restricted to authenticated staff or
superusers, and each identity/IP is rate limited (default: 30 requests per
60 seconds) to prevent abuse. To run the check:

1. Sign in with a staff account (or create an API token/session that keeps the
   user authenticated).
2. Issue a GET request to `/health/forwarding/` from within the trusted network.
   A JSON payload is returned describing the Office queue and forwarding backlog.
   Metrics are cached for `FORWARDING_HEALTH_CACHE_TIMEOUT` seconds (30 by default),
   so expect values to update in that interval—not on every request.

If the endpoint returns `403`, confirm the caller is a staff/superuser. A `429`
response indicates the rate limit has been exceeded—pause checks or reduce the
polling frequency before retrying.

### Office singleton maintenance

The Office building is a system singleton. Always change its ownership or
`is_system_default` flag through normal ORM saves (admin UI, Django shell) or by
running `python manage.py ensure_office_building --owner=<username>`. These
paths trigger the model signals that invalidate caches and re-seed owner/backoffice
memberships. Manual SQL/`update()` operations bypass the signals; the platform
will eventually recover thanks to the
`SYSTEM_DEFAULT_BUILDING_CACHE_TIMEOUT` (default 300 s), but during that window
other workers may keep serving stale IDs. Use the management command or a full
model `.save()` whenever possible, and reserve manual updates for break-glass
scenarios where you can tolerate the temporary inconsistency.
