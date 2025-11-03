# Work Order Attachments

Attachments allow technicians and property managers to add reference files (photos, PDF reports, invoices) directly to a work order. This document outlines how uploads are stored, validated, and presented so you can operate the feature confidently in each environment.

## Overview

- Attachments live on the work order detail page where staff can upload, preview, download, and delete files without leaving the view.
- Images display inline in a responsive gallery with a zoomable lightbox supporting drag/pan, pinch gestures, and keyboard shortcuts (`+`, `-`, `0`, `Esc`).
- Non-image documents render as cards with file-type badges and direct download actions.
- All actions funnel through a JSON API so the UI stays responsive even on mobile data connections.

## Storage layout

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `MEDIA_ROOT` | `./media/` | Filesystem root for attachments when using local storage. |
| `MEDIA_URL` | `/media/` | Public URL prefix for media responses. |
| `DEFAULT_FILE_STORAGE` | `django.core.files.storage.FileSystemStorage` | Pluggable storage backend; switch to S3 by setting `DJANGO_FILE_STORAGE=s3`. |

Each attachment is stored under `work_orders/<work-order-id>/<uuid>.<ext>`, ensuring unique filenames even when users upload duplicates. When S3 is enabled make sure the IAM policy allows `PutObject`, `GetObject`, and `DeleteObject` on that prefix.

## Validation & security

- Maximum file size defaults to **10 MB** and is configurable via `DJANGO_ATTACHMENT_MAX_BYTES`.
- MIME-type checks combine explicit allowlists (`DJANGO_ATTACHMENT_ALLOWED_TYPES`) with prefixes (`DJANGO_ATTACHMENT_ALLOWED_PREFIXES`, default `image/`).
- A pluggable antivirus hook `DJANGO_ATTACHMENT_SCAN_HANDLER` can point at a callable that raises `ValidationError` when a file does not pass scanning.
- Metadata (original name, size, MIME type) is persisted in `WorkOrderAttachment` for auditability.

Uploads that fail validation return 400 responses with per-file error messages; the UI surfaces those failures in the upload queue.

## API surface

| Method | Endpoint | Description |
| ------ | -------- | ----------- |
| `GET` | `/core/api/work-orders/<id>/attachments/` | List attachment metadata (name, size, URL, category). |
| `POST` | `/core/api/work-orders/<id>/attachments/` | Upload one or more files under the `files` form field. |
| `DELETE` | `/core/api/work-orders/<id>/attachments/<attachment-id>/` | Remove an attachment if the user can manage the work order. |

All endpoints require authentication. Ownership rules mirror the rest of the work order module: building owners and staff can modify attachments; read-only users can only fetch metadata.

## Front-end behaviour

- Completed uploads remain in the queue briefly before collapsing, helping mobile users stay oriented.
- Cards adapt to one-column layouts below 640 px while maintaining full keyboard/focus support.
- The lightbox disables body scrolling while open and restores focus to the originating trigger when closed.
- The uploader and viewer degrade gracefully—users without JavaScript can still add files via the edit form or Django admin.

## Troubleshooting

| Symptom | Likely cause | Resolution |
| ------- | ------------ | ---------- |
| "No files were provided." error | Browser blocked the request or the field name changed. | Ensure the upload uses the `files` multi-value field and that the request is multipart. |
| Upload succeeds but thumbnails are broken | `MEDIA_URL` is misconfigured or the storage backend denies GET requests. | Verify `MEDIA_URL` origin matches the application domain and that the bucket policy allows public reads (or the app can proxy private URLs). |
| Deletion returns 403 | Current user lacks permission on the building. | Confirm user is staff/owner; otherwise escalate or adjust building ownership. |
| Playwright tests fail on attachments | Required browsers not installed or the API server unavailable. | Run `npx playwright install` and ensure `BASE_URL` points at a running instance with seeded credentials. |

For environment-specific playbooks (staging or production cutovers), pair this guide with `docs/staging_cutover.md` or `docs/production_cutover.md`.
