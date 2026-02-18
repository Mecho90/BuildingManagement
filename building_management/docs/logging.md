## Logging & Timing Metrics

Our ad-hoc performance instrumentation uses `core.utils.metrics.log_duration`. To
keep privacy and log volume in check:

1. **PII scrubbing** – Any extra fields passed to `log_duration` that include
   `user`, `email`, `owner`, or `technician` in the key name are hashed before
   being emitted. Prefer passing stable IDs instead of raw emails; the helper
   takes care of hashing them (`hash:<first16hex>`).
2. **Error tagging** – When the wrapped block raises, the context manager logs a
   single `status=error` record (with `error_type`) and re-raises. Do not wrap it
   in a try/except unless you plan to re-raise; this keeps the emitted logs
   consistent for alerting.
3. **Volume controls** – Set `TIMING_LOG_MIN_DURATION_MS` (default `0`) to avoid
   flooding downstream log processors with sub-millisecond spans. Successful
   timings below the threshold are skipped automatically, while failures always
   log regardless of duration.

### Operational guidance

- **Sampling vs. retention** – If your log processor already samples timing
  entries, combine that with the duration threshold above. Avoid duplicating the
  same metric at multiple log levels.
- **Dashboards** – When constructing queries, filter on `metric` plus
  `status=ok|error`. Expect hashed identifiers for user-related fields and treat
  them as opaque tokens.
- **Manual overrides** – If you need to rotate the hashing salt, change
  `SECRET_KEY` (affects the app broadly) or provide a deterministic mapping in
  your log pipeline. The helper does not store raw user IDs anywhere in logs.
