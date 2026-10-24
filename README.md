# Dallas Theatre Calendar

Dallas–Fort Worth theatre listings as calendar feeds you can subscribe to. They're built from Onstage NTX's [Up & Coming](https://www.onstagentx.com/up-and-coming) page, the most complete list of DFW theatre around.

- Openings and closings: `https://kennethdenson.github.io/dallas-theatre/dallas-theatre-openings.ics`
- Full runs: `https://kennethdenson.github.io/dallas-theatre/dallas-theatre.ics`

## How it works

A GitHub Actions workflow runs every 6 hours. `app.py` fetches the page and checks its Squarespace `data-updated-on` stamp. When the page has changed, Claude Code (`claude -p`, Haiku, no tools, JSON schema) turns the hand-typed listings into structured shows. Working out the year for each show takes judgment, because seasons roll into the next year and a line can mix a second stage with holiday add-ons. The results go into `state.json`, which is committed, and the `.ics` files are published to GitHub Pages.

If a run fails, or finds less than half as many current shows as the previous run, the published feed stays as it was. Shows that close and drop off the page stay in the feed for a year.

Secret: `CLAUDE_CODE_OAUTH_TOKEN`, a `claude setup-token` used only by this repo.

Not affiliated with Onstage NTX.
