"""
Локальний сайт зі статистикою FACEIT.

Запуск:
    Windows (PowerShell):  $env:FACEIT_API_KEY="ключ"; python server.py
    Linux/macOS/MSYS2:     FACEIT_API_KEY=ключ python server.py
Потім відкрий http://localhost:8000

Ключ лишається тут, на сервері, і в браузер ніколи не потрапляє.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API = "https://open.faceit.com/data/v4"
GAME = "cs2"
KEY = os.environ.get("FACEIT_API_KEY", "")
HERE = Path(__file__).parent
CACHE_SECONDS = 300
MAX_PAGES = 20
cache = {}

# --- Захист і статистика відвідувань ---
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MIN", "60"))          # запитів до /api/ за хвилину з однієї адреси
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")                           # якщо задано, відкриває /metrics?key=...
GC_CODE = re.sub(r"[^a-z0-9-]", "", os.environ.get("GOATCOUNTER_CODE", "").lower())
SALT = os.urandom(8).hex()
hits, lock = {}, threading.Lock()
metrics = {"page_views": 0, "api_calls": 0, "rate_limited": 0, "since": time.time(), "visitors": {}}


def client_ip(handler):
    fwd = handler.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else handler.client_address[0]) or "?"


def allowed(ip, cost=1):
    """Ковзне вікно 60 с на адресу. Запити, що тягнуть багато даних, коштують дорожче."""
    now = time.time()
    with lock:
        q = hits.setdefault(ip, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) + cost > RATE_LIMIT:
            return False
        q.extend([now] * cost)
        if len(hits) > 5000:
            for k in [k for k, v in hits.items() if not v or now - v[-1] > 60][:1000]:
                hits.pop(k, None)
        return True


def note_visitor(ip):
    day = time.strftime("%Y-%m-%d")
    h = hashlib.sha256((ip + day + SALT).encode()).hexdigest()[:16]      # сира адреса не зберігається
    with lock:
        metrics["visitors"].setdefault(day, set()).add(h)
        for old in sorted(metrics["visitors"])[:-30]:
            metrics["visitors"].pop(old, None)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def api_get(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + KEY, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        messages = {
            401: "API key rejected",
            403: "API key rejected",
            404: "Not found",
            429: "Too many requests, try again later",
        }
        raise ApiError(e.code, messages.get(e.code, "FACEIT returned HTTP %d" % e.code))
    except urllib.error.URLError:
        raise ApiError(502, "Could not reach FACEIT")


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def nickname_from_link(link):
    m = re.search(r"/players/([^/?#]+)", link)
    if m:
        return urllib.parse.unquote(m.group(1))
    if re.fullmatch(r"[\w.\-]{2,32}", link):      # просто нікнейм
        return link
    return None


def get_rank(pid, region, country):
    """Позиція гравця в регіональному та національному рейтингу (офіційний API)."""
    out = {"regionName": region, "countryCode": country}
    if not region:
        return out
    for key, params in (("region", {"limit": 1}), ("country", {"limit": 1, "country": country})):
        if key == "country" and not country:
            continue
        try:
            r = api_get("/rankings/games/%s/regions/%s/players/%s" % (GAME, region, pid), params)
            out[key] = to_int(r.get("position")) or None
        except ApiError:
            pass
    return out


def fetch_player(nickname):
    player = api_get("/players", {"nickname": nickname})
    cs2 = (player.get("games") or {}).get(GAME) or {}
    matches, cursor = [], 0

    # offset у API обмежений 200, тому гортаємо за часом (параметр "to")
    for _ in range(MAX_PAGES):
        params = {"limit": 100, "offset": 0}
        if cursor:
            params["to"] = cursor
        data = api_get("/players/%s/games/%s/stats" % (player["player_id"], GAME), params)
        items = data.get("items") or []
        oldest = 0
        for it in items:
            s = it.get("stats") or {}
            finished = to_int(s.get("Match Finished At"))
            matches.append({
                "map": s.get("Map", ""),
                "win": to_int(s.get("Result")) == 1,
                "kills": to_int(s.get("Kills")),
                "deaths": to_int(s.get("Deaths")),
                "assists": to_int(s.get("Assists")),
                "adr": to_float(s.get("ADR")),
                "hs": to_float(s.get("Headshots %")),
                "finished": finished,
                "id": s.get("Match Id") or it.get("match_id") or "",
                "dk": to_int(s.get("Double Kills")),
                "tk": to_int(s.get("Triple Kills")),
                "qk": to_int(s.get("Quadro Kills")),
                "pk": to_int(s.get("Penta Kills")),
                "mvp": to_int(s.get("MVPs")),
            })
            if finished and (not oldest or finished < oldest):
                oldest = finished
        if len(items) < 100 or not oldest:
            break
        cursor = oldest - 1

    return {
        "player_id": player["player_id"],
        "rank": get_rank(player["player_id"], cs2.get("region"), player.get("country")),
        "nickname": player.get("nickname", nickname),
        "avatar": player.get("avatar", ""),
        "elo": cs2.get("faceit_elo"),
        "level": cs2.get("skill_level"),
        "matches": matches,
    }


veto_cache = {}


def veto_for(match_id, pid):
    """Бани/піки мап в одному матчі з точки зору гравця.
    Склад команд беремо з офіційного API, історію вето з неофіційної адреси democracy."""
    key = (match_id, pid)
    if key in veto_cache:
        return veto_cache[key]
    try:
        details = api_get("/matches/" + urllib.parse.quote(match_id))
        side, leader = None, False
        for f in ("faction1", "faction2"):
            team = (details.get("teams") or {}).get(f) or {}
            if any(p.get("player_id") == pid for p in team.get("roster") or []):
                side, leader = f, team.get("leader") == pid
        if not side:
            raise ApiError(404, "player not found in match roster")

        req = urllib.request.Request(
            "https://api.faceit.com/democracy/v1/match/%s/history" % urllib.parse.quote(match_id),
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            hist = json.load(r)

        out = {"id": match_id, "leader": leader, "myDrop": [], "oppDrop": [], "myPick": [], "oppPick": []}
        for t in (hist.get("payload") or {}).get("tickets") or []:
            if t.get("entity_type") != "map":
                continue
            for e in t.get("entities") or []:
                if e.get("random"):                    # автовибір системою, не рішення команди
                    continue
                kind = {"drop": "Drop", "pick": "Pick"}.get(e.get("status"))
                if kind and e.get("guid"):
                    out[("my" if e.get("selected_by") == side else "opp") + kind].append(e["guid"])
        if len(veto_cache) < 5000:
            veto_cache[key] = out
        return out
    except ApiError as e:
        return {"id": match_id, "error": str(e)}
    except (urllib.error.URLError, ValueError, OSError) as e:
        return {"id": match_id, "error": "veto data unavailable (%s)" % type(e).__name__}


elo_cache = {}


def elo_page(pid, size, page):
    """Одна сторінка історії. size=None означає запит без параметрів (як у браузері)."""
    qs = "" if size is None else "?size=%d&page=%d" % (size, page)
    req = urllib.request.Request(
        "https://api.faceit.com/stats/v1/stats/time/users/%s/games/%s%s" % (pid, GAME, qs),
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    return data if isinstance(data, list) else []


def fetch_elo(pid):
    """ELO після кожного матчу та його зміна. Неофіційна адреса FACEIT (без ключа), може змінитись."""
    hit = elo_cache.get(pid)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    # Не знаємо, які size/page приймає адреса, тому пробуємо кілька варіантів
    first, size, last_err = None, None, ""
    for v in (100, 50, 20, None):
        try:
            first, size = elo_page(pid, v, 0), v
            break
        except urllib.error.HTTPError as e:
            last_err = "HTTP %d" % e.code
        except (urllib.error.URLError, ValueError, OSError) as e:
            last_err = type(e).__name__
    if first is None:
        raise ApiError(502, "ELO history unavailable (%s)" % last_err)

    seen, out, pages, page = set(), [], [first], 1
    while size and pages[-1] and len(pages[-1]) >= size and page < MAX_PAGES:
        try:
            nxt = elo_page(pid, size, page)
        except (urllib.error.URLError, ValueError, OSError):
            break
        if not nxt or nxt[0].get("matchId") == pages[-1][0].get("matchId"):   # сторінки закінчились або повторюються
            break
        pages.append(nxt)
        page += 1

    for items in pages:
        for it in items:
            mid = it.get("matchId")
            if not mid or mid in seen or it.get("elo") in (None, ""):
                continue
            seen.add(mid)
            out.append({"id": mid, "date": to_int(it.get("date")), "elo": to_int(it.get("elo")),
                        "delta": to_int(it.get("elo_delta")), "map": it.get("i1", "")})
    out.sort(key=lambda x: x["date"], reverse=True)
    elo_cache[pid] = (time.time(), out)
    return out


match_cache = {}


def fetch_match(mid):
    """Табло матчу: команди, рахунок і статистика гравців (офіційний API)."""
    hit = match_cache.get(mid)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    data = api_get("/matches/%s/stats" % urllib.parse.quote(mid))
    rounds = data.get("rounds") or []
    if not rounds:
        raise ApiError(404, "No stats for this match")
    r = rounds[0]
    rs = r.get("round_stats") or {}
    teams = []
    for t in r.get("teams") or []:
        ts = t.get("team_stats") or {}
        players = []
        for p in t.get("players") or []:
            ps = p.get("player_stats") or {}
            players.append({"id": p.get("player_id"), "nick": p.get("nickname"), "k": to_int(ps.get("Kills")),
                            "d": to_int(ps.get("Deaths")), "a": to_int(ps.get("Assists")), "adr": to_float(ps.get("ADR")),
                            "hs": to_float(ps.get("Headshots %")), "mvp": to_int(ps.get("MVPs"))})
        players.sort(key=lambda x: x["k"], reverse=True)
        teams.append({"name": ts.get("Team") or t.get("team_id"), "score": to_int(ts.get("Final Score")),
                      "win": to_int(ts.get("Team Win")) == 1, "players": players})
    out = {"map": rs.get("Map", ""), "score": rs.get("Score", ""), "teams": teams}
    if len(match_cache) >= 500:
        match_cache.pop(min(match_cache, key=lambda k: match_cache[k][0]))
    match_cache[mid] = (time.time(), out)
    return out


recent_cache = {}


def fetch_recent(nick):
    """Гравець + його останні 30 матчів (легкий запит для друзів та оверлею)."""
    key = nick.lower()
    hit = recent_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    player = api_get("/players", {"nickname": nick})
    cs2 = (player.get("games") or {}).get(GAME) or {}
    data = api_get("/players/%s/games/%s/stats" % (player["player_id"], GAME), {"limit": 30, "offset": 0})
    ms = []
    for it in data.get("items") or []:
        st = it.get("stats") or {}
        ms.append({"win": to_int(st.get("Result")) == 1, "k": to_int(st.get("Kills")), "d": to_int(st.get("Deaths")),
                   "adr": to_float(st.get("ADR")), "krr": to_float(st.get("K/R Ratio")), "hs": to_int(st.get("Headshots")),
                   "finished": to_int(st.get("Match Finished At"))})
    out = {"nickname": player.get("nickname", nick), "avatar": player.get("avatar", ""), "elo": cs2.get("faceit_elo"),
           "level": cs2.get("skill_level"), "matches": ms, "pid": player["player_id"], "region": cs2.get("region"),
           "country": player.get("country")}
    if len(recent_cache) >= 500:
        recent_cache.pop(min(recent_cache, key=lambda k: recent_cache[k][0]))
    recent_cache[key] = (time.time(), out)
    return out


def kd_of(ms):
    d = sum(m["d"] for m in ms)
    k = sum(m["k"] for m in ms)
    return round(k / d if d else k, 2) if ms else None


def brief(nick):
    r = fetch_recent(nick)
    ms = r["matches"][:20]
    return {"nickname": r["nickname"], "avatar": r["avatar"], "elo": r["elo"], "level": r["level"], "n": len(ms),
            "kd": kd_of(ms), "wr": round(100 * sum(m["win"] for m in ms) / len(ms), 1) if ms else None}


rank_cache = {}


def rank_cached(pid, region, country):
    hit = rank_cache.get(pid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = get_rank(pid, region, country)
    if len(rank_cache) >= 500:
        rank_cache.pop(min(rank_cache, key=lambda k: rank_cache[k][0]))
    rank_cache[pid] = (time.time(), out)
    return out


def window_stats(ms):
    """Показники за вікно матчів: K/D, K/R, ADR, середні вбивства, HS%, відсоток перемог."""
    if not ms:
        return None
    n = len(ms)
    k, d = sum(m["k"] for m in ms), sum(m["d"] for m in ms)
    adrs = [m["adr"] for m in ms if m["adr"] > 0]
    return {"n": n, "kd": round(k / d if d else k, 2), "krr": round(sum(m["krr"] for m in ms) / n, 2),
            "adr": round(sum(adrs) / len(adrs), 1) if adrs else None, "avg": round(k / n, 1),
            "hs": round(100 * sum(m["hs"] for m in ms) / k) if k else None,
            "wr": round(100 * sum(m["win"] for m in ms) / n)}


def overlay(nick, since):
    r = fetch_recent(nick)
    sess = [m for m in r["matches"] if m["finished"] >= since]
    return {"nickname": r["nickname"], "elo": r["elo"], "level": r["level"], "wins": sum(m["win"] for m in sess),
            "losses": sum(not m["win"] for m in sess), "stats": window_stats(r["matches"][:20]),
            "last5": [{"win": m["win"], "k": m["k"], "d": m["d"]} for m in r["matches"][:5]],
            "rank": rank_cached(r["pid"], r["region"], r["country"])}


def matches_batch(ids):
    def one(i):
        try:
            return {"id": i, "data": fetch_match(i)}
        except ApiError as e:
            return {"id": i, "error": str(e)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(one, ids))


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send_bytes(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def api_call(self, fn):
        try:
            self.send_json(200, fn())
        except ApiError as e:
            self.send_json(e.status if e.status in (401, 403, 404, 429) else 502, {"error": "Player not found" if e.status == 404 else str(e)})

    def do_HEAD(self):
        # Render і сервіси моніторингу перевіряють сайт запитом HEAD
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        ip = client_ip(self)

        if url.path.startswith("/api/"):
            cost = 4 if url.path in ("/api/vetoes", "/api/matches") else 1
            if not allowed(ip, cost):
                metrics["rate_limited"] += 1
                self.send_json(429, {"error": "Too many requests, wait a minute"})
                return
            metrics["api_calls"] += 1

        if url.path == "/metrics":
            key = urllib.parse.parse_qs(url.query).get("key", [""])[0]
            if not ADMIN_KEY or not hmac.compare_digest(key, ADMIN_KEY):
                self.send_bytes(404, b"Not found", "text/plain")
                return
            today = time.strftime("%Y-%m-%d")
            self.send_json(200, {"uptime_hours": round((time.time() - metrics["since"]) / 3600, 1),
                                 "page_views": metrics["page_views"], "api_calls": metrics["api_calls"],
                                 "rate_limited": metrics["rate_limited"],
                                 "unique_visitors_today": len(metrics["visitors"].get(today, ())),
                                 "unique_visitors_by_day": {d: len(v) for d, v in sorted(metrics["visitors"].items())},
                                 "note": "Counters reset when the server restarts"})
            return

        if url.path in ("/api/brief", "/api/overlay"):
            q = urllib.parse.parse_qs(url.query)
            nick = (q.get("nick", [""])[0]).strip()
            if not re.fullmatch(r"[\w.\-]{2,32}", nick):
                self.send_json(400, {"error": "Bad request"})
                return
            if url.path == "/api/brief":
                self.api_call(lambda: brief(nick))
            else:
                since = to_int(q.get("since", ["0"])[0])
                self.api_call(lambda: overlay(nick, since))
            return

        if url.path == "/api/matches":
            ids = [i for i in urllib.parse.parse_qs(url.query).get("ids", [""])[0].split(",") if i][:20]
            if not ids or not all(re.fullmatch(r"[0-9A-Za-z-]{10,80}", i) for i in ids):
                self.send_json(400, {"error": "Bad request"})
                return
            self.send_json(200, {"results": matches_batch(ids)})
            return

        if url.path == "/api/match":
            mid = urllib.parse.parse_qs(url.query).get("id", [""])[0]
            if not re.fullmatch(r"[0-9A-Za-z-]{10,80}", mid):
                self.send_json(400, {"error": "Bad request"})
                return
            try:
                self.send_json(200, fetch_match(mid))
            except ApiError as e:
                self.send_json(e.status if e.status in (401, 403, 404, 429) else 502, {"error": str(e)})
            return

        static = {"/robots.txt": ("robots.txt", "text/plain; charset=utf-8"),
                  "/sitemap.xml": ("sitemap.xml", "application/xml; charset=utf-8"),
                  "/og.png": ("og.png", "image/png"),
                  "/overlay": ("overlay.html", "text/html; charset=utf-8")}
        if url.path in static:
            name, ctype = static[url.path]
            try:
                self.send_bytes(200, (HERE / name).read_bytes(), ctype)
            except OSError:
                self.send_bytes(404, b"Not found", "text/plain")
            return

        if url.path == "/health":
            self.send_bytes(200, b"ok", "text/plain")
            return

        if url.path == "/":
            metrics["page_views"] += 1
            note_visitor(ip)
            page = (HERE / "index.html").read_bytes()
            if GC_CODE:                      # необов'язкова безкоштовна аналітика GoatCounter
                tag = '<script data-goatcounter="https://%s.goatcounter.com/count" async src="//gc.zgo.at/count.js"></script></body>' % GC_CODE
                page = page.replace(b"</body>", tag.encode(), 1)
            self.send_bytes(200, page, "text/html; charset=utf-8")
            return

        if url.path == "/api/debug":
            # Тимчасова діагностика: сирі дані останнього матчу (щоб знайти поля на кшталт рейтингу). Потім можна видалити.
            link = urllib.parse.parse_qs(url.query).get("link", [""])[0].strip()
            nickname = nickname_from_link(link)
            if not nickname:
                self.send_json(400, {"error": "Enter a nickname or profile link"})
                return
            try:
                player = api_get("/players", {"nickname": nickname})
                data = api_get("/players/%s/games/%s/stats" % (player["player_id"], GAME), {"limit": 1, "offset": 0})
            except ApiError as e:
                self.send_json(e.status if e.status in (401, 403, 404, 429) else 502, {"error": str(e)})
                return
            items = data.get("items") or [None]
            self.send_json(200, {"player_id": player["player_id"], "latest_match": items[0]})
            return

        if url.path == "/api/elo":
            pid = urllib.parse.parse_qs(url.query).get("pid", [""])[0]
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", pid):
                self.send_json(400, {"error": "Bad request"})
                return
            try:
                self.send_json(200, {"items": fetch_elo(pid)})
            except ApiError as e:
                self.send_json(502, {"error": str(e)})
            return

        if url.path == "/api/vetoes":
            q = urllib.parse.parse_qs(url.query)
            pid = q.get("pid", [""])[0]
            ids = [i for i in q.get("ids", [""])[0].split(",") if i][:25]
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", pid) or not ids or not all(re.fullmatch(r"[0-9A-Za-z-]{10,80}", i) for i in ids):
                self.send_json(400, {"error": "Bad request"})
                return
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda i: veto_for(i, pid), ids))
            self.send_json(200, {"results": results})
            return

        if url.path == "/api/stats":
            link = urllib.parse.parse_qs(url.query).get("link", [""])[0].strip()
            nickname = nickname_from_link(link)
            if not nickname:
                self.send_json(400, {"error": "Enter a nickname or a link like https://www.faceit.com/en/players/Nickname"})
                return

            key = nickname.lower()
            hit = cache.get(key)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                self.send_json(200, hit[1])
                return

            try:
                result = fetch_player(nickname)
            except ApiError as e:
                msg = "Player not found" if e.status == 404 else str(e)
                self.send_json(e.status if e.status in (401, 403, 404, 429) else 502, {"error": msg})
                return

            if len(cache) >= 200:
                cache.pop(min(cache, key=lambda k: cache[k][0]))
            cache[key] = (time.time(), result)
            self.send_json(200, result)
            return

        self.send_bytes(404, b"Not found", "text/plain")


if __name__ == "__main__":
    if not KEY:
        sys.exit("FACEIT_API_KEY is not set (see comment at top of file).")
    # На хостингу (Render тощо) задана змінна PORT і треба слухати 0.0.0.0;
    # локально слухаємо лише 127.0.0.1, щоб сайт був доступний тільки тобі.
    port = int(os.environ.get("PORT", 8000))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), Handler)
    print("Open http://localhost:%d  (Ctrl+C to stop)" % port)
    server.serve_forever()
