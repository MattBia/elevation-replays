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


def self_check() -> int:
    """Verify both credentials work, without touching elevation.json.

    Run any day via the workflow's `check_only` dispatch input. The normal
    path bails on non-Mondays before it ever calls Grain, so this is the
    only way to confirm the secrets are actually good outside of a Monday.
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


def main() -> int:
    if os.environ.get("CHECK_ONLY", "").lower() == "true":
        return self_check()

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
