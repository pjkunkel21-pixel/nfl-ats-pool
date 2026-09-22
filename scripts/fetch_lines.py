#!/usr/bin/env python3
"""
Pull the week's NFL point spreads and write them to data/lines/week-N.json.

By default the numbers come from the sportsbook line ESPN publishes alongside
its scoreboard (DraftKings at the time of writing). ESPN supplies the matchup,
home/away, kickoff and the spread in one CORS-open call, so there is nothing to
OCR and no extra binaries to install.

Book lines move all week, so a week is priced ONCE: the first run that finds a
number keeps it, and later runs only fill in games that were still blank. Pass
--force to deliberately re-price a week, or fix an individual game by hand in
admin.html (hand-set lines always survive a re-run).

The original Circa Sports Million path is still here behind --circa: it finds
the contest's image-only PDF via the circasports.com WordPress media API, OCRs
the two-column grid, and cross-checks every parsed game against ESPN's
schedule. It needs pdftoppm (poppler-utils) and tesseract on the PATH.

Usage:
    python fetch_lines.py                 # current week, sportsbook lines
    python fetch_lines.py --week 5        # a specific week
    python fetch_lines.py --week 5 --force          # re-price it at today's number
    python fetch_lines.py --week 5 --schedule-only  # matchups only, fill in by hand
    python fetch_lines.py --circa                   # Circa contest PDF instead
    python fetch_lines.py --circa --pdf file.pdf --week 5
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

MEDIA_API = "https://www.circasports.com/wp-json/wp/v2/media"
ESPN_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)
UA = "nfl-ats-pool/1.0 (+https://github.com)"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_DIR = os.path.join(REPO_ROOT, "data", "lines")

# Circa prints team nicknames in caps. Map every nickname (and the common OCR
# manglings of it) to an ESPN team abbreviation.
TEAMS = {
    "CARDINALS": "ARI", "FALCONS": "ATL", "RAVENS": "BAL", "BILLS": "BUF",
    "PANTHERS": "CAR", "BEARS": "CHI", "BENGALS": "CIN", "BROWNS": "CLE",
    "COWBOYS": "DAL", "BRONCOS": "DEN", "LIONS": "DET", "PACKERS": "GB",
    "TEXANS": "HOU", "COLTS": "IND", "JAGUARS": "JAX", "CHIEFS": "KC",
    "RAIDERS": "LV", "CHARGERS": "LAC", "RAMS": "LAR", "DOLPHINS": "MIA",
    "VIKINGS": "MIN", "PATRIOTS": "NE", "SAINTS": "NO", "GIANTS": "NYG",
    "JETS": "NYJ", "EAGLES": "PHI", "STEELERS": "PIT", "49ERS": "SF",
    "SEAHAWKS": "SEA", "BUCS": "TB", "BUCCANEERS": "TB",
    "COMMANDERS": "WSH", "TITANS": "TEN",
}
# Characters tesseract commonly returns in place of the vulgar fraction "1/2".
HALF_CHARS = "½%¥Yy)/,'\"‚`h·"
# Glyphs a lone "1/2" can come back as. Digits are deliberately excluded so a
# genuine one-point spread is never mistaken for half a point.
BARE_HALF = HALF_CHARS + "il|!ItLJ"
# Glyphs tesseract returns in place of a digit inside a spread.
DIGIT_REPAIR = str.maketrans({
    "]": "7", "}": "7", "T": "7", "?": "7",
    "l": "1", "I": "1", "|": "1",
    "O": "0", "o": "0", "S": "5", "B": "8", "G": "6",
})


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def need(binary: str) -> None:
    if subprocess.run(["which", binary], capture_output=True).returncode != 0:
        sys.exit(
            f"error: '{binary}' is not installed.\n"
            "  Debian/Ubuntu: sudo apt-get install poppler-utils tesseract-ocr"
        )


def nfl_season_year(today: dt.date | None = None) -> int:
    """The NFL season a date belongs to (Jan/Feb belong to the prior season)."""
    today = today or dt.date.today()
    return today.year - 1 if today.month <= 2 else today.year


# --------------------------------------------------------------------------
# 1. find the PDF
# --------------------------------------------------------------------------

def season_of_publish(date_str: str) -> int | None:
    """Which NFL season a Circa sheet published on this date belongs to.

    Weeks 17 and 18 are posted in January, i.e. the calendar year *after* the
    season year -- so the year in the URL is not enough to tell seasons apart.
    """
    m = re.match(r"(\d{4})-(\d{2})", date_str or "")
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    return year - 1 if month <= 2 else year


def find_spreads_pdf(week: int | None, season: int) -> tuple[str, int]:
    """Return (pdf_url, week). Newest published week if week is None."""
    found: dict[int, tuple[str, str]] = {}
    for term in ("Contest Point Spreads", "Point Spreads Week", "Spreads"):
        q = urllib.parse.urlencode(
            {"search": term, "per_page": 60, "orderby": "date", "order": "desc"}
        )
        try:
            items = get_json(f"{MEDIA_API}?{q}")
        except Exception:
            continue
        for m in items:
            url = m.get("source_url") or ""
            hit = re.search(r"Spreads?[-_ ]*Week[-_ ]*(\d+)\.pdf$", url, re.I)
            if not hit:
                continue
            wk, date = int(hit.group(1)), m.get("date", "")
            # Only this season's sheets. Judged by publish date, not by the
            # year in the URL -- last season's Week 18 sheet lives under
            # /<season+1>/01/ and would otherwise look like this season's.
            if season_of_publish(date) != season:
                continue
            if wk not in found or date > found[wk][1]:
                found[wk] = (url, date)
        if found:
            break
    if not found:
        raise RuntimeError(
            f"no {season} contest point-spread PDFs found on circasports.com yet. "
            "Circa posts them around 10am PT on Thursdays; if the sheet is out, "
            "download it and use admin.html or --pdf."
        )
    if week is None:
        newest = max(found.items(), key=lambda kv: kv[1][1])
        return newest[1][0], newest[0]
    if week in found:
        return found[week][0], week
    raise RuntimeError(
        f"week {week} not published yet (found weeks: {sorted(found)})"
    )


# --------------------------------------------------------------------------
# 2. OCR
# --------------------------------------------------------------------------

def _page_px(pdf: str, dpi: int) -> tuple[int, int]:
    """Page size in pixels at this dpi, from pdfinfo."""
    out = subprocess.run(["pdfinfo", pdf], capture_output=True, text=True).stdout
    m = re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", out)
    if not m:
        return 0, 0
    return (int(float(m.group(1)) * dpi / 72), int(float(m.group(2)) * dpi / 72))


def ocr_pdf(pdf_bytes: bytes) -> list[str]:
    """OCR the sheet several ways and return every pass's text.

    Circa prints two columns of games side by side. Tesseract in single-block
    mode tries to read across both at once and drops rows where the columns
    don't line up, so each half is also read on its own. Passes are combined by
    majority vote in vote_rows(), which makes a row only has to survive one of
    them.
    """
    need("pdftoppm")
    need("tesseract")
    dpi = 300
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "spreads.pdf")
        with open(pdf, "wb") as f:
            f.write(pdf_bytes)

        width, height = _page_px(pdf, dpi)
        # full page, then the left and right halves with a little overlap
        crops = [("full", [])]
        if width and height:
            half, over = width // 2, width // 20
            crops += [
                ("left",  ["-x", "0", "-y", "0", "-W", str(half + over), "-H", str(height)]),
                ("right", ["-x", str(half - over), "-y", "0",
                           "-W", str(width - half + over), "-H", str(height)]),
            ]

        texts = []
        for name, crop in crops:
            stem = os.path.join(tmp, name)
            r = subprocess.run(
                ["pdftoppm", "-r", str(dpi), "-png", "-gray"] + crop + [pdf, stem],
                capture_output=True,
            )
            if r.returncode != 0:
                continue
            pages = sorted(f for f in os.listdir(tmp)
                           if f.startswith(name) and f.endswith(".png"))
            for page in pages:
                res = subprocess.run(
                    ["tesseract", os.path.join(tmp, page), "-", "--psm", "6"],
                    capture_output=True, text=True,
                )
                if res.returncode == 0:
                    texts.append(res.stdout)
        if not texts:
            raise RuntimeError("could not OCR the PDF")
        return texts


# --------------------------------------------------------------------------
# 3. parse
# --------------------------------------------------------------------------

# Glyphs tesseract routinely swaps for one another. Folding both the OCR text
# and the real nickname through this table makes "AYERS" match "49ERS" and
# "TLTANS" match "TITANS".
CONFUSABLE = str.maketrans({
    "0": "O", "1": "I", "L": "I", "2": "Z", "4": "A",
    "5": "S", "6": "G", "8": "B", "9": "Y",
})


def _fold(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper()).translate(CONFUSABLE)


_FOLDED = {_fold(name): abbr for name, abbr in TEAMS.items()}


def match_team(token: str) -> str | None:
    """Fuzzy-match one OCR'd nickname to an ESPN abbreviation."""
    t = re.sub(r"[^A-Z0-9]", "", token.upper())
    if not t:
        return None
    if t in TEAMS:
        return TEAMS[t]
    folded = _fold(t)
    if folded in _FOLDED:
        return _FOLDED[folded]
    # last resort: closest nickname, but only if it is a clear winner
    import difflib
    scored = sorted(
        ((difflib.SequenceMatcher(None, folded, f).ratio(), abbr)
         for f, abbr in _FOLDED.items()),
        reverse=True,
    )
    if scored and scored[0][0] >= 0.75 and (
        len(scored) < 2 or scored[0][0] - scored[1][0] >= 0.08
    ):
        return scored[0][1]
    return None


def parse_spread(token: str):
    """'-3Y,' -> -3.5 ; '+11%' -> 11.5 ; 'PK' -> 0.0 ; unparseable -> None."""
    t = token.strip()
    if re.fullmatch(r"[PpEeRrKkOo]{2,3}", t):     # PK / PIC / OCR'd variants
        return 0.0
    sign = -1.0 if t[:1] in "-–—~" else 1.0
    body = t[1:] if t[:1] in "-–—~+" else t
    # A half-point spread prints as a bare "1/2" with no whole number in front,
    # so there is no digit for the usual path to find. Only non-digit glyphs
    # count here, so a real "+1" still falls through and reads as one point.
    if body and all(c in BARE_HALF for c in body):
        return sign * 0.5
    digits = re.search(r"\d+", body)
    if not digits:
        # No digit survived OCR -- repair the glyphs that stand in for one.
        # (Half-point marks are left alone; they are handled below.)
        repaired = "".join(
            c if c in HALF_CHARS else c.translate(DIGIT_REPAIR) for c in body
        )
        digits = re.search(r"\d+", repaired)
        if not digits:
            return None
        body = repaired
    val = float(digits.group(0))
    if val > 30:                                   # e.g. '35' read from '3 1/2'
        s = digits.group(0)
        if s.endswith("5") and len(s) > 1:
            val = float(s[:-1]) + 0.5
        else:
            return None
    tail = body[digits.end():]
    if any(c in HALF_CHARS for c in tail):
        val += 0.5
    return sign * val


# A spread cell: a sign and something numeric, or a pick'em.
_SPREAD = r"(?:[-+–—~]\s?[^\s]{1,5})|(?:[Pp][KkRr])"

# What every row really carries is a nickname next to a spread. The date, the
# kickoff time and the rotation number are decoration -- the rotation number is
# never used -- and depending on them is what broke when Circa restyled the
# sheet and the OCR started running "Sep 13" and "8" together as "Sep 138".
# Small amounts of punctuation noise can land between the two ("COMMANDERS . _").
TEAM_SPREAD = re.compile(
    r"""(?P<team>[A-Z0-9][A-Z0-9'.\-]{1,14}(?:\s[A-Z0-9'.\-]{2,14})?)
        [\s._:=~—-]{1,6}
        (?P<spread>""" + _SPREAD + r""")
        (?=\s|$)""",
    re.X,
)


def parse_rows(text: str) -> list[dict]:
    """Every (team, spread) cell on the sheet.

    Anchored on the nickname rather than on the row's layout, so a restyled
    sheet still parses. Anything that isn't one of the 32 nicknames is dropped
    here, and anything that isn't in ESPN's schedule is dropped in reconcile().
    """
    rows = []
    for line in text.splitlines():
        for m in TEAM_SPREAD.finditer(line):
            abbr = match_team(m.group("team"))
            if abbr is None:
                continue
            spread = parse_spread(m.group("spread"))
            if spread is None:
                continue
            rows.append({
                "team": abbr,
                "spread": spread,
                "raw_team": m.group("team").strip(),
                "raw_spread": m.group("spread").strip(),
                "column": m.start(),
            })
    return rows


# --------------------------------------------------------------------------
# 4. ESPN schedule + reconciliation
# --------------------------------------------------------------------------

def vote_rows(passes: list[list[dict]]) -> list[dict]:
    """One spread per team, by majority across the OCR passes.

    A glyph misread rarely repeats identically across a full-page read and a
    single-column read, so the reading that shows up most often is almost
    always the right one.
    """
    tally: dict[str, dict[float, int]] = {}
    sample: dict[str, dict] = {}
    for rows in passes:
        for r in rows:
            tally.setdefault(r["team"], {})
            tally[r["team"]][r["spread"]] = tally[r["team"]].get(r["spread"], 0) + 1
            sample.setdefault(r["team"], r)
    out = []
    for team, votes in tally.items():
        best = max(votes.items(), key=lambda kv: (kv[1], -abs(kv[0])))[0]
        row = dict(sample[team])
        row["spread"] = best
        row["votes"] = votes[best]
        out.append(row)
    return out


def espn_schedule(season: int, week: int) -> list[dict]:
    url = f"{ESPN_SCOREBOARD}?dates={season}&seasontype=2&week={week}"
    data = get_json(url)
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        home = away = None
        for c in comp.get("competitors", []):
            abbr = (c.get("team") or {}).get("abbreviation")
            if c.get("homeAway") == "home":
                home = abbr
            else:
                away = abbr
        if not home or not away:
            continue
        # ESPN also carries a sportsbook line. It is not the Circa contest
        # number, but it is a usable stand-in before Circa posts on Thursday.
        book = None
        odds = comp.get("odds") or []
        if odds:
            o = odds[0]
            spread, details = o.get("spread"), (o.get("details") or "").strip()
            # `spread` is quoted from the home team's side; confirm against the
            # printed detail line, which names the favorite explicitly.
            m = re.match(r"^([A-Z0-9]{2,4})\s+([-+]?\d+(?:\.\d)?)$", details)
            if m and spread is not None:
                fav, num = m.group(1), float(m.group(2))
                book = num if fav == home else -num
            elif spread is not None:
                book = float(spread)
            elif re.fullmatch(r"(?i)even|pk|pick", details):
                book = 0.0

        games.append({
            "espnId": ev.get("id"),
            "home": home,
            "away": away,
            "kickoff": ev.get("date"),
            "shortName": ev.get("shortName"),
            "bookSpread": book,
            # ESPN fills in displayName on some events and only name on others.
            "bookProvider": (((odds[0].get("provider") or {}).get("displayName")
                              or (odds[0].get("provider") or {}).get("name"))
                             if odds else None),
        })
    games.sort(key=lambda g: g["kickoff"] or "")
    return games


def current_nfl_week(season: int) -> int | None:
    """Ask ESPN which regular-season week to pull.

    ESPN keeps pointing at a week for a day or two after its last game ends --
    on a Tuesday morning it still says "week 2" with all sixteen games final.
    Taking that at face value means the Tuesday cron re-pulls a finished week
    and never creates the next one, so once every game is complete we advance.
    """
    try:
        data = get_json(ESPN_SCOREBOARD)
        wk = (data.get("week") or {}).get("number")
        styp = (data.get("season") or {}).get("type")
        if isinstance(styp, dict):
            styp = styp.get("type")
        if not wk or styp not in (None, 2):
            return None
        wk = int(wk)
        events = data.get("events") or []
        done = [(((e.get("competitions") or [{}])[0].get("status") or {})
                 .get("type") or {}).get("completed") for e in events]
        if done and all(done):
            wk = min(wk + 1, 18)
        return wk
    except Exception:
        return None


def previous_week_meta(week: int) -> dict:
    """The source/warnings already recorded for a week, if any."""
    path = os.path.join(LINES_DIR, f"week-{week}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def reconcile(rows: list[dict], schedule: list[dict],
              strict: bool) -> tuple[list[dict], list[str]]:
    """Attach each OCR'd spread to the ESPN game it belongs to.

    Circa prints both halves of every game, so the two spreads must be exact
    negations of each other. That gives a free integrity check: a game whose
    two sides agree is 'verified'; anything else is called out.
    """
    by_team: dict[str, list[dict]] = {}
    for r in rows:
        by_team.setdefault(r["team"], []).append(r)

    games, warnings = [], []
    for g in schedule:
        home_spread = next((r["spread"] for r in by_team.get(g["home"], [])
                            if r["spread"] is not None), None)
        away_spread = next((r["spread"] for r in by_team.get(g["away"], [])
                            if r["spread"] is not None), None)

        if home_spread is None and away_spread is None:
            status, note, spread = "missing", "no spread found on the Circa sheet", None
        elif home_spread is None:
            status, note, spread = ("unverified",
                                    "read from the away side only", -away_spread)
        elif away_spread is None:
            status, note, spread = ("unverified",
                                    "read from the home side only", home_spread)
        elif abs(home_spread + away_spread) < 1e-9:
            status, note, spread = "verified", "", home_spread
        elif abs(abs(home_spread) - abs(away_spread)) < 1e-9:
            # Same number, both sides read with the same sign -- one sign is a
            # misread. The away row's sign is the one printed next to the
            # underdog/favorite label, so trust it.
            status, note, spread = ("unverified",
                                    "sign disagreement resolved from the away side",
                                    -away_spread)
        elif (home_spread * away_spread < 0
              and abs(abs(home_spread) - abs(away_spread)) == 0.5):
            # One side lost its "1/2" glyph. OCR drops the fraction, it never
            # invents one, so the half-point reading is the correct magnitude.
            mag = max(abs(home_spread), abs(away_spread))
            status = "unverified"
            note = "half-point restored from the other side"
            spread = mag if home_spread > 0 else -mag
        else:
            status, spread = "conflict", home_spread
            note = (f"the two sides disagree (home {home_spread:+g} / "
                    f"away {away_spread:+g}) -- check this one by hand")

        games.append({
            "espnId": g["espnId"],
            "away": g["away"],
            "home": g["home"],
            "kickoff": g["kickoff"],
            # The spread is always stated from the HOME team's perspective:
            # -3.5 means the home team is laying 3.5 points.
            "spread": spread,
            "status": status,
            "note": note,
        })
        if status in ("missing", "conflict"):
            warnings.append(f"{g['away']} @ {g['home']}: {note}")
        elif status == "unverified":
            warnings.append(f"{g['away']} @ {g['home']}: {spread:+g} ({note})")

    hard = [g for g in games if g["status"] == "conflict"]
    if strict and hard:
        raise RuntimeError(
            "line parsing failed on:\n  "
            + "\n  ".join(f"{g['away']} @ {g['home']}: {g['note']}" for g in hard)
        )
    return games, warnings


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def keep_manual(week: int, games: list[dict], freeze: bool = False) -> list[dict]:
    """Don't let a re-run overwrite a line that is already settled.

    A spread edited in admin.html is marked "manual". The scraper has no way to
    know better than the person who read the sheet, so those survive a re-run
    unless --force says otherwise.

    With freeze=True (the sportsbook path) the same protection covers *every*
    line that already has a number, not just the hand-set ones. Book lines move
    all week; the pool's number is whichever one was showing when the week was
    first pulled, so later runs only fill in games that were still blank. Pass
    --force to deliberately re-price a week.
    """
    path = os.path.join(LINES_DIR, f"week-{week}.json")
    if not os.path.exists(path):
        return games
    try:
        with open(path) as f:
            old = {str(g.get("espnId")): g for g in json.load(f).get("games", [])}
    except Exception:
        return games
    for g in games:
        prev = old.get(str(g.get("espnId")))
        if not prev or prev.get("spread") is None:
            continue
        if prev.get("status") == "manual":
            g["spread"] = prev["spread"]
            g["status"] = "manual"
            g["note"] = "set by hand; kept over the scraped value"
        elif freeze:
            g["spread"] = prev["spread"]
            g["status"] = prev.get("status", g["status"])
            g["note"] = prev.get("note", g["note"])
    return games


def write_week(season: int, week: int, games: list[dict], source: str,
               warnings: list[str]) -> str:
    os.makedirs(LINES_DIR, exist_ok=True)
    payload = {
        "season": season,
        "week": week,
        "source": source,
        "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "warnings": warnings,
        "games": games,
    }
    path = os.path.join(LINES_DIR, f"week-{week}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    rebuild_index(season)
    return path


def rebuild_index(season: int) -> None:
    weeks = []
    for name in os.listdir(LINES_DIR):
        m = re.fullmatch(r"week-(\d+)\.json", name)
        if not m:
            continue
        with open(os.path.join(LINES_DIR, name)) as f:
            d = json.load(f)
        weeks.append({
            "week": int(m.group(1)),
            "season": d.get("season", season),
            "games": len(d.get("games", [])),
            "warnings": len(d.get("warnings", [])),
        })
    weeks.sort(key=lambda w: w["week"])
    with open(os.path.join(LINES_DIR, "index.json"), "w") as f:
        json.dump({"season": season, "weeks": weeks,
                   "updatedAt": dt.datetime.now(dt.timezone.utc)
                   .isoformat(timespec="seconds")}, f, indent=2)
        f.write("\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", type=int, help="NFL week (default: newest published)")
    ap.add_argument("--season", type=int, default=nfl_season_year())
    ap.add_argument("--pdf", help="use a local PDF instead of downloading")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero if any game is not confidently parsed")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    ap.add_argument("--force", action="store_true",
                    help="re-price the week at today's numbers instead of "
                         "keeping the ones already on file (hand-set lines "
                         "are still kept)")
    ap.add_argument("--overwrite-manual", action="store_true",
                    help="also replace lines that were set by hand in admin.html")
    ap.add_argument("--schedule-only", action="store_true",
                    help="build the week from ESPN's schedule with blank "
                         "spreads, to be filled in with admin.html")
    ap.add_argument("--circa", action="store_true",
                    help="use the Circa contest PDF (OCR) instead of the "
                         "sportsbook feed; needs pdftoppm and tesseract")
    ap.add_argument("--espn-odds", action="store_true",
                    help=argparse.SUPPRESS)   # now the default; kept for old callers
    args = ap.parse_args()

    if not args.circa and not args.pdf:
        if args.week is None:
            args.week = current_nfl_week(args.season)
            if args.week is None:
                return int(bool(sys.stderr.write(
                    "error: could not work out the current week; pass --week\n")))
            print(f"current week per ESPN: {args.week}", file=sys.stderr)

        schedule = espn_schedule(args.season, args.week)
        if not schedule:
            return int(bool(sys.stderr.write(
                f"error: ESPN has no schedule for {args.season} "
                f"week {args.week}\n")))

        providers, games, warn = set(), [], []
        for g in schedule:
            book = g.pop("bookSpread", None)
            provider = g.pop("bookProvider", None)
            g.pop("shortName", None)
            if not args.schedule_only and book is not None:
                if provider:
                    providers.add(provider)
                games.append({**g, "spread": book, "status": "book",
                              "note": f"{provider} line via ESPN" if provider
                                      else "sportsbook line via ESPN"})
            else:
                games.append({**g, "spread": None, "status": "missing",
                              "note": "no line posted yet"})
                warn.append(f"{g['away']} @ {g['home']}: no spread")

        if not args.overwrite_manual:
            # --force re-prices the book lines but still defers to anything a
            # human set in admin.html; --overwrite-manual clears that too.
            games = keep_manual(args.week, games,
                                freeze=not (args.force or args.schedule_only))
            warn = [w for w in warn
                    if not any(w.startswith(f"{g['away']} @ {g['home']}:")
                               and g["spread"] is not None for g in games)]

        book_name = " / ".join(sorted(providers)) or "sportsbook"
        source = ("ESPN schedule (no spreads yet)" if args.schedule_only
                  else f"{book_name} via ESPN")

        # ESPN drops the odds block once a game is final, so re-running a week
        # that is already played finds nothing and would otherwise stamp the
        # file with a source it did not come from -- overwriting, say, a Circa
        # sheet's provenance with "sportsbook via ESPN". If this run learned no
        # new line, keep what the file already said about itself.
        if not args.schedule_only and not providers:
            prev = previous_week_meta(args.week)
            if prev.get("source"):
                source = prev["source"]
                warn = prev.get("warnings", warn)

        if args.dry_run:
            for g in games:
                sp = "  --  " if g["spread"] is None else f"{g['spread']:+g}"
                print(f"{'!!' if g['spread'] is None else '  '} "
                      f"{g['away']:>4} @ {g['home']:<4} {sp:>6}"
                      f"{('   <- ' + g['note']) if g['note'] else ''}")
            return 0

        path = write_week(args.season, args.week, games, source, warn)
        priced = sum(1 for g in games if g["spread"] is not None)
        kept = sum(1 for g in games if g["status"] == "manual")
        print(f"wrote {path} ({len(games)} games, {priced} with a spread"
              + (f", {kept} hand-set" if kept else "") + f") from {source}")
        for w in warn:
            print(f"  warning: {w}", file=sys.stderr)
        return 1 if (args.strict and warn) else 0

    if args.pdf:
        if args.week is None:
            m = re.search(r"Week-(\d+)", args.pdf, re.I)
            if not m:
                return int(bool(sys.stderr.write(
                    "error: pass --week when using --pdf\n")))
            args.week = int(m.group(1))
        source, pdf_bytes = args.pdf, open(args.pdf, "rb").read()
    else:
        try:
            source, week = find_spreads_pdf(args.week, args.season)
        except RuntimeError as err:
            # Not an error worth failing the job over: before Circa posts on
            # Thursday there is simply nothing to fetch. Leave the existing
            # week files alone and say so.
            print(f"nothing to do: {err}", file=sys.stderr)
            return 0
        args.week = week
        print(f"found: {source}", file=sys.stderr)
        pdf_bytes = get_bytes(source)

    texts = ocr_pdf(pdf_bytes)
    rows = vote_rows([parse_rows(t) for t in texts])
    print(f"OCR: {len(texts)} passes, {len(rows)} teams read", file=sys.stderr)

    schedule = espn_schedule(args.season, args.week)
    if not schedule:
        return int(bool(sys.stderr.write(
            f"error: ESPN has no schedule for {args.season} week {args.week}\n")))
    print(f"ESPN lists {len(schedule)} games for {args.season} week {args.week}",
          file=sys.stderr)

    games, warnings = reconcile(rows, schedule, args.strict)

    if args.dry_run:
        mark = {"verified": "  ", "unverified": " ?", "conflict": "!!", "missing": "!!"}
        for g in games:
            sp = "  --  " if g["spread"] is None else f"{g['spread']:+g}"
            print(f"{mark[g['status']]} {g['away']:>4} @ {g['home']:<4} {sp:>6}"
                  f"{('   <- ' + g['note']) if g['note'] else ''}")
        return 0

    if not (args.force or args.overwrite_manual):
        games = keep_manual(args.week, games)
        warnings = [w for w in warnings
                    if not any(g["status"] == "manual" and
                               w.startswith(f"{g['away']} @ {g['home']}:")
                               for g in games)]
    path = write_week(args.season, args.week, games, source, warnings)
    kept = sum(1 for g in games if g["status"] == "manual")
    print(f"wrote {path} ({len(games)} games, {len(warnings)} warnings"
          + (f", {kept} hand-set lines kept" if kept else "") + ")")
    for w in warnings:
        print(f"  warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
