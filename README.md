# FACEIT Tracker

Paste a FACEIT profile link and get CS2 stats: win rate, K/D, average kills, ADR, headshot %,
a form chart, per-map breakdown and a CSV export. Paste a second link to compare two players.

## Run locally
    FACEIT_API_KEY=your_key python server.py     # open http://localhost:8000

Get a server-side key at https://developers.faceit.com. Never commit the key: set it as an
environment variable (locally or in your hosting dashboard).

## Notes
- FACEIT limits `offset` to 200, so older matches are fetched by time (`to` parameter).
- `/health` returns `ok` for uptime monitors.
