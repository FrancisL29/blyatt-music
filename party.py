"""Fiesta online del karaoke: punto de encuentro (en blyatt.stream) entre el PC anfitrion y PCs de otras redes.

El anfitrion sigue mandando: sus jugadores, cola, ranking y turnos viven en su karaoke.py. Aqui solo:
  - el anfitrion crea la sala (codigo de 6 caracteres + clave propia) y publica su estado cada vez que cambia;
  - cada invitado se une con el codigo y su nombre (recibe su clave y su id de jugador) y lee ese estado;
  - lo que hace un invitado (anadir, quitar, reacciones y, cuando canta, fases/puntos/nota) le llega al
    anfitrion como orden con el id de jugador puesto AQUI: nadie puede hablar en nombre de otro.
Sin cuentas ni datos de la app: /party va sin Cloudflare Access (como /alexa). Un codigo erroneo o caducado no
da nada y los intentos fallidos estan limitados por IP.
"""
import json
import re
import secrets
import threading
import time

ROOMS = {}
_COND = threading.Condition()
_FAILS = {}               # ip -> instantes de intentos fallidos (y de salas creadas)
_MAX_ROOMS = 30
_IDLE = 30 * 60           # sala sin noticias del anfitrion: se borra
_HOST_GONE = 45           # sin sondeo del anfitrion: los invitados ven "sin conexion con el anfitrion"
_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # sin 0/O ni 1/I: se dicta sin confusiones
_CMDS = {"add", "remove", "react", "phase", "live", "result", "next", "leave"}


def _gc(now):
    for c, r in list(ROOMS.items()):
        if now - r["hseen"] > _IDLE:
            ROOMS.pop(c, None)
    for ip in list(_FAILS):
        _FAILS[ip] = [t for t in _FAILS[ip] if now - t < 600]
        if not _FAILS[ip]:
            _FAILS.pop(ip)


def _limited(ip, now, n=20):
    return len([t for t in _FAILS.get(ip, []) if now - t < 600]) >= n


def _mark(ip, now):
    _FAILS.setdefault(ip, []).append(now)


def _cmd(r, c):
    r["cid"] += 1
    r["cmds"].append(dict(c, id=r["cid"]))
    del r["cmds"][:-200]
    _COND.notify_all()


def _wait(pred, secs, touch):
    end = time.time() + secs
    while not pred():
        left = end - time.time()
        if left <= 0:
            break
        _COND.wait(min(left, 5))
        touch()


def api(path, qs, d, ip):
    g = lambda k: str((qs.get(k) or [""])[0] or d.get(k) or "")
    now = time.time()
    with _COND:
        _gc(now)
        if path == "/party/create":   # el anfitrion abre su fiesta
            if _limited(ip, now, 10):
                return {"error": "Demasiados intentos: espera unos minutos"}
            if len(ROOMS) >= _MAX_ROOMS:
                return {"error": "No hay salas libres ahora mismo"}
            _mark(ip, now)
            code = "".join(secrets.choice(_ALPHA) for _ in range(6))
            while code in ROOMS:
                code = "".join(secrets.choice(_ALPHA) for _ in range(6))
            ROOMS[code] = {"code": code, "hkey": secrets.token_hex(16), "state": None, "v": 1, "cmds": [], "cid": 0,
                           "guests": {}, "hseen": now, "host": re.sub(r"\s+", " ", g("name")).strip()[:40]}
            return {"code": code, "hkey": ROOMS[code]["hkey"]}
        code = re.sub(r"[^A-Z0-9]", "", g("code").upper())[:6]
        r = ROOMS.get(code)
        if path == "/party/join":
            if _limited(ip, now):
                return {"error": "Demasiados intentos: espera unos minutos"}
            if not r:
                _mark(ip, now)
                return {"error": "No hay ninguna fiesta con ese código"}
            name = re.sub(r"\s+", " ", g("name")).strip()[:24]
            if not name:
                return {"error": "Escribe tu nombre"}
            if len(r["guests"]) >= 12:
                return {"error": "La fiesta está llena"}
            gkey, pid = secrets.token_hex(16), "r" + secrets.token_hex(4)
            r["guests"][gkey] = {"pid": pid, "name": name, "seen": now}
            _cmd(r, {"type": "join", "pid": pid, "name": name})
            return {"gkey": gkey, "pid": pid, "host": r["host"]}
        if not r:
            return {"error": "gone"}
        if path in ("/party/push", "/party/hpoll", "/party/close"):   # solo el anfitrion
            if not secrets.compare_digest(g("hkey"), r["hkey"]):
                _mark(ip, now)
                return {"error": "key"}
            r["hseen"] = now
            if path == "/party/push":
                if not isinstance(d.get("state"), dict):
                    return {"error": "?"}
                r["state"] = d["state"]
                r["v"] += 1
                _COND.notify_all()
                return {"ok": True}
            if path == "/party/close":
                ROOMS.pop(code, None)
                r["v"] += 1
                _COND.notify_all()
                return {"ok": True}
            try:
                since = int(g("since") or 0)
            except ValueError:
                since = 0

            def touch():
                r["hseen"] = time.time()
            _wait(lambda: r["cid"] > since or ROOMS.get(code) is not r, 20, touch)
            t = time.time()
            return {"cmds": [c for c in r["cmds"] if c["id"] > since],
                    "guests": {x["pid"]: round(t - x["seen"], 1) for x in r["guests"].values()}}
        gst = r["guests"].get(g("gkey"))
        if not gst:
            _mark(ip, now)
            return {"error": "key"}
        gst["seen"] = now
        if path == "/party/cmd":
            c = d.get("cmd")
            if not isinstance(c, dict) or c.get("type") not in _CMDS or len(json.dumps(c)) > 6000:
                return {"error": "?"}
            _cmd(r, dict(c, pid=gst["pid"]))   # el id lo pone el punto de encuentro, no el invitado
            if c["type"] == "leave":
                r["guests"].pop(g("gkey"), None)
            return {"ok": True}
        if path == "/party/state":
            try:
                v = int(g("v") or -1)
            except ValueError:
                v = -1

            def touch():
                gst["seen"] = time.time()
            _wait(lambda: r["v"] != v or ROOMS.get(code) is not r, 20, touch)
            return {"v": r["v"], "state": r["state"], "host_on": time.time() - r["hseen"] < _HOST_GONE,
                    "closed": ROOMS.get(code) is not r}
        return {"error": "?"}
