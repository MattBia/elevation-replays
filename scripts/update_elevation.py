#!/usr/bin/env python3
"""
BIA Monthly Elevation Call updater.

Runs Monday evenings via GitHub Actions. If an Elevation Call happened today,
grabs its Grain share URL and upserts it into elevation.json (keyed by date, so
re-runs are harmless).

The `topic` and `recap` fields are written blank here and filled in by
scripts/write_recap.py, which runs as the next workflow step (transcript ->
Claude -> style check). The embed hides blank fields, so if the recap step can't
run yet the entry still shows up as a plain link.

The call lands on the first OR second Monday of the month, so this runs every
Monday and simply exits quietly when there's nothing to post. A month is only
treated as MISSED if the second Monday passes with no entry for that month.

Env vars:
  GRAIN_API_TOKEN_V2  required - same token BIA's onboarding automation uses
  SLACK_WEBHOOK_URL   optional - posts a note on success or final failure
  FINAL_ATTEMPT       optional - "true" on the last cron slot of the evening;
                      controls whether a miss is treated as a failure
  MANUAL              optional - "true" when triggered by hand (Stream Deck /
                      "Run workflow"). Looks for the newest Elevation Call in
                      the last MANUAL_LOOKBACK_DAYS instead of the most recent
                      Monday, and treats "nothing found" as a failure so the
                      scheduled runs know they still have work to do.

Scheduled runs target the most recent Monday (ET) rather than "today" because
GitHub fires cron jobs hours late on low-traffic repos. Every scheduled run from
Aug-Sep 2026 landed after midnight ET and bailed as "not a Monday" - the fix is
to key everything off the Monday the run was *meant* for.

Exit codes:
  0 = entry added, already present, or nothing expected today
  1 = hard error, the month's call missing after its second Monday, or
      not-found on a manual run

The last line of output is always "RESULT: ..." so a caller (the Stream Deck
button) can show it without parsing the whole log.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Grain v2 public API. Recording search is a POST with a JSON filter body;
# this matches the contract BIA's client_onboarding.py already uses in prod.
GRAIN_RECORDINGS_URL = "https://api.grain.com/_/public-api/v2/recordings"
# .strip() matters: pasting a secret into the GitHub UI very easily carries a
# trailing newline, and requests rejects the whole header before it ever sends
# ("Invalid leading whitespace, reserved character(s), or return character(s)").
GRAIN_TOKEN = os.environ["GRAIN_API_TOKEN_V2"].strip()
GRAIN_HEADERS = {
    "Authorization": f"Bearer {GRAIN_TOKEN}",
    "Public-Api-Version": "2025-10-31",
    "Content-Type": "application/json",
}
CALLS_PATH = Path(__file__).resolve().parent.parent / "elevation.json"
TITLE_KEYWORD = "elevation"  # real title is "BIA Monthly Elevation Call"
ET = ZoneInfo("America/New_York")
MONDAY = 0
# Manual runs look back this many days (inclusive). Long enough that a press in
# second-Monday week still finds a first-Monday call the schedule missed, but
# shorter than the gap to the previous month's call, so a press before this
# month's call can't "find" last month's and report success.
MANUAL_LOOKBACK_DAYS = 13


def notify_slack(message: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        requests.post(url, json={"text": message}, timeout=10)
    except requests.RequestException:
        pass  # notifications are best-effort


def load_calls() -> dict:
    with open(CALLS_PATH) as f:
        return json.load(f)


def save_calls(data: dict) -> None:
    data["calls"].sort(key=lambda c: c["date"], reverse=True)
    with open(CALLS_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _recording_date_et(recording: dict) -> str | None:
    """Return the recording's start date (YYYY-MM-DD) in ET, or None.

    The call starts 7:00 PM ET, which is the *next* UTC day for most of the
    year - so converting to ET is what keeps the posted date correct.
    """
    raw = recording.get("start_datetime")
    if not raw:
        return None
    # Grain v2 returns ISO-8601, e.g. "2026-08-10T23:01:28Z".
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def most_recent_monday(today: date) -> date:
    """The most recent Monday on or before `today` (today itself if Monday)."""
    return today - timedelta(days=(today.weekday() - MONDAY) % 7)


def fetch_call(start: date, end: date) -> dict | None:
    """Return the newest Grain Elevation Call recording dated start..end (ET), or None.

    Search is a POST with a title filter (Grain v2), then we keep only
    recordings that (a) contain the "Elevation" keyword and (b) started within
    the window in ET. Newest wins if there are several.
    """
    resp = requests.post(
        GRAIN_RECORDINGS_URL,
        headers=GRAIN_HEADERS,
        json={"filter": {"title_search": "Elevation"}},
        timeout=30,
    )
    resp.raise_for_status()
    recordings = resp.json().get("recordings", [])

    lo, hi = start.isoformat(), end.isoformat()
    matches = []
    for r in recordings:
        if TITLE_KEYWORD not in (r.get("title") or "").lower():
            continue
        d = _recording_date_et(r)
        if d and lo <= d <= hi:
            matches.append(r)
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("start_datetime") or "", reverse=True)
    return matches[0]


def extract_share_url(recording: dict) -> str | None:
    """
    Pull the public share URL off the recording object. Grain's v2 recording
    objects expose it as `recording_url` (a .../share/recording/<id>/<token>
    link); older/related endpoints have used `url`. Check known keys and
    prefer an actual /share/ link so we never post an auth-gated workspace URL.
    """
    candidates = [
        recording.get(k)
        for k in ("recording_url", "url", "share_url", "public_url")
    ]
    candidates = [c for c in candidates if c and "grain.com" in c]
    if not candidates:
        return None
    for c in candidates:
        if "/share/" in c:
            return c
    return candidates[0]


def monday_ordinal(day: int) -> int:
    """Which Monday of the month this is (1st, 2nd, ...)."""
    return (day - 1) // 7 + 1


def self_check() -> int:
    """Verify both credentials work, without touching elevation.json.

    Run any day via the workflow's `check_only` dispatch input. Confirms the
    secrets are good without touching the page or Slack-alarming anyone.
    """
    print("== Self-check: credentials ==")

    raw = os.environ.get("GRAIN_API_TOKEN_V2", "")
    print(f"GRAIN_API_TOKEN_V2 present: {bool(raw)} "
          f"(raw length {len(raw)}, stripped {len(raw.strip())})")
    if raw != raw.strip():
        print("NOTE: token had surrounding whitespace/newline; stripped before use. "
              "Harmless here, but worth re-pasting the secret without the trailing "
              "newline if you set it by piping a file.")

    try:
        resp = requests.post(
            GRAIN_RECORDINGS_URL,
            headers=GRAIN_HEADERS,
            json={"filter": {"title_search": "Elevation"}},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"FAIL: could not reach Grain: {e}", file=sys.stderr)
        return 1

    print(f"Grain HTTP status: {resp.status_code}")
    if resp.status_code in (401, 403):
        print("FAIL: Grain rejected the token (bad or expired).", file=sys.stderr)
        return 1
    if not resp.ok:
        print(f"FAIL: Grain returned {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return 1

    recordings = resp.json().get("recordings", [])
    matches = [r for r in recordings if TITLE_KEYWORD in (r.get("title") or "").lower()]
    print(f"PASS: Grain auth OK - {len(recordings)} recordings returned, "
          f"{len(matches)} matching '{TITLE_KEYWORD}'.")
    if matches:
        matches.sort(key=lambda r: r.get("start_datetime") or "", reverse=True)
        newest = matches[0]
        date_et = _recording_date_et(newest)
        has_url = bool(extract_share_url(newest))
        print(f"       Most recent: {newest.get('title')!r} on {date_et} (ET), "
              f"share URL present: {has_url}")

    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    print(f"SLACK_WEBHOOK_URL present: {bool(webhook)}")
    if not webhook:
        print("WARN: no Slack webhook set - notifications will be skipped "
              "(the updater still works without it).")
        return 0

    try:
        s = requests.post(
            webhook,
            json={"text": ":white_check_mark: Elevation Call updater self-check - "
                          "Grain and Slack credentials are both working. "
                          "(Test message, no action needed.)"},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"FAIL: Slack webhook unreachable: {e}", file=sys.stderr)
        return 1

    if not s.ok:
        print(f"FAIL: Slack returned {s.status_code}: {s.text[:200]}", file=sys.stderr)
        return 1
    print("PASS: Slack webhook accepted the test message.")
    return 0


def result(msg: str) -> None:
    """Final status line; the Stream Deck button surfaces this verbatim."""
    print(f"RESULT: {msg}")


def main() -> int:
    if os.environ.get("CHECK_ONLY", "").lower() == "true":
        rc = self_check()
        result("Credential self-check " + ("passed." if rc == 0 else "FAILED - see log."))
        return rc

    now_et = datetime.now(ET)
    today = now_et.date()
    final_attempt = os.environ.get("FINAL_ATTEMPT", "").lower() == "true"
    manual = os.environ.get("MANUAL", "").lower() == "true"

    data = load_calls()
    posted_months = {c["date"][:7] for c in data["calls"]}

    if manual:
        start = today - timedelta(days=MANUAL_LOOKBACK_DAYS)
        label = f"the last {MANUAL_LOOKBACK_DAYS} days"
    else:
        # The Monday this run is for: today if it's still Monday in ET, or
        # yesterday when GitHub's cron delay pushed us past midnight.
        start = most_recent_monday(today)
        label = start.isoformat()
        month = start.strftime("%Y-%m")
        # Idempotency: bail if this month is already posted (earlier Monday,
        # earlier slot, or a button press).
        if month in posted_months:
            print(f"Entry for {month} already exists. Nothing to do.")
            result(f"Already posted for {month}. Nothing to do.")
            return 0

    try:
        recording = fetch_call(start, today)
    except requests.RequestException as e:
        print(f"Grain API error: {e}", file=sys.stderr)
        notify_slack(f":warning: Elevation Call updater hit a Grain API error: {e}")
        result(f"Grain API error: {e}")
        return 1

    if recording is None:
        if manual:
            print(f"No Elevation Call recording in Grain for {label}.")
            # Fail so the scheduled runs don't stand down on our account.
            notify_slack(
                f":hourglass: Elevation Call button pressed, but Grain has no Elevation "
                f"Call recording from {label} yet. Try again in a few minutes, or let "
                "the scheduled Monday-night checks pick it up."
            )
            result("No new recording in Grain yet. Try again in a few minutes.")
            return 1
        which = monday_ordinal(start.day)
        print(f"No Elevation Call recording in Grain for {label} (Monday #{which}).")
        # The call is first OR second Monday. Only sound the alarm once the
        # second Monday has come and gone with nothing posted for the month.
        if final_attempt and which >= 2:
            notify_slack(
                f":x: No Elevation Call has been posted for {month} - "
                f"the second Monday ({label}) passed with no matching Grain "
                "recording. Check that the recording title contains "
                "'Elevation', or add the link manually."
            )
            result(f"NOT posted for {month} - second Monday passed with no recording.")
            return 1
        result(f"Nothing for {label} (Monday #{which}); a later run will retry.")
        return 0  # first Monday with no call is normal - it's a second-Monday month

    rec_date = _recording_date_et(recording)
    rec_month = rec_date[:7]
    if rec_month in posted_months:
        # Manual path: the newest recording in the window is already on the page.
        print(f"Entry for {rec_month} already exists. Nothing to do.")
        result(f"Already posted for {rec_month}. Nothing new in Grain.")
        return 0

    share_url = extract_share_url(recording)
    if not share_url:
        rec_id = recording.get("id", "unknown")
        msg = (
            f"Found recording {rec_id} for {rec_date} but it has no public share URL. "
            "The recording likely needs sharing enabled in Grain."
        )
        print(msg, file=sys.stderr)
        notify_slack(f":x: Elevation Call updater: {msg}")
        result(f"Found {rec_date} but it has no public share link - enable sharing in Grain.")
        return 1

    # topic/recap start empty; scripts/write_recap.py (next workflow step) fills
    # them from the transcript. The embed renders a bare link until then.
    data["calls"].append({
        "date": rec_date,
        "url": share_url,
        "topic": "",
        "recap": "",
    })
    save_calls(data)
    print(f"Added {rec_date} -> {share_url}")
    month_name = date.fromisoformat(rec_date).strftime("%B")
    notify_slack(
        f":white_check_mark: {month_name} Elevation Call posted to "
        f"bianutrition.com/elevation-replays\n{share_url}"
    )
    result(f"Posted {rec_date} to bianutrition.com/elevation-replays")
    return 0


if __name__ == "__main__":
    sys.exit(main())
