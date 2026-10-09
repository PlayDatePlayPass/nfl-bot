#!/usr/bin/env python3
"""Fetch NBC Sports NFL player news and write news.json (headlines only, never analysis).

Run by .github/workflows/fetch-news.yml. Python standard library only.
Source: https://www.nbcsports.com/fantasy/football/player-news ("All News" view,
no filter param), first PAGES pages via its own ?p=N pagination.
For each listed player, their /news page is fetched to build a de-duplicated
history. A previous news.json (if given) is reused so unchanged players aren't
re-fetched every run.

Rolling window: each run MERGES the fresh posts into the previous news.json's
items (dedupe key = newsUrl or name + timestamp + headline; the fresh copy wins),
drops anything older than KEEP_DAYS, and sorts newest first (ties broken by key,
so unchanged input gives byte-identical output and the workflow's
"skip publish if unchanged" check still works). history keeps only players that
still have an item in the window, capped at MAX_HISTORY entries each.

Usage: fetch_news.py OUT.json [PREV.json]
Exit code 3 = nothing parsed (blocked / layout change); the workflow then fails
loudly instead of publishing an empty feed.
"""
import html, json, re, sys, time, urllib.request, datetime

SOURCE = "https://www.nbcsports.com/fantasy/football/player-news"
PAGES = 3            # 10 posts per page
DELAY = 1.0          # seconds between requests to NBC
MAX_HISTORY = 25     # per player
KEEP_DAYS = 7        # rolling window for items
MAX_ITEMS = 3000     # safety cap on the merged list
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0 Safari/537.36")

def get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")

def text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()

def inner(chunk, cls, tag=r"\w+"):
    # Content of the first element with exactly this class token (no nesting needed for these fields).
    m = re.search(r'<(%s)\b[^>]*class="(?:[^"]*\s)?%s(?:\s[^"]*)?"[^>]*>(.*?)</\1>' % (tag, re.escape(cls)), chunk, re.S)
    return text(m.group(2)) if m else ""

def attr(chunk, cls, name):
    m = re.search(r'<\w+\b[^>]*class="(?:[^"]*\s)?%s(?:\s[^"]*)?"[^>]*>' % re.escape(cls), chunk)
    if not m: return ""
    a = re.search(r'\b%s="([^"]*)"' % re.escape(name), m.group(0))
    return html.unescape(a.group(1)) if a else ""

def parse_date(s):
    try:
        d = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
        return d if d.year >= 2000 else None
    except Exception:
        return None

def valid_date(s): return parse_date(s) is not None

def item_key(it):
    # Stable identity for the rolling list: player (news URL, else name) + instant + headline.
    d = parse_date(it.get("date"))
    return "|".join((it.get("newsUrl") or it.get("name") or "",
                     d.isoformat() if d else str(it.get("date")), it.get("headline") or ""))

def parse_posts(page):
    out = []
    starts = [m.start() for m in re.finditer(r'<div class="PlayerNewsPost"[\s>]', page)]
    for i, st in enumerate(starts):
        chunk = page[st: starts[i + 1] if i + 1 < len(starts) else len(page)]
        name = " ".join(x for x in (inner(chunk, "PlayerNewsPost-firstName"), inner(chunk, "PlayerNewsPost-lastName")) if x) \
            or inner(chunk, "PlayerNewsPost-name")
        it = {
            "name": name,
            "team": inner(chunk, "PlayerNewsPost-team-abbr"),
            "pos": inner(chunk, "PlayerNewsPost-position"),
            "headline": inner(chunk, "PlayerNewsPost-headline"),   # headline only; -analysis is never read
            "type": inner(chunk, "PlayerNewsPost-type"),
            "date": attr(chunk, "PlayerNewsPost-date", "data-date"),
            "newsUrl": attr(chunk, "PlayerNewsPost-moreNewsLink", "href"),
        }
        if it["headline"] and valid_date(it["date"]):
            out.append(it)
    return out

def key(h): return (h["headline"], h["date"])

def main():
    out_path = sys.argv[1]
    prev = {}
    if len(sys.argv) > 2:
        try:
            with open(sys.argv[2]) as f: prev = json.load(f)
        except Exception as e:
            print("no usable previous feed:", e)
    prev_hist = prev.get("history", {}) if isinstance(prev, dict) else {}

    items, seen, errors = [], set(), []
    for p in range(1, PAGES + 1):
        url = SOURCE if p == 1 else f"{SOURCE}?p={p}"
        try:
            posts = parse_posts(get(url))
            print(f"page {p}: {len(posts)} posts")
        except Exception as e:
            errors.append(f"{url}: {e}"); print("ERROR", url, e); posts = []
            if p == 1: break
        for it in posts:
            k = (it["name"],) + key(it)
            if k not in seen:
                seen.add(k); items.append(it)
        time.sleep(DELAY)

    if not items:
        print("No posts parsed; NBC may be blocking this runner or changed its markup.", errors)
        sys.exit(3)
    fresh = items

    # Merge into the previous rolling list, then prune to the last KEEP_DAYS.
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=KEEP_DAYS)
    prev_items = prev.get("items", []) if isinstance(prev, dict) else []
    merged = {}
    for it in (prev_items if isinstance(prev_items, list) else []):
        if isinstance(it, dict) and it.get("headline") and valid_date(it.get("date")):
            merged[item_key(it)] = {k: it.get(k, "") for k in ("name", "team", "pos", "headline", "type", "date", "newsUrl")}
    carried = len(merged)
    for it in fresh:
        merged[item_key(it)] = it                       # fresh copy wins
    items = [it for it in merged.values() if parse_date(it["date"]) >= cutoff]
    items.sort(key=lambda x: (parse_date(x["date"]), item_key(x)), reverse=True)
    items = items[:MAX_ITEMS]
    print(f"merge: {len(fresh)} fresh + {carried} previous -> {len(items)} within {KEEP_DAYS} days")

    history, fetched, reused = {}, 0, 0
    for url in dict.fromkeys(i["newsUrl"] for i in items if i["newsUrl"]):
        latest = {key(i) for i in fresh if i["newsUrl"] == url}
        old = prev_hist.get(url)
        if old and latest <= {key(h) for h in old}:
            history[url] = old; reused += 1; continue   # nothing new for this player
        try:
            hist = [{"headline": h["headline"], "type": h["type"], "date": h["date"]} for h in parse_posts(get(url))]
            fetched += 1
        except Exception as e:
            print("history ERROR", url, e); errors.append(f"{url}: {e}")
            hist = old or []
        hist += [{"headline": i["headline"], "type": i["type"], "date": i["date"]}
                 for i in fresh if i["newsUrl"] == url]           # make sure the listed item is included
        dedup = {}
        for h in hist: dedup.setdefault(key(h), h)
        history[url] = sorted(dedup.values(), key=lambda h: h["date"], reverse=True)[:MAX_HISTORY]
        time.sleep(DELAY)

    feed = {
        "source": SOURCE,
        "fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "items": items,
        "history": history,
    }
    if errors: feed["errors"] = errors[:20]
    with open(out_path, "w") as f:
        json.dump(feed, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(items)} items ({len(fresh)} fresh), {len(history)} players (fetched {fetched}, reused {reused}), {len(errors)} errors")

if __name__ == "__main__":
    main()
