"""DJ de radio (estilo DJ de Spotify): Lara pone musica a tu gusto por bloques y habla entre canciones.

Reparto del trabajo:
  - el dispositivo manda lo que sabe de quien escucha (me gusta, lo que mas escucha, lo que salto, su hora);
  - aqui se arma cada bloque de canciones (favoritas, descubrimientos de la radio de YT Music, recuerdos...);
  - antes de cada intervencion se juntan datos reales (pagina del artista en YT Music, Wikipedia, reproducciones)
    y se escribe el guion: con Claude si hay clave de la API, si no con plantillas;
  - la voz la genera Edge TTS (en la nube de Microsoft: el telefono servidor apenas trabaja) y ffmpeg recorta
    las pausas largas entre frases a ~200 ms.
Todo va por cuenta (la sesion de cada dispositivo): los gustos y las frases de uno no se mezclan con los de otro.
"""
import asyncio
import concurrent.futures
import hashlib
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

APP = None   # app.py se inyecta aqui (radio, artist, search, viewcounts, cached, DATA, CACHE_DIR...)

VOICE, RATE, PITCH = "es-CO-SalomeNeural", "+12%", "+4Hz"   # voz elegida por el usuario; la DJ se llama Lara
# pausas entre frases: Edge las hace de ~0.85 s; se dejan en ~0.2 s (habla rapida y natural)
_TRIM = ("silenceremove=start_periods=1:start_threshold=-45dB:stop_periods=-1:stop_duration=0.1"
         ":stop_threshold=-45dB:stop_silence=0.1")
MODELS = {"opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}
_MAX_FILES = 400        # frases guardadas en disco (~40 KB c/u)
_AI_DAY = 150           # intervenciones con IA por cuenta y dia; despues, plantillas
_UA = "Blyatt/1.0 (radio DJ; https://blyatt.stream)"
_MEM, _mem_lock = {}, threading.Lock()
_AI_ERR = {"at": 0, "msg": "", "pause": 0}   # ultimo fallo de la API (se ve en Ajustes); sin saldo se pausa un rato


def _dir():
    d = os.path.join(APP.CACHE_DIR, "dj")
    os.makedirs(d, exist_ok=True)
    return d


# ---------- configuracion: clave de la API de Anthropic (solo el dueno del server / del PC) ----------
def _cfg_file():
    return os.path.join(APP.DATA, "dj.json")


def config():
    try:
        with open(_cfg_file(), encoding="utf8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    key = str(d.get("key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    model = d.get("model") if d.get("model") in MODELS else "haiku"
    return {"key": key, "model": model}


def set_config(d):
    try:
        with open(_cfg_file(), encoding="utf8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    if "key" in d:
        k = str(d.get("key") or "").strip()
        if k and not re.fullmatch(r"sk-ant-[\w-]{20,200}", k):
            return {"error": "Esa clave no parece de Anthropic (empieza por sk-ant-)"}
        raw["key"] = k
    if d.get("model") in MODELS:
        raw["model"] = d["model"]
    tmp = _cfg_file() + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(raw, f)
    os.replace(tmp, _cfg_file())
    return status()


def status():
    c = config()
    try:
        import edge_tts   # noqa: F401
        voice = True
    except ImportError:
        voice = False
    err = _AI_ERR["msg"] if c["key"] and time.time() - _AI_ERR["at"] < 3600 else ""
    return {"ai": bool(c["key"]), "model": c["model"], "voice": voice, "server": bool(APP.SERVER_MODE), "ai_error": err}


# ---------- bloques de canciones ----------
def _song(x):
    if not isinstance(x, dict) or not re.fullmatch(r"[\w-]{6,20}", str(x.get("id") or "")):
        return None
    s = {k: x[k] for k in ("id", "title", "artist", "cover", "duration", "explicit") if x.get(k)}
    arts = [{"name": str(a.get("name") or "")[:80], "id": str(a.get("id") or "")[:40]}
            for a in (x.get("artists") or [])[:4] if isinstance(a, dict) and a.get("name")]
    if arts:
        s["artists"] = arts
    s["title"] = str(s.get("title") or "")[:150]
    if not s.get("cover"):
        s["cover"] = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % s["id"]
    s["artist"] = str(s.get("artist") or (arts[0]["name"] if arts else ""))[:150]
    for k in ("n", "days"):
        if isinstance(x.get(k), (int, float)):
            s[k] = int(x[k])
    return s if s["title"] else None


def _list(v, cap):
    return [s for s in (_song(x) for x in (v or [])[:cap]) if s]


def _main_artist(s):
    return ((s.get("artists") or [{}])[0].get("name") or s.get("artist") or "").split(",")[0].strip().lower()


def _key(s):   # misma cancion aunque YT la de con otro id (video oficial, version del album...)
    return _norm(s.get("title")) + "|" + _main_artist(s)


def _radio(seed_id):
    try:
        return [s for s in (_song(x) for x in APP.radio(seed_id)) if s]
    except Exception:
        return []


def plan(ctx):
    """Siguiente bloque (3-5 canciones del mismo rollo) y por que suena cada una."""
    liked, top, recent = _list(ctx.get("liked"), 300), _list(ctx.get("top"), 60), _list(ctx.get("recent"), 40)
    played_l = [x for x in (ctx.get("played") or [])[:600] if isinstance(x, dict)]
    played = {str(x.get("id")) for x in played_l} | {_key(x) for x in played_l if x.get("title")}
    skip_art = {str(a).lower() for a in (ctx.get("skip_artists") or [])[:50]}
    last = str(ctx.get("last_kind") or "")
    cur = _song(ctx.get("cur"))
    known = {s["id"] for s in liked + top + recent} | {_key(s) for s in liked + top + recent}
    rng = random.Random()

    def fresh(lst, n, why, avoid=()):
        out, arts = [], set()
        for s in lst:
            a, k = _main_artist(s), _key(s)
            if {s["id"], k} & (played | set(avoid)) or a in skip_art or a in arts:
                continue   # sin repetir artista dentro del bloque
            arts.add(a)
            out.append(dict(s, why=why(s)))
            if len(out) >= n:
                break
        return out

    def seed_pick(pool):
        pool = [s for s in pool if _main_artist(s) not in skip_art]
        return rng.choice(pool[:25]) if pool else None

    fav_pool = sorted(top, key=lambda s: -s.get("n", 0)) + liked[:40]
    old_liked = liked[len(liked) // 3:] if len(liked) > 12 else []
    if ctx.get("vibe"):
        kinds = ["vibra"]
    elif not last:
        kinds = ["favoritas", "mezcla"]   # se arranca con lo conocido
    else:   # sorteo con pesos sin repetir el bloque anterior: mas mezclas y descubrimientos que recuerdos
        weights = {k: w for k, w in (("mezcla", 3), ("descubrir", 3), ("favoritas", 2), ("recuerdos", 1)) if k != last}
        kinds = sorted(weights, key=lambda k: -rng.random() ** (1 / weights[k]))
    for kind in kinds + ["mezcla", "descubrir"]:
        tracks, seed = [], None
        if kind == "favoritas":
            pool = fav_pool[:]
            rng.shuffle(pool)
            pool.sort(key=lambda s: -min(s.get("n", 0), 6))   # las mas escuchadas primero, con algo de azar
            tracks = fresh(pool, 4, lambda s: {"t": "top", "n": s.get("n", 0), "days": s.get("days", 0)}
                           if s.get("n", 0) >= 2 else {"t": "liked"})
        elif kind == "recuerdos":
            pool = [s for s in old_liked if s["id"] not in {x["id"] for x in top + recent}]
            rng.shuffle(pool)
            tracks = fresh(pool, 4, lambda s: {"t": "old"})
        elif kind in ("descubrir", "mezcla", "vibra"):
            src = (old_liked or liked) if kind == "vibra" else (top or liked or recent)
            seed = seed_pick(src) or cur or (recent[0] if recent else None)
            if not seed:
                continue
            rad = _radio(seed["id"])
            sd = {"title": seed["title"], "artist": seed["artist"]}
            new = [s for s in rad if s["id"] not in known and _key(s) not in known]
            if kind == "descubrir":
                tracks = fresh(new, 4, lambda s: {"t": "new", "seed": sd})
            else:
                fam = [s for s in rad if s["id"] in known or _key(s) in known]
                first = [] if {seed["id"], _key(seed)} & played else [dict(seed, why={"t": "seed"})]
                tracks = first + fresh(fam, 1, lambda s: {"t": "liked"}, {seed["id"], _key(seed)})
                tracks += fresh(new, 4 - len(tracks), lambda s: {"t": "new", "seed": sd},
                                {x["id"] for x in tracks} | {_key(x) for x in tracks})
        if len(tracks) >= 3 or (tracks and kind == "vibra"):
            return {"kind": kind, "seed": {"title": seed["title"], "artist": seed["artist"]} if seed else None,
                    "tracks": tracks}
    # sin gustos guardados (cuenta nueva): la radio de lo que suena
    if cur:
        rad = _radio(cur["id"])
        tracks = fresh([s for s in rad if _key(s) != _key(cur)], 4,
                       lambda s: {"t": "new", "seed": {"title": cur["title"], "artist": cur["artist"]}})
        if tracks:
            return {"kind": "descubrir", "seed": {"title": cur["title"], "artist": cur["artist"]}, "tracks": tracks}
    return {"error": "Escucha o dale me gusta a algunas canciones y vuelve: así sé qué ponerte"}


# ---------- datos reales para hablar ----------
def _get(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf8", "replace"))


def _norm(t):
    t = re.sub(r"\(.*?\)|\[.*?\]|-.*$", "", (t or "").lower())
    return re.sub(r"[^\w]+", " ", t).strip()


def _sentences(text, cap):
    text = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(s) > cap:
            break
        out += (" " if out else "") + s
    return out or text[:cap]


def _wiki(title, artist):
    """Resumen de Wikipedia de la cancion (es y si no en) solo si el articulo es de ESA cancion."""
    nt = _norm(title)
    if len(nt) < 2:
        return ""
    for lang in ("es", "en"):
        try:
            q = urllib.parse.quote("%s %s" % (title, artist))
            hits = _get("https://%s.wikipedia.org/w/api.php?action=query&list=search&srlimit=4&format=json&srsearch=%s"
                        % (lang, q)).get("query", {}).get("search", [])
        except Exception:
            continue
        for h in hits:
            snip = re.sub(r"<[^>]+>", "", h.get("snippet", "")).lower()
            if _norm(h.get("title")).startswith(nt) and (_norm(artist)[:12] in _norm(snip) or re.search(
                    r"canci[oó]n|sencillo|single|song|track", snip)):
                try:
                    ex = _get("https://%s.wikipedia.org/api/rest_v1/page/summary/%s"
                              % (lang, urllib.parse.quote(h["title"].replace(" ", "_")))).get("extract", "")
                except Exception:
                    ex = ""
                if ex:
                    return _sentences(ex, 650)
    return ""


def _artist_info(name, aid):
    try:
        if not aid and name:
            res = APP.search(name, "artists") or []
            hit = next((r for r in res if _norm(r.get("title")) == _norm(name)), None)
            aid = hit and hit.get("browseId")
        if not aid:
            return {}
        a = APP.artist(aid)
        bio = re.split(r"\s*(?:Fuente|Source|From Wikipedia|De Wikipedia)\s*:?\s*Wikipedia|\s*Fuente: ", a.get("description") or "")[0]
        return {"bio": _sentences(bio, 650), "oyentes": a.get("subtitle") or ""}
    except Exception:
        return {}


def _plays(n):
    if not n:
        return ""
    if n >= 10 ** 9:
        return ("%.1f" % (n / 1e9)).rstrip("0").rstrip(".").replace(".", ",") + " mil millones"
    if n >= 10 ** 6:
        return "%d millones" % (n // 10 ** 6)
    if n >= 1000:
        return "%d mil" % (n // 1000)
    return str(n)


def facts(t):
    aid = ((t.get("artists") or [{}])[0].get("id") or "") if t.get("artists") else ""
    name = ((t.get("artists") or [{}])[0].get("name") if t.get("artists") else "") or re.split(r",| y |&", t.get("artist") or "")[0]
    key = "djf:" + t["id"]

    def build():
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            fa = ex.submit(_artist_info, name.strip(), aid)
            fw = ex.submit(_wiki, t.get("title", ""), name)
            fv = ex.submit(lambda: (APP.viewcounts([t["id"]]) or {}).get(t["id"]))
            out = {}
            try:
                out.update(fa.result(timeout=12))
            except Exception:
                pass
            try:
                out["cancion"] = fw.result(timeout=12)
            except Exception:
                pass
            try:
                out["reproducciones"] = _plays(fv.result(timeout=12))
            except Exception:
                pass
        return {k: v for k, v in out.items() if v}
    return APP.cached(key, 86400, build)


# ---------- guion ----------
_SYS = """Eres Lara, la DJ de Blyatt: una radio personal que suena para UNA persona y está hecha con sus gustos. \
Hablas entre canciones como una locutora joven, cercana, divertida y con mucha calle. Mezclas jerga de Colombia y \
de Ecuador.

Cómo hablas:
- Español latino muy informal y natural. Jerga que puedes usar (una a tres por intervención, varía siempre): \
quiubo, parce, de una, bacán, bacano, chimba, qué fue, ñaño, ñaña, pana, full, de ley, farra, chuchaqui, camello, \
chuta, chucha, qué bestia, mijo, mija, rola, temazo, súbele.
- Nunca digas "qué más pues" ni "¿ah?". Nada de tono de presentador formal ("Buenas noches, fulano...").
- Groserías: {swear}
- Lo que escribes lo lee una voz sintética. Por eso: frases cortas; signos ¡! y ¿? para dar entonación; coma SOLO \
donde de verdad hay una pausa al hablar. Nada de coma antes de un vocativo (ñaño, mijo, parce, pana, el nombre del \
oyente) ni antes de "o qué?" o "sí o qué?", ni entre frases que se dicen de corrido ("¡Súbele todo que hoy no hay \
chuchaqui que nos pare!"). Ejemplos buenos: "Ya es tarde mijo." "¡Chucha, qué temazo el de The Marías!" \
"¿Te saltaste esa? ¡Qué falta de respeto parce! Bueno, de una vamos con otra."
- Nunca uses puntos suspensivos, comillas, paréntesis, guiones, emojis, hashtags, asteriscos ni abreviaturas. \
Escribe "pa" (sin apóstrofo). La hora en palabras de radio: "las 9 y 40", "las 10 en punto", "la 1 y media".
- Di el nombre del oyente a veces, no en cada intervención.

Qué cuentas:
- Presenta la canción que viene (título y artista, el título tal cual) de forma natural, como en la radio.
- Cuando haya DATOS, aprovéchalos para contar algo interesante o curioso del artista o de la canción, contado con \
tus palabras y con gracia, no leído.
- Usa solo datos que vengan en DATOS o que sepas con total seguridad de artistas muy conocidos. Si dudas, no lo \
digas. Nunca inventes cifras, fechas, premios, colaboraciones ni anécdotas. Mejor una opinión con gracia que un \
dato falso.
- Lo personal sale del CONTEXTO (lo que más escucha, si se saltó una, la hora, el día). No inventes cosas de su vida.
- No repitas aperturas, muletillas ni chistes de tus frases anteriores.
- Largo: {length}

Responde SOLO con lo que vas a decir, en un párrafo, sin nada más."""

_SWEAR = {
    0: "ninguna. Nada de malas palabras.",
    1: "como mucho una suave (chucha, puta) y no en todas las intervenciones.",
    2: "permitidas y con gusto (hijueputa, puta, chucha, verga solo si queda claro que es algo bueno), siempre como "
       "exclamación de emoción. Nunca insultes al oyente ni a nadie y nada ofensivo hacia ningún grupo (no uses "
       "marica).",
}
_LEN = {"intro": "entre 25 y 45 palabras.", "block": "entre 20 y 40 palabras.", "song": "entre 12 y 32 palabras.",
        "skip": "entre 8 y 20 palabras.", "vibe": "entre 10 y 22 palabras."}
_KIND_ES = {"favoritas": "sus favoritas (lo que más escucha)", "descubrir": "descubrimientos: canciones que no conoce",
            "recuerdos": "recuerdos: canciones de sus me gusta que hace tiempo no pone",
            "mezcla": "mezcla de lo suyo con cosas nuevas parecidas", "vibra": "cambio de vibra que pidió el oyente"}


def _say_title(t):
    t = re.sub(r"\s*[(\[][^)\]]*(official|oficial|video|lyric|letra|audio|visuali[sz]er|remaster)[^)\]]*[)\]]", "",
               t or "", flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def _say_artist(a):   # "SZA, The Weeknd, Travis Scott" -> "SZA, The Weeknd y Travis Scott"
    parts = [x.strip() for x in re.split(r",|&| feat\.? | ft\.? ", a or "") if x.strip()]
    return a if len(parts) < 2 else ", ".join(parts[:-1]) + " y " + parts[-1]


def _hora(hhmm):
    try:
        h, m = (int(x) for x in str(hhmm).split(":")[:2])
    except ValueError:
        return ""
    h12 = h % 12 or 12
    art = "la" if h12 == 1 else "las"
    tail = {0: " en punto", 15: " y cuarto", 30: " y media"}.get(m, " y %d" % m if m else "")
    if m >= 50:   # "las 10 menos 10" suena raro en radio joven: "casi las 10"
        n = (h + 1) % 12 or 12
        return "casi %s %d" % ("la" if n == 1 else "las", n)
    return "%s %d%s" % (art, h12, tail)


def _why_es(w):
    t = (w or {}).get("t")
    if t == "top":
        d = w.get("days") or 0
        return "es de lo que más escucha: la puso %d veces%s" % (w.get("n", 0), " en los últimos %d días" % d if d else "")
    if t == "liked":
        return "está en sus me gusta"
    if t == "old":
        return "está en sus me gusta pero hace tiempo que no la pone"
    if t == "seed":
        return "es una de las suyas y abre el bloque"
    if t == "new":
        s = w.get("seed") or {}
        return "no la conoce todavía; se parece a %s de %s, que le gusta" % (s.get("title"), s.get("artist"))
    return ""


def _ai(ctx, fx, prev_lines, cfg):
    kind = ctx["kind"]
    t, p, b = ctx["track"], ctx.get("prev") or {}, ctx.get("block") or {}
    lines = ["CONTEXTO"]
    if ctx.get("name"):
        lines.append("- Oyente: %s" % ctx["name"])
    tm = ctx.get("time") or {}
    if tm.get("hhmm"):
        lines.append("- Cuando hables serán %s del %s (%s)%s" % (
            _hora(tm["hhmm"]), tm.get("wd", ""), tm.get("part", ""),
            ". Di la hora" if ctx.get("say_time") else ". No hace falta decir la hora"))
    what = {"intro": "Arranca la radio: saluda y presenta la primera canción.",
            "block": "Empieza un bloque nuevo: %s." % _KIND_ES.get(b.get("kind"), ""),
            "song": "Entre canciones del mismo bloque: cuenta algo de la que viene.",
            "skip": "El oyente se acaba de saltar una canción: reacciona con gracia y presenta la siguiente.",
            "vibe": "El oyente pidió cambiar de vibra: confirma con energía y presenta la siguiente."}[kind]
    lines.append("- Momento: " + what)
    if p.get("title") and kind != "intro":
        lines.append("- Acaba de sonar: %s de %s%s" % (p["title"], p.get("artist", ""),
                                                     " (se la saltó a los pocos segundos)" if p.get("skipped") else ""))
    lines.append("- Ahora suena: %s de %s" % (t["title"], t.get("artist", "")))
    why = _why_es(t.get("why"))
    if why:
        lines.append("- Por qué la elegiste: " + why)
    if b.get("next"):
        lines.append("- Después en este bloque vienen: " + "; ".join(b["next"][:3]))
    if fx:
        lines.append("\nDATOS (reales, de YouTube Music y Wikipedia)")
        if fx.get("bio"):
            lines.append("- Sobre %s: %s" % (t.get("artist", ""), fx["bio"]))
        if fx.get("oyentes"):
            lines.append("- %s tiene %s en YouTube Music" % (t.get("artist", ""), fx["oyentes"]))
        if fx.get("cancion"):
            lines.append("- Sobre la canción: " + fx["cancion"])
        if fx.get("reproducciones"):
            lines.append("- La canción lleva %s de reproducciones en YouTube" % fx["reproducciones"])
    if prev_lines:
        lines.append("\nTUS ÚLTIMAS INTERVENCIONES (no repitas aperturas ni muletillas)")
        lines += ["- " + x for x in prev_lines[-6:]]
    system = _SYS.format(swear=_SWEAR[ctx["swear"]], length=_LEN[kind])
    model = MODELS[cfg["model"]]
    body = {"model": model, "max_tokens": 2000, "system": system, "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": "\n".join(lines)}]}
    hdr = {"Content-Type": "application/json", "x-api-key": cfg["key"], "anthropic-version": "2026-10-21"}
    if cfg["model"] == "opus":   # si un filtro de seguridad rechaza (groserias...), reintenta en otro modelo
        body["fallbacks"] = "default"
        hdr["anthropic-beta"] = "server-side-fallback-2026-07-01"
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode("utf8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf8")).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError("API %s %s" % (e.code, msg[:160]))
    if d.get("stop_reason") == "refusal":
        raise RuntimeError("rechazado")
    return "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text")


# plantillas (sin clave de la API o si la API falla): mismo estilo, datos sencillos
_T = {
    "intro": [
        "¡Quiubo {n}! Son {h} y arrancamos tu radio con todo. Primero va {t} de {a}. ¡De una súbele!",
        "¡Qué fue {n}! Aquí Lara en tu radio personal. Son {h} así que vamos con algo que te encanta. Suena {t} de {a}.",
        "¡Hola {n}! Son {h} de este {wd} y ya tengo armada tu música. Arrancamos con {t} de {a}. ¡Dale nomás!",
    ],
    "favoritas": [
        "Eso fue {p}. Ahora se viene un bloque de tus favoritas ñaño. Arrancamos con {t} de {a}.",
        "¡Qué temazo {p}! Seguimos con lo que más escuchas. Va {t} de {a}.",
        "Bloque de puras favoritas {n}. Empezamos con {t} de {a}. ¡Súbele pa que suene!",
    ],
    "descubrir": [
        "Bueno {n} ahora te traigo cosas que todavía no conoces. Primero {t} de {a}. Confía en mí.",
        "Hora de descubrir música pana. Esta es {t} de {a} y de ley te va a gustar si te gusta {s}.",
        "Eso fue {p}. Ahora te pongo algo nuevo que tiene la misma onda. Va {t} de {a}.",
    ],
    "recuerdos": [
        "¿Te acuerdas de esta? Hace rato que no la ponías. Va {t} de {a}. ¡Qué bestia cómo pasa el tiempo!",
        "Bloque de recuerdos parce. Volvemos a {t} de {a} que la tenías bien guardadita en tus me gusta.",
    ],
    "mezcla": [
        "Eso fue {p}. Ahora mezclamos lo tuyo con cositas nuevas. Sigue {t} de {a}.",
        "Seguimos {n}. Te armé una mezcla de lo que te gusta con algo que no conoces. Va {t} de {a}.",
    ],
    "vibra": ["¡De una cambiamos de vibra! Va {t} de {a}.", "¡Listo, otra onda! Arrancamos con {t} de {a}."],
    "song_top": ["Esta ya la pusiste {nn} veces {n}. Oficialmente es tu canción. Va {t}.",
                 "Otra de tus favoritas. {t} de {a}. ¡Súbele!"],
    "song_new": ["Esta todavía no la conoces. Es {t} de {a}. Dale una oportunidad.",
                 "Va una nueva pa ti. {t} de {a}. ¡De ley te va a gustar!"],
    "song_plays": ["{t} de {a} ya pasa de {pl} reproducciones. ¡Qué bestia!"],
    "song": ["Sigue {a} con {t}. ¡Súbele pa que suene!", "Ahora va {t} de {a}. ¡Qué chévere esta!"],
    "skip": ["¿Te saltaste esa? ¡Qué falta de respeto parce! Bueno, de una vamos con {t}.",
             "Ya ya entendí que esa no era. Mejor {t} de {a}."],
}
_EXCL = {1: ["¡Chucha!", "¡Uy chuta!"], 2: ["¡Chucha!", "¡Puta qué nota!", "¡Ay hijueputa, qué rola!"]}


def _tmpl(ctx, fx):
    kind, t, p, b = ctx["kind"], ctx["track"], ctx.get("prev") or {}, ctx.get("block") or {}
    w = t.get("why") or {}
    if kind == "block":
        key = b.get("kind") if b.get("kind") in _T else "mezcla"
    elif kind == "song":
        key = ("song_plays" if fx.get("reproducciones") and random.random() < .5 else
               "song_top" if w.get("t") == "top" and w.get("n", 0) >= 3 else
               "song_new" if w.get("t") == "new" else "song")
    else:
        key = "vibra" if kind == "vibe" else kind
    opts = [x for x in _T.get(key, _T["song"]) if ("{p}" not in x or p.get("title")) and ("{s}" not in x or w.get("seed"))]
    line = random.choice(opts or _T["song"])
    seed = w.get("seed") or {}
    line = line.format(n=ctx.get("name") or "", t=t["title"], a=t.get("artist", ""), p=p.get("title", ""),
                       h=_hora((ctx.get("time") or {}).get("hhmm")) or "la hora de la música",
                       wd=(ctx.get("time") or {}).get("wd", ""), s=seed.get("artist", ""),
                       nn=w.get("n", 0), pl=fx.get("reproducciones", ""))
    if ctx["swear"] and kind != "intro" and random.random() < (.3 if ctx["swear"] == 1 else .55):
        line = random.choice(_EXCL[ctx["swear"]]) + " " + line
    elif ctx.get("say_time") and kind in ("block", "song") and (ctx.get("time") or {}).get("hhmm") and "{h}" not in line:
        line = "Son %s. %s" % (_hora(ctx["time"]["hhmm"]), line)
    return line


def _clean(text):
    text = re.sub(r"[*_#\"“”«»()\[\]]|[\U0001F000-\U0001FFFF☀-➿]", "", text or "")
    text = text.replace("...", ".").replace("…", ".").replace("pa'", "pa").replace("—", ", ").replace(" - ", ", ")
    text = re.sub(r"\s+", " ", re.sub(r"\s+([,.!?])", r"\1", text)).strip()
    text = re.sub(r"(^|[¡¿])\s*,\s*", r"\1", text)   # nombre vacio en plantilla: "¡Quiubo !" -> "¡Quiubo!"
    return text[:600]


# ---------- voz ----------
def _evict(d):
    try:
        fs = sorted((os.path.getmtime(os.path.join(d, n)), n) for n in os.listdir(d) if n.endswith(".mp3"))
        for _, n in fs[:max(0, len(fs) - _MAX_FILES)]:
            os.remove(os.path.join(d, n))
    except OSError:
        pass


def tts(text):
    import edge_tts
    h = hashlib.sha1(("%s|%s|%s|%s" % (VOICE, RATE, PITCH, text)).encode()).hexdigest()[:20]
    d = _dir()
    out = os.path.join(d, h + ".mp3")
    if os.path.exists(out):
        return h
    raw = os.path.join(d, h + ".raw.mp3")
    for i in range(2):
        try:
            asyncio.run(edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH).save(raw))
            break
        except Exception:
            if i:
                raise
            time.sleep(1)
    tmp = out + ".tmp.mp3"
    try:
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}   # sin ventana de consola en Windows
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, "-af", _TRIM, "-ar", "24000", "-ac", "1",
                            "-c:a", "libmp3lame", "-b:a", "48k", tmp], capture_output=True, timeout=60, **kw)
        if r.returncode or not os.path.getsize(tmp):
            raise OSError("ffmpeg")
        os.replace(tmp, out)
        os.remove(raw)
    except (OSError, subprocess.SubprocessError):   # sin ffmpeg: la voz tal cual (pausas algo mas largas)
        os.replace(raw, out)
    _evict(d)
    return h


def audio_path(h):
    if not re.fullmatch(r"[0-9a-f]{20}", h or ""):
        return None
    p = os.path.join(_dir(), h + ".mp3")
    return p if os.path.exists(p) else None


# ---------- una intervencion ----------
def _mem(acct):
    with _mem_lock:
        m = _MEM.get(acct)
        day = time.strftime("%Y-%m-%d")
        if not m or m["day"] != day:
            m = _MEM[acct] = {"day": day, "ai": 0, "lines": (m or {}).get("lines") or deque(maxlen=10)}
        for k in [k for k, v in _MEM.items() if v["day"] != day and k != acct]:
            _MEM.pop(k, None)   # cuentas que no escuchan desde ayer
        return m


def say(ctx, acct):
    kind = ctx.get("kind") if ctx.get("kind") in _LEN else "song"
    t = _song(ctx.get("track"))
    if not t:
        return {"error": "?"}
    t["why"] = ctx.get("track", {}).get("why") if isinstance(ctx.get("track", {}).get("why"), dict) else {}
    t["title"] = _say_title(t["title"]) or t["title"]
    t["artist"] = _say_artist(t["artist"])
    p = ctx.get("prev") if isinstance(ctx.get("prev"), dict) else {}
    b = ctx.get("block") if isinstance(ctx.get("block"), dict) else {}
    c = {"kind": kind, "track": t, "name": re.sub(r"[^\w\s'-]", "", str(ctx.get("name") or ""))[:30].strip(),
         "prev": {"title": _say_title(str(p.get("title") or ""))[:150], "artist": _say_artist(str(p.get("artist") or ""))[:150],
                  "skipped": bool(p.get("skipped"))},
         "block": {"kind": b.get("kind") if b.get("kind") in _KIND_ES else "",
                   "next": [str(x)[:120] for x in (b.get("next") or [])[:4]]},
         "time": {k: str((ctx.get("time") or {}).get(k) or "")[:20] for k in ("hhmm", "wd", "part")},
         "say_time": bool(ctx.get("say_time")), "swear": ctx.get("swear") if ctx.get("swear") in (0, 1, 2) else 1}
    m, cfg = _mem(acct), config()
    fx = {}
    try:   # la IA cuenta datos del artista/cancion; las plantillas solo usan las reproducciones (mas rapido)
        if cfg["key"] and kind in ("intro", "block", "song"):
            fx = facts(t)
        elif kind == "song":
            fx = {"reproducciones": _plays((APP.viewcounts([t["id"]]) or {}).get(t["id"]))}
    except Exception:
        fx = {}
    text, ai = "", False
    if cfg["key"] and m["ai"] < _AI_DAY and time.time() > _AI_ERR["pause"]:
        try:
            text = _clean(_ai(c, fx, list(m["lines"]), cfg))
            ai = bool(text)
            m["ai"] += 1
            _AI_ERR["msg"] = ""
        except Exception as e:
            msg = str(e)
            print("[dj] guion con IA fallo: %s" % msg, flush=True)
            low = msg.lower()
            _AI_ERR.update(at=time.time(), msg=(
                "Tu cuenta de Anthropic no tiene saldo: compra créditos en console.anthropic.com (Plans & Billing)"
                if "credit" in low else "La clave de Anthropic no es válida" if " 401 " in msg or "authentication" in low
                else "La IA falló: " + msg[:120]))
            if "credit" in low or " 401 " in msg:
                _AI_ERR["pause"] = time.time() + 600   # sin saldo / clave mala: no reintentar en cada cancion
    if not text:
        text = _clean(_tmpl(c, fx))
    m["lines"].append(text)
    try:
        h = tts(text)
    except ImportError:
        return {"text": text, "ai": ai, "error": "Falta la voz (edge-tts) en el servidor"}
    except Exception as e:
        return {"text": text, "ai": ai, "error": "No se pudo generar la voz: %s" % str(e)[:120]}
    return {"text": text, "ai": ai, "audio": "/dj/a/%s.mp3" % h}
