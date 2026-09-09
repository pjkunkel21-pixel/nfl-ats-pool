#!/usr/bin/env python3
"""
Pull the current week's Circa Sports Million contest point spreads and write
them to data/lines/week-N.json.

Circa publishes the weekly contest spreads as an image-only PDF (posted around
10am PT each Thursday). This script:

  1. Finds the newest "Contest Point Spreads" PDF via the circasports.com
     WordPress media API.
  2. Renders it and OCRs the two-column grid.
  3. Cross-validates every parsed game against ESPN's authoritative NFL
     schedule for that week -- ESPN supplies the matchup, home/away and kickoff
     time; the OCR only has to supply the number.
  4. Writes JSON, flagging anything it is not confident about so it can be
     fixed by hand in admin.html.

Usage:
    python fetch_lines.py                 # newest published week
    python fetch_lines.py --week 5        # a specific week
    python fetch_lines.py --pdf file.pdf --week 5   # a PDF you downloaded
    python fetch_lines.py --season 2026 --week 5
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
            # Weeks 17-18 land in January, i.e. the calendar year after the
            # season year -- keep anything from this season's window.
            if not re.search(rf"/({season}|{season + 1})/", url):
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

def ocr_pdf(pdf_bytes: bytes) -> str:
    need("pdftoppm")
    need("tesseract")
    with tempfile.TemporaryDirectory() as tmp:
        pdf = os.path.join(tmp, "spreads.pdf")
        with open(pdf, "wb") as f:
            f.write(pdf_bytes)
        subprocess.run(
            ["pdftoppm", "-r", "300", "-png", "-gray", pdf, os.path.join(tmp, "pg")],
            check=True, capture_output=True,
        )
        pages = sorted(p for p in os.listdir(tmp) if p.endswith(".png"))
        if not pages:
            raise RuntimeError("could not render the PDF to an image")
        out = []
        for page in pages:
            res = subprocess.run(
                ["tesseract", os.path.join(tmp, page), "-", "--psm", "6"],
                check=True, capture_output=True, text=True,
            )
            out.append(res.stdout)
        return "\n".join(out)


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


ROW = re.compile(
    r"""(?P<when>\d{1,2}[:.]\d{2}\s*[APap]\s?\.?[Mm]\.?|[A-Za-z]{3}\.?\s+\d{1,2})
        [.,]?\s+
        (?P<rot>\d{1,3})\s+
        (?P<team>[A-Z0-9][A-Z0-9'.\- ]{2,20}?)\s+
        (?P<spread>(?:[-+–—~]\s?[^\s]{1,5})|(?:[Pp][KkRr]))
        (?=\s|$)""",
    re.X,
)


def parse_rows(text: str) -> list[dict]:
    """Every (team, spread) cell on the sheet, in reading order per column."""
    rows = []
    for line in text.splitlines():
        # Two side-by-side columns share a line; findall walks left to right.
        for m in ROW.finditer(line):
            abbr = match_team(m.group("team"))
            spread = parse_spread(m.group("spread"))
            if abbr is None:
                continue
            rows.append({
                "team": abbr,
                "spread": spread,
                "rot": int(m.group("rot")),
                "when": m.group("when").strip(),
                "raw_team": m.group("team").strip(),
                "raw_spread": m.group("spread").strip(),
                "column": m.start(),
            })
    return rows


# --------------------------------------------------------------------------
# 4. ESPN schedule + reconciliation
# --------------------------------------------------------------------------

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
        })
    games.sort(key=lambda g: g["kickoff"] or "")
    return games


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
    ap.add_argument("--schedule-only", action="store_true",
                    help="build the week from ESPN's schedule with blank "
                         "spreads, to be filled in with admin.html")
    ap.add_argument("--espn-odds", action="store_true",
                    help="fill the week with ESPN's sportsbook line as a "
                         "placeholder until Circa's sheet is posted")
    args = ap.parse_args()

    if args.schedule_only or args.espn_odds:
        if args.week is None:
            return int(bool(sys.stderr.write(
                "error: --schedule-only / --espn-odds needs --week\n")))
        schedule = espn_schedule(args.season, args.week)
        games, warn = [], []
        for g in schedule:
            book = g.pop("bookSpread", None)
            g.pop("shortName", None)
            if args.espn_odds and book is not None:
                games.append({**g, "spread": book, "status": "book",
                              "note": "sportsbook line via ESPN, not the Circa sheet"})
            else:
                games.append({**g, "spread": None, "status": "missing",
                              "note": "waiting on the Circa sheet"})
                warn.append(f"{g['away']} @ {g['home']}: no spread")
        source = ("ESPN sportsbook lines (placeholder until Circa posts)"
                  if args.espn_odds else "ESPN schedule (no spreads yet)")
        path = write_week(args.season, args.week, games, source, warn)
        print(f"wrote {path} ({len(games)} games, "
              f"{len(games) - len(warn)} with a spread)")
        return 0

    if args.pdf:
        if args.week is None:
            m = re.search(r"Week-(\d+)", args.pdf, re.I)
            if not m:
                return int(bool(sys.stderr.write(
                    "error: pass --week when using --pdf\n")))
            args.week = int(m.group(1))
        source, pdf_bytes = args.pdf, open(args.pdf, "rb").read()
    else:
        source, week = find_spreads_pdf(args.week, args.season)
        args.week = week
        print(f"found: {source}", file=sys.stderr)
        pdf_bytes = get_bytes(source)

    text = ocr_pdf(pdf_bytes)
    rows = parse_rows(text)
    print(f"OCR produced {len(rows)} team rows", file=sys.stderr)

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

    path = write_week(args.season, args.week, games, source, warnings)
    print(f"wrote {path} ({len(games)} games, {len(warnings)} warnings)")
    for w in warnings:
        print(f"  warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
