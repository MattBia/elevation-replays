# BIA Monthly Elevation Call Replays

Auto-posts each month's **BIA Monthly Elevation Call** Grain replay to
`bianutrition.com/elevation-replays`, with a short recap of what the call was about.

Same shape as [`ph-replays`](../ph-replays), with one difference: each entry carries a
**topic** and **recap** as well as a link, and the page shows the month name
("June Elevation Call") rather than a date.

## How it works

1. A GitHub Action runs Monday evenings and calls `scripts/update_elevation.py`.
2. The script asks Grain for a recording titled "…Elevation…" that started on the
   **most recent Monday in ET**. Not "today": GitHub fires cron on low-traffic repos
   hours late, and every scheduled run in Aug–Sep 2026 landed after midnight ET and
   bailed as "not a Monday" before this was changed.
3. If it finds one, it appends `{date, url, topic: "", recap: ""}` to `elevation.json`.
4. `scripts/write_recap.py` then pulls the call's transcript from Grain, has Claude
   write the `topic` + `recap` in the house style below, checks the draft against the
   style rules (length, no participant names, no prize talk), and fills them in.
5. A Squarespace Code Block (`squarespace-embed.html`) fetches the raw JSON from
   GitHub and renders the list, newest first, grouped by year.

The call is on the **first or second Monday** of the month, at 7:00–8:00 PM ET.
The workflow runs every Monday and exits quietly when there's no call. A month is
only reported as missed once its **second** Monday passes with nothing posted.

## Stream Deck button (manual run)

`workflow_dispatch` runs (the Stream Deck button in `../streamdeck-actions`, or
"Run workflow" in the Actions tab) set `MANUAL=true`, which changes two things:

- It looks for the newest Elevation Call in the **last 13 days** instead of the most
  recent Monday — long enough that a press in second-Monday week still rescues a
  first-Monday call, short enough that it can't "find" last month's call.
- "Nothing found" is a **failure** (exit 1, Slack `:hourglass:` note) instead of a
  quiet no-op. That's deliberate: the `check-manual-run` job makes the scheduled
  slots stand down when a *successful* manual run happened in the last 12 hours,
  so a press that came before Grain finished processing must not count.

Every run ends with a `RESULT: …` line; the button shows that line in its notification.
The `check_only` input still works for a credential self-check on any day.

## The recap

`write_recap.py` runs on every workflow run and writes a topic + recap for any call
that is missing one, from the **transcript** (`/recordings/{id}/transcript.txt`), never
from Grain's AI summary. It uses `claude-opus-5` with a JSON-schema output and
Anthropic's server-side refusal fallback enabled. The four most recent finished recaps
are passed in as voice examples, so the page stays consistent with itself.

Drafts are checked before they're saved: 15–55 words, topic ≤ 9 words, no
raffle/prize words, no first name of any non-host speaker from the transcript, no
"Michelle". A failing draft gets one retry with the problems listed; if it still fails,
the entry stays blank and Slack gets a `:warning:` so you can write it by hand.

Needs the `ANTHROPIC_API_KEY` repo secret. Without it the link still posts and the
recap step logs a warning; the next run with the key fills it in.

To re-do a recap, blank out `topic` and `recap` for that entry in `elevation.json`
and run the workflow (or press the Stream Deck button). To tweak wording, just edit
`elevation.json` directly.

House style for recaps (this is what the prompt enforces):

- **1–2 sentences, roughly 30–45 words.** The page is read on phones, and each
  recap sits in a card — much longer and the card turns into a wall of text.
- Second person ("you"), plain language.
- Lead with **what the call was actually about** — the idea, framework, or theme.
- **No client names, no individual wins, no raffle or prize talk, no logistics.**
  Members should get a feel for the content, not a roll call.
- Keep the one specific that makes it worth clicking (the milkshake study, FEAR as
  False Evidence Appearing Real, Wish/Outcome/Obstacle/Plan) — that's the hook.
- `topic` is a short title-case phrase and becomes the card headline; `recap` is
  the short paragraph under it. If `topic` is blank the card falls back to
  "{Month} Elevation Call", so a new entry still renders before it's written up.

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

## Setup

- [x] GitHub repo `MattBia/elevation-replays` (public), Actions write permission.
- [x] Repo secrets `GRAIN_API_TOKEN_V2` and `SLACK_WEBHOOK_URL`.
- [ ] Repo secret `ANTHROPIC_API_KEY` (same as the one in bia-executive-assisstant / bia-feedback-bot) for the recap step.
- [ ] Create the `/elevation-replays` Squarespace page and paste
      `squarespace-embed.html` into a Code Block (`REPO` is already set).

## History note

Before June 2025 this same monthly call was titled **"BIA MCC with BIA Coaches"**
in Grain. Those 11 calls (July 2024 – May 2025) **are** included, and the page labels
them the same way as everything else ("May Elevation Call"). Michelle used the two
names interchangeably on the calls themselves — "the next MCC (Monthly Elevation
Call)" — so one label across the archive reads correctly.

Most of the MCC-era recordings have **no AI notes generated in Grain**, so those
recaps were written from the full transcripts rather than from Grain summaries.
If you ever regenerate or re-verify them, go to the transcript, not the summary.
