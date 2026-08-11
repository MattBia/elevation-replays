#!/usr/bin/env python3
"""
BIA Monthly Elevation Call updater.

Runs Monday evenings via GitHub Actions. If an Elevation Call happened today,
grabs its Grain share URL and upserts it into elevation.json (keyed by date, so
re-runs are harmless).

The `topic` and `recap` fields are left blank on purpose - they require reading
the call and writing a client-facing summary, which is a judgment call, not a
string transform. The embed hides blank fields, so a new entry shows up as a
plain link until the recap is filled in. Slack gets pinged when that's pending.

The call lands on the first OR second Monday of the month, so this runs every
Monday and simply exits quietly when there's nothing to post. A month is only
treated as MISSED if the second Monday passes with no entry for that month.

Env vars:
  GRAIN_API_TOKEN_V2  required - same token BIA's onboarding automation uses
  SLACK_WEBHOOK_URL   optional - posts a note on success or final failure
  FINAL_ATTEMPT       optional - "true" on the last cron slot of the evening;
                      controls whether a miss is treated as a failure

Exit codes:
  0 = entry added, already present, or nothing expected today
  1 = hard error, or the month's call is missing after its second Monday
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# Grain v2 public API. Recording search is a POST with a JSON filter body;
# this matches the contract BIA's client_onboarding.py already uses in prod.
GRAIN_RECORDINGS_URL = "https://api.grain.com/_/public-api/v2/recordings"
GRAIN_HEADERS = {
    "Authorization": f"Bearer {os.environ['GRAIN_API_TOKEN_V2']}",
    "Public-Api-Version": "2025-10-31",
    "Content-Type": "application/json",
}
CALLS_PATH = Path(__file__).resolve().parent.parent / "elevation.json"
TITLE_KEYWORD = "elevation"  # real title is "BIA Monthly Elevation Call"
ET = ZoneInfo("America/New_York")


def notify_slack(message: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL")
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


def fetch_todays_call(today: str) -> dict | None:
    """Return the Grain recording dict for today's Elevation Call, or None.

    `today` is a YYYY-MM-DD string in ET. Search is a POST with a title filter
    (Grain v2), then we keep only recordings that (a) contain the "Elevation"
    keyword and (b) actually started today in ET.
    """
    resp = requests.post(
        GRAIN_RECORDINGS_URL,
        headers=GRAIN_HEADERS,
        json={"filter": {"title_search": "Elevation"}},
        timeout=30,
    )
    resp.raise_for_status()
    recordings = resp.json().get("recordings", [])

    matches = [
        r for r in recordings
        if TITLE_KEYWORD in (r.get("title") or "").lower()
        and _recording_date_et(r) == today
    ]
    if not matches:
        return None
    # If somehow multiple today, take the most recent start.
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


def main() -> int:
    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")
    this_month = now_et.strftime("%Y-%m")
    final_attempt = os.environ.get("FINAL_ATTEMPT", "").lower() == "true"

    if now_et.weekday() != 0:  # 0 = Monday
        print(f"{today} is not a Monday in ET. Nothing to do.")
        return 0

    data = load_calls()

    # Idempotency: bail if this month is already posted (earlier Monday or slot)
    if any(c["date"].startswith(this_month) for c in data["calls"]):
        print(f"Entry for {this_month} already exists. Nothing to do.")
        return 0

    try:
        recording = fetch_todays_call(today)
    except requests.RequestException as e:
        print(f"Grain API error: {e}", file=sys.stderr)
        notify_slack(f":warning: Elevation Call updater hit a Grain API error: {e}")
        return 1

    if recording is None:
        which = monday_ordinal(now_et.day)
        print(f"No Elevation Call recording in Grain for {today} (Monday #{which}).")
        # The call is first OR second Monday. Only sound the alarm once the
        # second Monday has come and gone with nothing posted for the month.
        if final_attempt and which >= 2:
            notify_slack(
                f":x: No Elevation Call has been posted for {this_month} - "
                f"the second Monday ({today}) passed with no matching Grain "
                "recording. Check that the recording title contains "
                "'Elevation', or add the link manually."
            )
            return 1
        return 0  # first Monday with no call is normal - it's a second-Monday month

    share_url = extract_share_url(recording)
    if not share_url:
        rec_id = recording.get("id", "unknown")
        msg = (
            f"Found recording {rec_id} for {today} but it has no public share URL. "
            "The recording likely needs sharing enabled in Grain."
        )
        print(msg, file=sys.stderr)
        notify_slack(f":x: Elevation Call updater: {msg}")
        return 1

    # topic/recap stay empty until a human (or Claude) writes them - the embed
    # renders the entry as a bare link in the meantime rather than breaking.
    data["calls"].append({
        "date": today,
        "url": share_url,
        "topic": "",
        "recap": "",
    })
    save_calls(data)
    print(f"Added {today} -> {share_url}")
    notify_slack(
        f":white_check_mark: {now_et.strftime('%B')} Elevation Call posted to "
        f"bianutrition.com/elevation-replays\n{share_url}\n"
        ":pencil: Still needs a *topic* and *recap* - run the recap step to fill them in."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
