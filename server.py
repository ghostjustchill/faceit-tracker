"""
Локальний сайт зі статистикою FACEIT.

Запуск:
    Windows (PowerShell):  $env:FACEIT_API_KEY="ключ"; python server.py
    Linux/macOS/MSYS2:     FACEIT_API_KEY=ключ python server.py
Потім відкрий http://localhost:8000

Ключ лишається тут, на сервері, і в браузер ніколи не потрапляє.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send_bytes(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_HEAD(self):
        # Render і сервіси моніторингу перевіряють сайт запитом HEAD
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)

        if url.path == "/health":
            self.send_bytes(200, b"ok", "text/plain")
            return

        if url.path == "/":
            self.send_bytes(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
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
