# Fitbit Air dashboard (Claude Artifact)

The page template is `src/fitbit_air/dashboard/template.html`. `fitbit-air dashboard build` combines it with your
`config/dashboard.toml` (tabs, cards, trend metrics, default range, theme colours; layout only, no personal data) and
writes `data/dashboard/dashboard.html` in your home folder. That built file is the source of your private Claude
Artifact dashboard. With no `dashboard.toml` the default layout is used.

To publish it the first time, ask Claude to publish `data/dashboard/dashboard.html` as a private Artifact with the
capabilities below. Keep the Artifact URL that Claude returns. One option is a file such as
`data/dashboard/url.txt` in your home folder, which is never committed; fitbit-air itself doesn't read it.

- **Live data:** the page calls the local `fitbit-coach` MCP server (`host:fitbit-coach`) through the Claude app.
  This works only in Claude Desktop on your Mac, for the artifact's owner, with fitbit-coach registered.
- **Offline data:** the page reads the `snapshot/latest` document from the artifact's own database. Its content is
  the file that `fitbit-air snapshot` writes (`data/dashboard/snapshot.json`, under 240 KB).
- **Capabilities declared at publish:**
  `{mcp: {servers: [{server: "host:fitbit-coach", tools: [data_status, get_overview, get_trends, get_sleep,
  get_activity, get_running, get_body_age, get_coach_notes, get_evidence]}]}, db: {rules: [{path: "", read: "view",
  write: "admin"}]}}`

To republish after a rebuild, ask Claude to publish `data/dashboard/dashboard.html` to the Artifact URL you kept.
Leave capabilities out, so the stored ones are kept.

To refresh the snapshot by hand:
1. Run `uv run --frozen fitbit-air snapshot`.
2. Ask Claude to write `data/dashboard/snapshot.json` to the dashboard's `snapshot/latest` document.

fitbit-air doesn't upload the snapshot itself: step 2 is done by Claude. To keep it fresh, schedule both steps
yourself, for example as a daily scheduled Claude task that runs `fitbit-air snapshot` and writes the result to
`snapshot/latest`.
