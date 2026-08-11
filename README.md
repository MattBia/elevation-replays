# BIA Monthly Elevation Call Replays

Auto-posts each month's **BIA Monthly Elevation Call** Grain replay to
`bianutrition.com/elevation-replays`, with a short recap of what the call was about.

Same shape as [`ph-replays`](../ph-replays), with one difference: each entry carries a
**topic** and **recap** as well as a link, and the page shows the month name
("June Elevation Call") rather than a date.

## How it works

1. A GitHub Action runs Monday evenings and calls `scripts/update_elevation.py`.
2. The script asks Grain for a recording titled "…Elevation…" that started **today in ET**.
3. If it finds one, it appends `{date, url, topic: "", recap: ""}` to `elevation.json`.
4. A Squarespace Code Block (`squarespace-embed.html`) fetches the raw JSON from
   GitHub and renders the list, newest first, grouped by year.

The call is on the **first or second Monday** of the month, at 7:00–8:00 PM ET.
The workflow runs every Monday and exits quietly when there's no call. A month is
only reported as missed once its **second** Monday passes with nothing posted.

## Writing the recap

`topic` and `recap` are intentionally left blank by the updater. Summarizing an
hour-long call into something worth reading is a judgment call, not a string
transform, so it stays a deliberate step. The embed hides blank fields, so a new
call appears as a plain link until the recap is written.

To fill one in, ask Claude:

> Read the Grain notes for the latest BIA Elevation Call and write the topic + recap
> for `elevation.json`.

House style for recaps, based on what's already in the file:

- **2–3 sentences**, second person ("you"), plain language.
- Lead with **what the call was actually about** — the idea, framework, or theme.
- **No client names, no individual wins, no raffle or prize talk, no logistics.**
  Members should get a feel for the content, not a roll call.
- Keep specifics that make it worth clicking (the milkshake study, FEAR as False
  Evidence Appearing Real, 0.8–1g protein per pound) — those are the hook.
- `topic` is a short title-case phrase; `recap` is the paragraph underneath.

## Data shape

```json
{
  "calls": [
    {
      "date": "2026-08-10",
      "url": "https://grain.com/share/recording/<id>/<token>",
      "topic": "Who You Are Now vs. Your Future Self",
      "recap": "A pen-and-paper exercise mapping who you are today against…"
    }
  ]
}
```

`date` is the call date in **ET**. This matters: the call starts 7 PM ET, which is
already the next day in UTC for most of the year, so a naive UTC date would put
every call on a Tuesday.

## Grain v2 contract

- `POST https://api.grain.com/_/public-api/v2/recordings`
- Body: `{"filter": {"title_search": "Elevation"}}`
- Headers: `Public-Api-Version: 2025-10-31`, `Authorization: Bearer <token>`
- Token env var is `GRAIN_API_TOKEN_V2` (**not** `GRAIN_API_TOKEN`) — same value
  the onboarding automation uses.
- Share link is the `recording_url` field (`/share/recording/<id>/<token>`).
- Recordings are auto-shared.

## Setup steps (Matt)

- [ ] Create the GitHub repo `MattBia/elevation-replays` and push this folder.
- [ ] Add repo secret `GRAIN_API_TOKEN_V2` (same value `ph-replays` uses).
- [ ] Optionally add `SLACK_WEBHOOK_URL`.
- [ ] Create the `/elevation-replays` Squarespace page and paste
      `squarespace-embed.html` into a Code Block.
- [ ] If the repo name differs, update `REPO` at the top of the embed's `<script>`.

## History note

Before June 2025 this same monthly call was titled **"BIA MCC with BIA Coaches"**
in Grain. Those 11 calls (July 2024 – May 2025) **are** included, and the page labels
them the same way as everything else ("May Elevation Call"). Michelle used the two
names interchangeably on the calls themselves — "the next MCC (Monthly Elevation
Call)" — so one label across the archive reads correctly.

Most of the MCC-era recordings have **no AI notes generated in Grain**, so those
recaps were written from the full transcripts rather than from Grain summaries.
If you ever regenerate or re-verify them, go to the transcript, not the summary.
