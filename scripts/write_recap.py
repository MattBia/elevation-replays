#!/usr/bin/env python3
"""
Elevation Call recap writer.

Runs right after update_elevation.py in the same workflow. For every entry in
elevation.json whose `topic` or `recap` is blank, it pulls the call's transcript
from Grain, asks Claude to write the topic + recap in the house style, checks the
result against the style rules, and writes it into elevation.json.

Env vars:
  GRAIN_API_TOKEN_V2  required - transcript fetch
  ANTHROPIC_API_KEY   required - if missing, prints a warning and exits 0 so the
                      link still posts; the recap gets written on the next run
                      that has the key
  SLACK_WEBHOOK_URL   optional - posts the written recap, or the reason it wasn't

Exit codes:
  0 = recaps written, or nothing to do, or a soft skip (no key / no transcript yet)
  1 = hard error talking to Grain or Claude

House style (mirrors README "Writing the recap"):
  - topic: short Title Case phrase, becomes the card headline
  - recap: 1-2 sentences, ~30-45 words, second person, plain language
  - lead with what the call was actually about; keep the one specific that
    makes it worth clicking
  - no client names, no individual wins, no raffle/prize talk, no logistics

Ends with a "RESULT: ..." line only when it did something (the Stream Deck
button shows the last RESULT line in the job log).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests

GRAIN_TOKEN = os.environ.get("GRAIN_API_TOKEN_V2", "").strip()
GRAIN_HEADERS = {
    "Authorization": f"Bearer {GRAIN_TOKEN}",
    "Public-Api-Version": "2025-10-31",
}
GRAIN_TRANSCRIPT_URL = "https://api.grain.com/_/public-api/v2/recordings/{id}/transcript.txt"
CALLS_PATH = Path(__file__).resolve().parent.parent / "elevation.json"

MODEL = "claude-opus-5"
HOSTS = {"michelle", "matt", "chelley", "coach"}   # never treated as client names
BANNED = re.compile(r"\b(raffle|prize|giveaway|winner|drawing|gift card)\b", re.I)
# AI tells Matt has banned across all BIA-facing copy (2026-09-01 / 2026-09-08).
DASHES = re.compile(r"[\u2014\u2013]|\s-\s|--")
CONTRASTS = [
    # "It's not a diet. It's a lifestyle." (sentence-split version of the contrast)
    re.compile(r"\b(it'?s|that'?s|this is)\s+not\b[^.;!?]{1,60}[.;]\s*(it'?s|that'?s|this is)\b", re.I),
    re.compile(r"\b(not|isn'?t|aren'?t|wasn'?t)\s+(just|only|about|because)\b", re.I),
    re.compile(r"\b(not|isn'?t|aren'?t)\b[^.;!?]{1,60},\s*(it'?s|that'?s|they'?re|but|just)\b", re.I),
    re.compile(r",\s*not\s+(a|an|the|your|to|just|about)\b", re.I),
    re.compile(r"\b(less about|more than just|rather than [^.;]{1,40}, it)\b", re.I),
]
CALLOUTS = re.compile(r"\b(sound familiar|this (one|call) is for you|if you'?ve ever)\b", re.I)
MAX_RECAP_WORDS = 55
MAX_TOPIC_WORDS = 9

SYSTEM_PROMPT = """You write the short recap that sits under each replay link on the members-only
"Elevation Call Replays" page for BIA Nutrition, a women's nutrition-coaching company.
The monthly Elevation Call is a one-hour group coaching call led by Michelle (the founder).
Members skim this page on their phones to decide which replay to watch.

Write a `topic` and a `recap` for the call from its transcript.

topic: a short Title Case phrase (2-7 words). It becomes the card headline.
recap: 1-2 sentences, 30-45 words, second person ("you"), plain language.
  - Lead with what the call was actually about: the idea, framework, or theme.
  - Keep the one specific that makes it worth clicking (a study, an acronym,
    a named exercise), but only if it is genuinely central.
  - Ground everything in what was actually said. Do not invent frameworks,
    statistics, or claims that are not in the transcript.

Hard rules:
  - No client or member names, ever. Members other than the host must not be
    identifiable in any way.
  - No individual wins or shout-outs, no raffle/prize/giveaway talk, no logistics
    (dates, links, homework reminders, tech issues, who joined late).
  - Do not mention Michelle or the host by name in the recap.
  - Do not start with "In this call" or "This call".

Voice rules (these are checked mechanically and a draft that breaks them is thrown out):
  - No em dashes or en dashes anywhere. Use a period, a comma, or "to" for ranges.
  - No "it's not X, it's Y" / "not about X, about Y" / "not just X, but Y" /
    "X, not Y" contrast constructions of any kind. Say the thing directly.
  - No rhetorical questions.
  - No reader call-outs like "sound familiar?" or "this one is for you".
  - No neat parallel triads for rhythm.
  The test: would a busy person writing a quick note to members have typed this
  sentence? Short plain sentences, specific to what was actually said.

Here are recent recaps from the page, in the voice to match:
"""


def notify_slack(message: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        r = requests.post(url, json={"text": message}, timeout=10)
        print(f"Slack notified ({r.status_code}).")
    except requests.RequestException as e:
        print(f"Slack notify failed: {e}")


def result(msg: str) -> None:
    print(f"RESULT: {msg}")


def recording_id(url: str) -> str | None:
    m = re.search(r"/share/recording/([0-9a-f-]{36})/", url)
    return m.group(1) if m else None


def fetch_transcript(rec_id: str) -> str:
    resp = requests.get(
        GRAIN_TRANSCRIPT_URL.format(id=rec_id), headers=GRAIN_HEADERS, timeout=60
    )
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return resp.text.strip()


def speaker_first_names(transcript: str) -> set[str]:
    """First names of everyone who spoke, minus the hosts - used to catch leaks."""
    names = set()
    # Tolerates "Name:", "Name (00:12):", "[00:12] Name:", "00:12:03 Name:" line shapes.
    pattern = r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*[-\u2013]?\s*)?([A-Z][a-zA-Z'\-]+)(?: [A-Z][a-zA-Z'\-]+)*\s*[:(\[]"
    for m in re.finditer(pattern, transcript, re.M):
        first = m.group(1)
        if first.lower() not in HOSTS:
            names.add(first)
    return names


def style_problems(topic: str, recap: str, names: set[str]) -> list[str]:
    problems = []
    if not topic.strip():
        problems.append("topic is empty")
    if len(topic.split()) > MAX_TOPIC_WORDS:
        problems.append(f"topic is {len(topic.split())} words (max {MAX_TOPIC_WORDS})")
    words = len(recap.split())
    if words < 15:
        problems.append(f"recap is only {words} words")
    if words > MAX_RECAP_WORDS:
        problems.append(f"recap is {words} words (max {MAX_RECAP_WORDS})")
    if BANNED.search(recap) or BANNED.search(topic):
        problems.append("mentions raffle/prize/giveaway")
    leaked = [n for n in names if re.search(rf"\b{re.escape(n)}\b", recap + " " + topic)]
    if leaked:
        problems.append(f"names a participant: {', '.join(sorted(leaked))}")
    if re.search(r"\bMichelle\b", recap):
        problems.append("names the host")
    text = topic + " " + recap
    if DASHES.search(text):
        problems.append("uses a dash (em/en dash or ' - '); use a period or comma instead")
    for pat in CONTRASTS:
        m = pat.search(text)
        if m:
            problems.append(f"uses a 'not X, it's Y' style contrast ({m.group(0)!r}); say it directly")
            break
    if "?" in text:
        problems.append("contains a question; no rhetorical questions")
    if CALLOUTS.search(text):
        problems.append("uses a reader call-out phrase")
    return problems


def examples_block(calls: list[dict], exclude_date: str) -> str:
    done = [c for c in calls if c.get("topic") and c.get("recap") and c["date"] != exclude_date]
    done.sort(key=lambda c: c["date"], reverse=True)
    return "\n".join(
        f"- {c['topic']}: {c['recap']}" for c in done[:4]
    )


def ask_claude(client, transcript: str, call_date: str, examples: str, extra_note: str = "") -> dict:
    month = date.fromisoformat(call_date).strftime("%B %Y")
    user = (
        f"Transcript of the {month} Elevation Call:\n\n<transcript>\n{transcript}\n</transcript>\n\n"
        "Write the topic and recap." + (f"\n\nNote from the style check on your last attempt: {extra_note}" if extra_note else "")
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT + examples,
        messages=[{"role": "user", "content": user}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "recap": {"type": "string"},
                    },
                    "required": ["topic", "recap"],
                    "additionalProperties": False,
                },
            }
        },
        # Opt into Anthropic's server-side refusal fallback (routes a declined
        # request to another model inside the same call). Sent as raw body/header
        # so this works on any SDK version.
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write this recap (stop_reason=refusal)")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return {"topic": data["topic"].strip(), "recap": " ".join(data["recap"].split())}


def main() -> int:
    data = json.loads(CALLS_PATH.read_text())
    calls = data["calls"]
    pending = [c for c in calls if not c.get("topic") or not c.get("recap")]
    if not pending:
        print("Every call has a topic and recap. Nothing to do.")
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        msg = (f"{len(pending)} call(s) need a recap but ANTHROPIC_API_KEY is not set - "
               "add it as a repo secret and re-run.")
        print(f"WARNING: {msg}")
        notify_slack(f":warning: Elevation recap not written: {msg}")
        result(f"Link posted; recap NOT written (ANTHROPIC_API_KEY missing).")
        return 0
    if not GRAIN_TOKEN:
        print("GRAIN_API_TOKEN_V2 is not set.", file=sys.stderr)
        return 1

    import anthropic  # imported here so a missing key never trips on a missing package
    client = anthropic.Anthropic(api_key=api_key)

    written, skipped = [], []
    for call in sorted(pending, key=lambda c: c["date"], reverse=True):
        rec_id = recording_id(call["url"])
        if not rec_id:
            skipped.append(f"{call['date']}: URL is not a Grain share link")
            continue
        try:
            transcript = fetch_transcript(rec_id)
        except requests.RequestException as e:
            print(f"Grain transcript error for {call['date']}: {e}", file=sys.stderr)
            notify_slack(f":warning: Elevation recap: Grain transcript fetch failed for {call['date']}: {e}")
            return 1
        if len(transcript.split()) < 300:
            skipped.append(f"{call['date']}: transcript not available yet ({len(transcript.split())} words)")
            continue

        names = speaker_first_names(transcript)
        # Observability for the name-leak guard: if this is 0 on a real call,
        # Grain's transcript line format changed and the regex needs updating.
        print(f"{call['date']}: transcript {len(transcript.split())} words, "
              f"{len(names)} non-host speaker name(s) detected for the leak check")
        examples = examples_block(calls, call["date"])
        note = ""
        out, problems = None, ["not attempted"]
        for attempt in range(2):
            try:
                out = ask_claude(client, transcript, call["date"], examples, note)
            except anthropic.APIStatusError as e:
                print(f"Claude API error ({e.status_code}) for {call['date']}: {e.message}", file=sys.stderr)
                notify_slack(f":warning: Elevation recap: Claude API error for {call['date']}: {e.status_code}")
                return 1
            except (anthropic.APIConnectionError, RuntimeError) as e:
                print(f"Claude error for {call['date']}: {e}", file=sys.stderr)
                notify_slack(f":warning: Elevation recap: {e}")
                return 1
            problems = style_problems(out["topic"], out["recap"], names)
            if not problems:
                break
            note = "; ".join(problems) + ". Fix these and keep everything else."
            print(f"Attempt {attempt + 1} for {call['date']} failed style check: {note}")

        if problems:
            skipped.append(f"{call['date']}: draft failed style check ({'; '.join(problems)})")
            notify_slack(
                f":warning: Elevation recap for {call['date']} was NOT saved - the draft "
                f"failed the style check ({'; '.join(problems)}). Write it by hand or re-run."
            )
            continue

        call["topic"], call["recap"] = out["topic"], out["recap"]
        written.append(call)
        print(f"Recap for {call['date']}: {out['topic']} - {out['recap']}")

    if written:
        calls.sort(key=lambda c: c["date"], reverse=True)
        CALLS_PATH.write_text(json.dumps(data, indent=2) + "\n")
        for c in written:
            month = date.fromisoformat(c["date"]).strftime("%B")
            notify_slack(
                f":memo: {month} Elevation Call recap written:\n*{c['topic']}*\n{c['recap']}\n"
                f"Edit elevation.json if you want to tweak it."
            )
    for s in skipped:
        print(f"Skipped {s}")

    if written:
        result(f"Posted {written[0]['date']} + recap: {written[0]['topic']}")
    elif skipped:
        result(f"Link posted; recap pending ({skipped[0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
