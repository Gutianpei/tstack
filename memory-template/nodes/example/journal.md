# example journal

## 2026-01-18 — first version renders three cities

- Built `serve.py` and one HTML table for three cities from `config/cities.yaml`.
- The feed returned 429 after about 100 calls in one afternoon of reloads.
- Next: cache the feed.

## 2026-01-20 — hourly cache, PR #12

- Added an on-disk cache under `cache/` that expires after one hour; opened PR #12.
- Why: the feed allows 100 calls a day (see the 2026-01-18 entry).
- Tried a CSV export first and dropped it; recorded in HISTORY.md.
- Next: merge #12, then add a fourth city.
