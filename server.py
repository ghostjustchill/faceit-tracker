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
            })
            if finished and (not oldest or finished < oldest):
                oldest = finished
        if len(items) < 100 or not oldest:
            break
        cursor = oldest - 1

    return {
        "nickname": player.get("nickname", nickname),
        "avatar": player.get("avatar", ""),
        "elo": cs2.get("faceit_elo"),
        "level": cs2.get("skill_level"),
        "matches": matches,
    }


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
