# Budget Request Workflow

This release introduces a full-stack workflow for capturing technician budget requests, logging expenses with attachments, and giving backoffice reviewers an auditable approval queue.

## Data model

| Model | Purpose |
| --- | --- |
| `BudgetRequest` | Tracks the requester, building/project metadata, requested vs. approved caps, status transitions, and feature-flag gating. |
| `Expense` | Individual spend entries tied to a budget with category policies, per-day caps, and status transitions. |
| `ExpenseAttachment` | File uploads (PDF, image, invoices) with checksum + metadata for each expense. |
| `BudgetRequestEvent` | Immutable timeline of submissions, approvals, expenses, and attachment changes. |
| `ExpenseCategory` | Admin-managed lookup of allowed expense types (receipt required, mileage rate, daily max). |
| `BudgetFeatureFlag` | Enables per-building + per-role rollout of the “Budgets” tab and API. |

Use `python manage.py migrate` to install the new tables (`core/migrations/0030_*`).

## Permissions & feature flags

Capabilities were extended:

- Technicians: `view_budgets`, `manage_budgets` (create + log expenses on their own requests).
- Backoffice/Admin: `approve_budgets`, `export_budgets`.

`BudgetFeatureFlag` gates access per building/role. Populate at least one `key='budgets'` row (optionally scoped to a pilot building) to enable the navigation link and APIs.

## APIs

| Endpoint | Description |
| --- | --- |
| `GET/POST /api/budgets/` | List or create budget requests. POST accepts JSON or form-data matching `BudgetRequestForm`. |
| `GET/PATCH /api/budgets/<id>/` | Fetch or approve/reject budgets (requires `approve_budgets`). |
| `GET/POST /api/budgets/<id>/expenses/` | List or log expenses (technician/backoffice scope). |
| `GET/POST/DELETE /api/budgets/<id>/expenses/<expense_id>/attachments/` | Manage expense attachments via the existing upload guardrails. |

Responses include normalized numbers as strings for easier consumption in TypeScript/Playwright tests.

## UI surfaces

- Technician portal gains a feature-flagged “Budgets” tab (list, detail, inline expense drawer).
- Newly created budgets start “Unassigned” (no building/work order) but include a technician-provided title so approvers can quickly identify them.
- Backoffice/admin dashboard adds a review queue, bulk CSV export, and hooks to the immutable event timeline.
- Detail view surfaces warnings when attachments are missing for categories that require receipts.
- Fully spent budgets can be archived (status → Closed) and appear in an “Archived budgets” view grouped by requester for quick audits/history.

## QA & rollout

1. Create at least one `ExpenseCategory` (“fuel”, “materials”, etc.) before logging expenses.
2. Add pilot `BudgetFeatureFlag` rows (per building/role) and verify the new navigation pill appears only for opted-in users.
3. Run unit tests: `python -m pytest core/tests/test_budgets.py`.
4. Smoke-test the Playwright happy path once updated: request → approval → expense + attachment → overage alert.
5. Update the release checklist with migration order (`0030`), CSV export verification, and notification thresholds (`BUDGET_ALERT_THRESHOLD` setting).

For escalation + finance notifications, configure `BUDGET_ALERT_THRESHOLD` (default 0.9) and confirm `NotificationService` outputs appear in the user dashboard when a budget exceeds that percentage of its approved cap.
