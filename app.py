"""Dallas theatre calendar.

Reads the Onstage NTX "Up & Coming" page, has Claude Haiku (through Claude Code in
print mode, on the subscription) pull the shows out of the prose into structured
data, and writes two iCalendar feeds for Apple Calendar to site/:

  dallas-theatre.ics           one all-day event per run (opening through closing)
  dallas-theatre-openings.ics  separate opening-night and closing-night events

Runs once and exits; the GitHub Actions workflow schedules it, commits state.json
and publishes site/ to GitHub Pages. The page is only re-extracted when its
Squarespace "updated-on" stamp changes, so most runs don't call Claude. Shows that
drop off the page after closing are kept for a year so the calendar keeps its history.
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE_URL = os.environ.get("SOURCE_URL", "https://www.onstagentx.com/up-and-coming")
ROOT = Path(os.environ.get("DATA_DIR", "."))
SITE = ROOT / "site"
MODEL = os.environ.get("MODEL", "haiku")
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
TZ = ZoneInfo("America/Chicago")
UA = "Mozilla/5.0 (theatre-cal; personal calendar feed)"
LINES_PER_CHUNK = 25
KEEP_PAST_DAYS = 365


SHOW_FIELDS = {
    "company": "company name as written before the colon",
    "title": "show title",
    "start_date": "YYYY-MM-DD",
    "end_date": "YYYY-MM-DD, same as start_date for a single night",
    "venue": "venue if the listing names one for this show, else empty",
    "premiere": "world, regional, or empty",
    "website": "company website as a full https:// URL",
    "note": "short extra detail worth keeping (add-on show, festival, etc.), else empty",
}
SCHEMA = json.dumps({
    "type": "object",
    "properties": {"shows": {"type": "array", "items": {
        "type": "object",
        "properties": {k: {"type": "string", "description": d} for k, d in SHOW_FIELDS.items()},
        "required": list(SHOW_FIELDS),
        "additionalProperties": False,
    }}},
    "required": ["shows"],
    "additionalProperties": False,
})


PROMPT = """These lines are from a North Texas theatre listings page. Each line is one company: \
its name, a colon, then its upcoming shows in free prose, usually ending with the company website.

Today is {today}. Pull out every show that has at least one concrete date.

- title: the show's name only. Drop director/author credits, "(RP)"/"(WP)" tags and commentary. \
If a title has a subtitle after a dash or colon (e.g. "Sweeney Todd: The Demon Barber of Fleet Street"), \
keep the full title.
- Dates: ranges like "October 9-November 1" become start/end. Most dates have no year, so read \
each line the way a person would. The listings cover shows running now (from about {earliest}) \
through roughly the next year. A season is usually listed in order, so later shows in the same list \
can fall in the next year, but one line can hold several separate lists (a main stage and a second \
stage, add-on shows, a holiday special tacked on at the end), and each of those starts again near \
today. Use a stated year or season label (e.g. "2027 season") when there is one. "thru November 1" means it is already running: use today as the start.
- Single nights or lists of nights ("June 13 & June 27") become one entry per night.
- premiere: "world" for WP or world premiere, "regional" for RP or regional premiere, else "".
- venue: only if the line names one for that show (e.g. "@ The Reid Cabaret Theatre", "(Winspear)").
- website: the company site from the line, as a full https:// URL.
- Skip entries that are only "TBA", "watch website", or have no date.

Lines:
{lines}"""


def log(msg):
    print(f"{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def page_lines(page):
    """The company lines from the theater section, as plain text."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S)
    t = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\u200d", "")
    lines = [re.sub(r"\s+", " ", l).strip() for l in t.splitlines()]
    lines = [l for l in lines if l]
    # Theater listings sit between the intro note and the music/dance link section
    start = next((i + 1 for i, l in enumerate(lines) if l.startswith("Scroll down below")), None)
    end = next((i for i, l in enumerate(lines) if l.startswith("Links to NTX Music")), None)
    if start is None or end is None:
        raise RuntimeError("page layout changed: theater section markers not found")
    return lines[start:end]


def extract(lines):
    today = dt.datetime.now(TZ).date()
    shows = []
    for i in range(0, len(lines), LINES_PER_CHUNK):
        chunk = lines[i : i + LINES_PER_CHUNK]
        prompt = PROMPT.format(
            today=today.isoformat(),
            earliest=(today - dt.timedelta(days=60)).isoformat(),
            lines="\n".join(chunk),
        )
        # No tools: the page text is untrusted, so the model can only answer, not act
        proc = subprocess.run(
            [CLAUDE, "-p", "--model", MODEL, "--tools", "", "--output-format", "json",
             "--no-session-persistence", "--json-schema", SCHEMA],
            input=prompt, capture_output=True, text=True, timeout=900,
        )
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}")
        if out.get("is_error") or not out.get("structured_output"):
            raise RuntimeError(f"extraction failed on lines {i}-{i + len(chunk)}: "
                               f"{out.get('subtype')} {str(out.get('result'))[:300]}")
        found = out["structured_output"]["shows"]
        shows.extend(found)
        u = out.get("usage", {})
        log(f"lines {i}-{i + len(chunk)}: {len(found)} shows ({u.get('output_tokens')} tokens out)")
    return [s for s in shows if valid(s)]


def valid(s):
    try:
        a, b = dt.date.fromisoformat(s["start_date"]), dt.date.fromisoformat(s["end_date"])
    except ValueError:
        log(f"dropping {s['company']} / {s['title']}: bad date {s['start_date']}..{s['end_date']}")
        return False
    if b < a or (b - a).days > 200:
        log(f"dropping {s['company']} / {s['title']}: odd range {a}..{b}")
        return False
    return True


def uid(s):
    # Company + title + opening month, so a date tweak on the site updates the event in place
    key = f"{s['company']}|{s['title']}|{s['start_date'][:7]}".lower().replace("’", "'")
    return hashlib.sha1(key.encode()).hexdigest()[:20] + "@theatre-cal"


def merge_history(new, old):
    """Keep shows that closed and fell off the page, for up to a year."""
    today = dt.datetime.now(TZ).date()
    seen = {uid(s) for s in new}
    kept = [
        s for s in old
        if uid(s) not in seen
        and today - dt.timedelta(days=KEEP_PAST_DAYS) <= dt.date.fromisoformat(s["end_date"]) < today
    ]
    return new + kept


def esc(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, b = [], line.encode()
    while len(b) > 75:
        cut = 75
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def vevent(s, uid_, summary, start, end):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = s["start_date"] if s["start_date"] == s["end_date"] else f"{s['start_date']} to {s['end_date']}"
    desc = [s["company"], f"Run: {run}"]
    if s["premiere"]:
        desc.append(f"{s['premiere'].capitalize()} premiere")
    if s["note"]:
        desc.append(s["note"])
    desc.append(s["website"])
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid_}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
        f"DTEND;VALUE=DATE:{end + dt.timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{esc(summary)}",
        f"DESCRIPTION:{esc(chr(10).join(desc))}",
        "TRANSP:TRANSPARENT",
    ]
    if s["venue"]:
        lines.append(f"LOCATION:{esc(s['venue'])}")
    if s["website"].startswith("http"):
        lines.append(f"URL:{s['website']}")
    lines.append("END:VEVENT")
    return lines


def calendar(name, events):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//densonlab//theatre-cal//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{name}",
        "X-WR-TIMEZONE:America/Chicago",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for e in events:
        lines += e
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def write_feeds(shows):
    runs, marks = [], []
    for s in sorted(shows, key=lambda s: (s["start_date"], s["company"])):
        a, b = dt.date.fromisoformat(s["start_date"]), dt.date.fromisoformat(s["end_date"])
        base = uid(s)
        label = f"{s['title']} ({s['company']})"
        runs.append(vevent(s, base, label, a, b))
        if a == b:
            marks.append(vevent(s, "o-" + base, label, a, a))
        else:
            marks.append(vevent(s, "o-" + base, f"Opens: {label}", a, a))
            marks.append(vevent(s, "c-" + base, f"Closes: {label}", b, b))
    write_atomic(SITE / "dallas-theatre.ics", calendar("Dallas Theatre", runs))
    write_atomic(SITE / "dallas-theatre-openings.ics", calendar("Dallas Theatre (openings)", marks))


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)



def run_once():
    state_file = ROOT / "state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    page = fetch(SOURCE_URL)
    m = re.search(r'data-updated-on="(\d+)"', page)
    stamp = m.group(1) if m else hashlib.sha1(page.encode()).hexdigest()
    shows = state.get("shows", [])

    if stamp != state.get("stamp") or not shows or os.environ.get("FORCE") == "true":
        lines = page_lines(page)
        if len(lines) < 20:
            raise RuntimeError(f"only {len(lines)} listing lines found, page layout may have changed")
        log(f"page changed ({stamp}), extracting {len(lines)} lines")
        fresh = extract(lines)
        if shows and len(fresh) < len([s for s in shows if s["end_date"] >= dt.date.today().isoformat()]) / 2:
            raise RuntimeError(f"extraction found {len(fresh)} shows, far fewer than before; keeping old feed")
        shows = merge_history(fresh, shows)
        state = {"stamp": stamp, "updated": dt.datetime.now(TZ).isoformat(), "shows": shows}
        write_atomic(state_file, json.dumps(state, indent=1))
    else:
        log("page unchanged")

    write_feeds(shows)  # rewritten every run so closed shows age out on schedule
    write_index(len(shows), state.get("updated", ""))


def write_index(count, updated):
    base = "dallas-theatre"
    write_atomic(SITE / "index.html", f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dallas Theatre Calendar</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}}code{{word-break:break-all}}</style>
<h1>Dallas Theatre Calendar</h1>
<p>DFW theatre listings from <a href="{SOURCE_URL}">Onstage NTX Up &amp; Coming</a>, as calendar feeds. {count} shows; listings last changed {updated[:10]}.</p>
<p>Subscribe in Apple Calendar (File &rarr; New Calendar Subscription) or on iPhone (Settings &rarr; Apps &rarr; Calendar &rarr; Calendar Accounts &rarr; Add Account &rarr; Other &rarr; Add Subscribed Calendar):</p>
<ul>
<li>Openings and closings only: <a href="{base}-openings.ics"><code>{base}-openings.ics</code></a></li>
<li>Full runs, one event per show: <a href="{base}.ics"><code>{base}.ics</code></a></li>
</ul>
""")


if __name__ == "__main__":
    run_once()
