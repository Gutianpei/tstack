# example

> Made-up node that shows the format: a small weather dashboard that reads a public
> forecast feed and shows the next 48 hours for three cities.
> Last verified: 2026-01-20.
> Finished work: [HISTORY.md](HISTORY.md). Recent sessions: [journal.md](journal.md).

## Pointers
- Repo: `~/code/weather-dash` (branch `main`). Its own docs: `README.md` there.
- Config: `config/cities.yaml` lists the cities and the feed URL.
- Open PR: #12, hourly cache for the forecast feed.

## Status
- The dashboard runs locally and renders all three cities.
- Next: merge #12, then add a fourth city.

## Key decisions and why
- Cache the feed for one hour on disk. The feed allows 100 calls a day, and the page
  reloads far more often than that.
- Plain HTML with no framework. The page has one table, so a framework adds nothing.

## Gotchas and runbook
- Start: `cd ~/code/weather-dash && python3 serve.py --port 8080`.
- The feed returns times in UTC; convert them before display.
- Delete `cache/` to force a fresh fetch.
