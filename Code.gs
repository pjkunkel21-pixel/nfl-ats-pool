/**
 * NFL ATS Picks Pool -- Google Apps Script backend.
 *
 * This is the only moving part that isn't a static file. It does three things:
 *   1. Stores submitted picks in a Google Sheet.
 *   2. Enforces the deadline server-side, so a pick can't be slipped in after
 *      kickoff by editing the page.
 *   3. Withholds everyone else's picks until the weekly lock, so "hidden until
 *      the deadline" is a real lock rather than a CSS trick.
 *
 * Kickoff times come from ESPN inside this script, not from the browser, so a
 * doctored request can't move the deadline.
 *
 * SETUP: see README.md. In short -- new Google Sheet, Extensions > Apps Script,
 * paste this in, Deploy > New deployment > Web app, execute as Me, access
 * "Anyone", then paste the /exec URL into data/config.json.
 */

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/** Picks per entrant per week. Circa Million uses 5. */
var PICKS_PER_WEEK = 5;

/** Weekly deadline, local to Las Vegas: Saturday at 4:00 PM PT. */
var LOCK_DAY = 6;        // 0 = Sunday ... 6 = Saturday
var LOCK_HOUR = 16;      // 24-hour clock
var LOCK_TZ = 'America/Los_Angeles';

var SHEET_NAME = 'Picks';
var HEADERS = ['submittedAt', 'season', 'week', 'entrant', 'espnId',
               'side', 'team', 'spread'];

// ---------------------------------------------------------------------------
// HTTP entry points
// ---------------------------------------------------------------------------

function doGet(e) {
  try {
    var p = (e && e.parameter) || {};
    var action = p.action || 'picks';
    if (action === 'ping') return json_({ ok: true, version: 3 });
    if (action === 'picks') {
      return json_(readWeek_(int_(p.season), int_(p.week), p.who || ''));
    }
    if (action === 'season') return json_(readSeason_(int_(p.season)));
    return json_({ ok: false, error: 'unknown action: ' + action });
  } catch (err) {
    return json_({ ok: false, error: String(err && err.message || err) });
  }
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(20000);
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (body.action === 'submit') return json_(submit_(body));
    return json_({ ok: false, error: 'unknown action: ' + body.action });
  } catch (err) {
    return json_({ ok: false, error: String(err && err.message || err) });
  } finally {
    try { lock.releaseLock(); } catch (ignored) {}
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
      .setMimeType(ContentService.MimeType.JSON);
}

function int_(v) { var n = parseInt(v, 10); return isNaN(n) ? 0 : n; }

// ---------------------------------------------------------------------------
// Sheet access
// ---------------------------------------------------------------------------

function sheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    sh.appendRow(HEADERS);
    sh.setFrozenRows(1);
  }
  if (sh.getLastRow() === 0) sh.appendRow(HEADERS);
  return sh;
}

function allRows_() {
  var sh = sheet_();
  if (sh.getLastRow() < 2) return [];
  var values = sh.getRange(2, 1, sh.getLastRow() - 1, HEADERS.length).getValues();
  var out = [];
  for (var i = 0; i < values.length; i++) {
    var r = values[i];
    if (!r[3]) continue;                       // no entrant -> blank row
    out.push({
      row: i + 2,
      submittedAt: r[0],
      season: Number(r[1]),
      week: Number(r[2]),
      entrant: String(r[3]).trim(),
      espnId: String(r[4]).trim(),
      side: String(r[5]).trim().toUpperCase(),
      team: String(r[6]).trim().toUpperCase(),
      spread: r[7] === '' || r[7] === null ? null : Number(r[7])
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Schedule + deadline (authoritative, fetched here rather than trusted)
// ---------------------------------------------------------------------------

function schedule_(season, week) {
  var key = 'sched-' + season + '-' + week;
  var cache = CacheService.getScriptCache();
  var hit = cache.get(key);
  if (hit) return JSON.parse(hit);

  var url = 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/' +
            'scoreboard?dates=' + season + '&seasontype=2&week=' + week;
  var res = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (res.getResponseCode() !== 200) throw new Error('ESPN schedule unavailable');
  var data = JSON.parse(res.getContentText());

  var games = {};
  (data.events || []).forEach(function (ev) {
    var comp = (ev.competitions || [])[0] || {};
    var home = '', away = '';
    (comp.competitors || []).forEach(function (c) {
      var abbr = (c.team || {}).abbreviation || '';
      if (c.homeAway === 'home') home = abbr; else away = abbr;
    });
    games[String(ev.id)] = {
      home: home, away: away, kickoff: new Date(ev.date).getTime()
    };
  });

  var out = { games: games, lockAt: computeLock_(games) };
  cache.put(key, JSON.stringify(out), 900);   // 15 minutes
  return out;
}

/**
 * The Saturday 4:00 PM PT before the week's Sunday slate. Derived from the
 * real kickoff times so it stays correct through bye weeks, holiday weeks and
 * international kickoffs.
 */
function computeLock_(games) {
  var times = [];
  for (var id in games) times.push(games[id].kickoff);
  if (!times.length) return null;
  times.sort(function (a, b) { return a - b; });

  // The Sunday slate is where most of the week sits; anchor on the first
  // kickoff that falls on a Sunday in Pacific time, else on the last game.
  var anchor = times[times.length - 1];
  for (var i = 0; i < times.length; i++) {
    if (Number(Utilities.formatDate(new Date(times[i]), LOCK_TZ, 'u')) === 7) {
      anchor = times[i];
      break;
    }
  }
  // Walk back to the preceding LOCK_DAY at LOCK_HOUR, Pacific.
  var d = new Date(anchor);
  for (var guard = 0; guard < 10; guard++) {
    var dow = Number(Utilities.formatDate(d, LOCK_TZ, 'u')) % 7;  // Sun = 0
    if (dow === LOCK_DAY) break;
    d = new Date(d.getTime() - 86400000);
  }
  var ymd = Utilities.formatDate(d, LOCK_TZ, 'yyyy-MM-dd');
  var pad = LOCK_HOUR < 10 ? '0' + LOCK_HOUR : String(LOCK_HOUR);
  // Build the instant by asking for the offset in effect on that date.
  var probe = new Date(ymd + 'T12:00:00Z');
  var offset = Utilities.formatDate(probe, LOCK_TZ, 'Z');         // e.g. -0700
  return new Date(ymd + 'T' + pad + ':00:00' + offset.slice(0, 3) + ':' +
                  offset.slice(3)).getTime();
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

function readWeek_(season, week, who) {
  var sched = schedule_(season, week);
  var now = Date.now();
  var locked = sched.lockAt !== null && now >= sched.lockAt;

  var mine = [], others = {}, all = [];
  allRows_().forEach(function (r) {
    if (r.season !== season || r.week !== week) return;
    all.push(r);
    if (who && r.entrant.toLowerCase() === who.toLowerCase()) mine.push(r);
    others[r.entrant] = true;
  });

  var pick = function (r) {
    return { entrant: r.entrant, espnId: r.espnId, side: r.side,
             team: r.team, spread: r.spread };
  };

  return {
    ok: true,
    season: season,
    week: week,
    lockAt: sched.lockAt,
    serverTime: now,
    locked: locked,
    submitted: Object.keys(others).sort(),
    // Before the deadline the server simply does not hand out anyone else's
    // picks -- that is what makes the reveal real rather than cosmetic.
    picks: locked ? all.map(pick) : mine.map(pick)
  };
}

function readSeason_(season) {
  var byWeek = {};
  allRows_().forEach(function (r) {
    if (r.season !== season) return;
    (byWeek[r.week] = byWeek[r.week] || []).push({
      entrant: r.entrant, espnId: r.espnId, side: r.side,
      team: r.team, spread: r.spread
    });
  });
  // Only hand back weeks whose deadline has passed.
  var now = Date.now(), out = {};
  Object.keys(byWeek).forEach(function (wk) {
    var lockAt = null;
    try { lockAt = schedule_(season, Number(wk)).lockAt; } catch (e) {}
    if (lockAt !== null && now >= lockAt) out[wk] = byWeek[wk];
  });
  return { ok: true, season: season, serverTime: now, weeks: out };
}

// ---------------------------------------------------------------------------
// Write
// ---------------------------------------------------------------------------

function submit_(body) {
  var season = int_(body.season), week = int_(body.week);
  var entrant = String(body.entrant || '').trim();
  var picks = body.picks || [];

  if (!entrant) return { ok: false, error: 'Pick your name first.' };
  if (!season || !week) return { ok: false, error: 'Missing season or week.' };
  if (picks.length !== PICKS_PER_WEEK) {
    return { ok: false,
             error: 'Take exactly ' + PICKS_PER_WEEK + ' games (you sent ' +
                    picks.length + ').' };
  }

  var sched = schedule_(season, week);
  var now = Date.now();
  if (sched.lockAt !== null && now >= sched.lockAt) {
    return { ok: false, locked: true,
             error: 'This week is locked -- the deadline has passed.' };
  }

  var seen = {};
  for (var i = 0; i < picks.length; i++) {
    var p = picks[i];
    var id = String(p.espnId || '');
    var game = sched.games[id];
    if (!game) return { ok: false, error: 'Game ' + id + ' is not in week ' + week + '.' };
    if (seen[id]) return { ok: false, error: 'You picked the same game twice.' };
    seen[id] = true;
    if (now >= game.kickoff) {
      return { ok: false,
               error: game.away + ' @ ' + game.home + ' has already kicked off.' };
    }
    var side = String(p.side || '').toUpperCase();
    if (side !== 'HOME' && side !== 'AWAY') {
      return { ok: false, error: 'Bad side on game ' + id + '.' };
    }
    p.side = side;
    p.team = side === 'HOME' ? game.home : game.away;
  }

  // Replace this entrant's week: delete old rows bottom-up, then append.
  var sh = sheet_();
  var rows = allRows_().filter(function (r) {
    return r.season === season && r.week === week &&
           r.entrant.toLowerCase() === entrant.toLowerCase();
  }).sort(function (a, b) { return b.row - a.row; });
  rows.forEach(function (r) { sh.deleteRow(r.row); });

  var stamp = new Date();
  var toAppend = picks.map(function (p) {
    return [stamp, season, week, entrant, String(p.espnId),
            p.side, p.team,
            (p.spread === null || p.spread === undefined) ? '' : Number(p.spread)];
  });
  sh.getRange(sh.getLastRow() + 1, 1, toAppend.length, HEADERS.length)
    .setValues(toAppend);

  return { ok: true, saved: toAppend.length, entrant: entrant,
           lockAt: sched.lockAt, serverTime: Date.now() };
}
