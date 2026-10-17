#!/usr/bin/env python3
"""Fetch Dan's Yahoo Fantasy NFL rosters and write rosters.json.

Run by .github/workflows/fetch-rosters.yml. Python standard library only.
Env: YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REFRESH_TOKEN (GitHub secrets).
Optional: YAHOO_REDIRECT_URI (default: the GitHub Pages URL registered with the app).

Flow: refresh the OAuth2 access token (api.login.yahoo.com/oauth2/get_token), then
  GET /fantasy/v2/users;use_login=1/games;game_keys=nfl/teams      (Dan's teams, current season)
  GET /fantasy/v2/team/<team_key>/roster                            (each roster)
XML responses are parsed (Yahoo's JSON shape is awkward).

Output (public repo, so minimal): {fetchedAt, season, teams:[team names],
players:[{name, team (NFL abbr, NBC style), pos, labels:[team names]}]}.
No league names, no Yahoo ids/keys. Team names are included (Dan approved).

SECRETS: tokens are never printed. The access token and any new refresh token are
registered with ::add-mask:: before anything else happens. Error output is limited to
HTTP status codes, Yahoo's short error description, and non-secret fields (scope).
If Yahoo returns a different refresh token, that is reported (yes/no only); the secret
is NOT rewritten (the workflow's GITHUB_TOKEN can't write secrets). Yahoo refresh
tokens are long-lived and normally stay the same.

Usage: fetch_rosters.py OUT.json
Exit 2 = auth failure (token exchange / 401 / 403), 3 = no teams found.
"""
import base64, datetime, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2"
REDIRECT = os.environ.get("YAHOO_REDIRECT_URI") or "https://playdateplaypass.github.io/nfl-bot/"
# Yahoo editorial_team_abbr -> NBC code used by news.json
ALIAS = {"LAR": "LA", "JAX": "JAC", "WSH": "WAS", "LVR": "LV", "OAK": "LV", "STL": "LA", "SD": "LAC"}

SECRETS = []
def mask(v):
    if v:
        SECRETS.append(v)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::add-mask::" + v, flush=True)

def scrub(s):
    s = str(s)
    for v in SECRETS:
        s = s.replace(v, "***")
    s = re.sub(r"(?i)(bearer\s+(?!expires)|access_token\W+|refresh_token\W+)[A-Za-z0-9._~+/=-]{8,}", r"\1***", s)
    return s[:300]

def log(*a):
    print(scrub(" ".join(str(x) for x in a)), flush=True)

def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

def refresh():
    cid, sec, rt = (os.environ.get(k, "").strip() for k in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REFRESH_TOKEN"))
    for v in (cid, sec, rt): mask(v)
    missing = [k for k, v in (("YAHOO_CLIENT_ID", cid), ("YAHOO_CLIENT_SECRET", sec), ("YAHOO_REFRESH_TOKEN", rt)) if not v]
    if missing:
        log("missing env:", ",".join(missing)); sys.exit(2)
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    mask(basic)
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": rt, "redirect_uri": REDIRECT}).encode()
    st, txt, _ = http(TOKEN_URL, body, {"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try: d = json.loads(txt)
    except Exception: d = {}
    at, nrt = d.get("access_token", ""), d.get("refresh_token", "")
    mask(at); mask(nrt)
    if st != 200 or not at:
        log(f"token refresh failed: HTTP {st} error={d.get('error')} description={d.get('error_description')}")
        sys.exit(2)
    # Non-secret diagnostics only.
    log(f"token refresh: HTTP 200 token_type={d.get('token_type')} expires_in={d.get('expires_in')} "
        f"scope={d.get('scope', '(not returned)')} guid_present={'xoauth_yahoo_guid' in d} "
        f"new_refresh_token_differs={'yes' if nrt and nrt != rt else 'no'}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f: f.write(f"refresh_rotated={'true' if nrt and nrt != rt else 'false'}\n")
    return at

def strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag: el.tag = el.tag.split("}", 1)[1]
    return root

def yget(at, path, quiet=False):
    url = API + path
    for attempt in range(3):
        st, txt, h = http(url, headers={"Authorization": "Bearer " + at, "Accept": "application/xml"})
        if st in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(3 * (attempt + 1)); continue
        break
    if st != 200 and not quiet:
        desc = re.search(r"<description>(.*?)</description>", txt, re.S)
        www = h.get("WWW-Authenticate") or h.get("Www-Authenticate") or ""
        log(f"GET {path} -> HTTP {st} desc={desc.group(1).strip() if desc else txt[:160]!r} www-auth={www[:160]!r}")
    return st, txt

def debug_variants(at):
    """Called on 401/403: probe a few endpoint variants, statuses only."""
    parts = at.split(".")
    if len(parts) == 3:  # JWT-shaped token: print claim NAMES and scope only, never values of ids
        try:
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            log(f"  access token is JWT; claim names={sorted(claims)} scope={claims.get('scope') or claims.get('scp')}")
        except Exception:
            log("  access token is JWT-shaped but undecodable")
    else:
        log(f"  access token is opaque ({len(parts)} part(s))")
    st, _, _ = http("https://api.login.yahoo.com/openid/v1/userinfo", headers={"Authorization": "Bearer " + at})
    log(f"  probe openid userinfo -> HTTP {st}")
    for p in ("/game/nfl", "/users;use_login=1", "/users;use_login=1/games",
              "/users;use_login=1/games;game_keys=nfl/teams?format=json",
              "/users;use_login=1/games;game_codes=nfl;seasons=2026/teams"):
        st, _ = yget(at, p, quiet=True)
        log(f"  probe {p} -> HTTP {st}")

def t(el, path):
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "rosters.json"
    at = refresh()
    st, txt = yget(at, "/users;use_login=1/games;game_keys=nfl/teams")
    if st in (401, 403):
        log("access denied; waiting 20s and retrying once (app propagation)")
        time.sleep(20)
        st, txt = yget(at, "/users;use_login=1/games;game_keys=nfl/teams")
    if st != 200:
        debug_variants(at)
        sys.exit(2 if st in (401, 403) else 1)
    root = strip_ns(ET.fromstring(txt))
    games = root.findall(".//game")
    season = ""
    teams = []
    for g in games:
        season = t(g, "season") or season
        for tm in g.findall(".//teams/team"):
            teams.append((t(tm, "team_key"), t(tm, "name")))
    log(f"season={season} teams_found={len(teams)}")
    if not teams:
        sys.exit(3)
    names = []
    players = {}
    for key, name in teams:
        label = name or f"Team {len(names) + 1}"
        while label in names: label += " ·"
        names.append(label)
        st, txt = yget(at, f"/team/{key}/roster")
        if st != 200:
            log(f"roster fetch failed for team #{len(names)}: HTTP {st}"); sys.exit(1)
        r = strip_ns(ET.fromstring(txt))
        n = 0
        for p in r.findall(".//players/player"):
            full = t(p, "name/full")
            if not full: continue
            abbr = t(p, "editorial_team_abbr").upper()
            abbr = ALIAS.get(abbr, abbr)
            pos = t(p, "display_position") or t(p, "primary_position")
            k = (full, abbr)
            e = players.setdefault(k, {"name": full, "team": abbr, "pos": pos, "labels": []})
            if label not in e["labels"]: e["labels"].append(label)
            n += 1
        log(f"team #{len(names)}: {n} players")
        time.sleep(0.5)
    plist = sorted(players.values(), key=lambda e: (e["name"].lower(), e["team"]))
    doc = {"fetchedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "season": season, "teams": names, "players": plist}
    with open(out, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=0, sort_keys=False)
    log(f"wrote {out}: {len(plist)} unique players")

if __name__ == "__main__":
    main()
