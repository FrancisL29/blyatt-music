"""Skill de Alexa: pedir canciones, artistas, albumes y las playlists de tu cuenta y que suenen en el Echo.

Alexa -> POST https://blyatt.stream/alexa (peticion firmada por Amazon) -> respuesta con AudioPlayer.Play
-> el Echo pide el audio a https://blyatt.stream/alexa/a/<id>.m4a?t=<firma> (sin cookies: la firma HMAC
es la llave). La cola vive aqui, por usuario de Amazon: cuando una cancion esta por acabar Alexa avisa
(PlaybackNearlyFinished) y se le encola la siguiente; al final de la lista siguen recomendaciones (radio).

Vincular: en la app (menu de usuario -> Conectar Alexa) sale un codigo de 4 cifras; se le dice a la skill
una vez y ese usuario de Amazon queda unido a la sesion de YouTube Music de ese dispositivo. La skill queda
fijada a la primera que se vincula (otra skill que apunte aqui no sirve de nada).

Seguridad: cada peticion se verifica con la firma de Amazon (certificado bajado por HTTPS de su bucket
oficial, RSA-SHA256 con la biblioteca estandar) y su marca de tiempo (< 150 s).
"""
import base64
import concurrent.futures
import difflib
import hashlib
import hmac
import json
import os
import posixpath
import random
import re
import secrets
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

APP = None   # app.py se inyecta aqui (busqueda, biblioteca, audio, sesiones)

_LOCK = threading.RLock()
_CFG = None
_STATE = {}
_PAIR = {}   # codigo -> (caduca, archivo de sesion)
_PAIR_FAILS = []
M4A_CACHE_MB = 600


def _path(*p):
    d = os.path.join(APP.DATA, *p[:-1])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, p[-1])


def _cfg():
    global _CFG
    if _CFG is None:
        try:
            with open(_path("auth", "alexa.json"), encoding="utf-8") as f:
                _CFG = json.load(f)
        except (OSError, ValueError):
            _CFG = {}
        _CFG.setdefault("secret", secrets.token_hex(32))
        _CFG.setdefault("skill", "")
        _CFG.setdefault("users", {})
        _save_cfg()
    return _CFG


def _save_cfg():
    tmp = _path("auth", "alexa.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_CFG, f)
    os.replace(tmp, _path("auth", "alexa.json"))


def _load_state():
    if not _STATE:
        try:
            with open(_path("cache", "alexa_state.json"), encoding="utf-8") as f:
                _STATE.update(json.load(f))
        except (OSError, ValueError):
            pass
    return _STATE


def _save_state():
    try:
        tmp = _path("cache", "alexa_state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STATE, f)
        os.replace(tmp, _path("cache", "alexa_state.json"))
    except OSError:
        pass


# ---------------------------------------------------------------- vincular (codigo desde la app)
def pair_new(session_file):
    """Codigo de 4 cifras (10 min, un uso) para unir un usuario de Amazon a la sesion de este dispositivo."""
    now = time.time()
    with _LOCK:
        for c in [c for c, v in _PAIR.items() if v[0] < now or v[1] == session_file]:
            _PAIR.pop(c, None)
        code = "%04d" % secrets.randbelow(10 ** 4)
        while code in _PAIR:
            code = "%04d" % secrets.randbelow(10 ** 4)
        _PAIR[code] = (now + 600, session_file)
    return {"code": code, "expires": 600}


def _pair_use(uid, app_id, code):
    now = time.time()
    with _LOCK:
        _PAIR_FAILS[:] = [t for t in _PAIR_FAILS if now - t < 600]
        if len(_PAIR_FAILS) >= 10:   # por si alguien prueba codigos a ciegas
            return False
        v = _PAIR.pop(code, None)
        if not v or v[0] < now:
            _PAIR_FAILS.append(now)
            return False
        cfg = _cfg()
        cfg["skill"] = cfg["skill"] or app_id
        cfg["users"][uid] = {"file": os.path.basename(v[1]), "t": int(now)}
        _save_cfg()
    return True


# ---------------------------------------------------------------- firma de Amazon
_CERTS = {}   # url -> (valido hasta, n, e)
_SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_RSA_OID = bytes.fromhex("2a864886f70d010101")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _tlv(b, i):
    t, n = b[i], b[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    return t, i, i + n


def _items(b, s, e):
    out = []
    while s < e:
        t, cs, ce = _tlv(b, s)
        out.append((t, cs, ce))
        s = ce
    return out


def _rsa_key(der):
    """Clave publica RSA (n, e) de un certificado X.509 en DER (sin dependencias)."""
    _, s, e = _tlv(der, 0)
    tbs = _items(der, s, e)[0]
    f = _items(der, tbs[1], tbs[2])
    if f[0][0] == 0xA0:   # version
        f = f[1:]
    spki = f[5]           # serial, algoritmo, emisor, validez, sujeto, clave publica
    alg, bits = _items(der, spki[1], spki[2])
    if _RSA_OID not in der[alg[1]:alg[2]]:
        raise ValueError("no es RSA")
    _, ks, ke = _tlv(der, bits[1] + 1)   # el primer byte del BIT STRING = bits sin usar
    n_, e_ = _items(der, ks, ke)
    return int.from_bytes(der[n_[1]:n_[2]], "big"), int.from_bytes(der[e_[1]:e_[2]], "big")


def _cert(url):
    p = urlparse(url)
    if (p.scheme.lower() != "https" or (p.hostname or "").lower() != "s3.amazonaws.com" or p.port not in (None, 443)
            or not posixpath.normpath(p.path).startswith("/echo.api/")):
        return None
    hit = _CERTS.get(url)
    if hit and hit[0] > time.time():
        return hit[1:]
    # HTTPS verificado contra los CA del sistema: el archivo viene del bucket oficial de Amazon
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(url, timeout=6) as r:
        pem = r.read(200000)
    leaf = re.search(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S).group(0)
    fd, tmp = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(leaf)
        info = ssl._ssl._test_decode_cert(tmp)
    finally:
        os.remove(tmp)
    now = time.time()
    t0, t1 = ssl.cert_time_to_seconds(info["notBefore"]), ssl.cert_time_to_seconds(info["notAfter"])
    if not t0 <= now <= t1 or ("DNS", "echo-api.amazon.com") not in info.get("subjectAltName", ()):
        return None
    der = base64.b64decode(b"".join(re.findall(rb"^[A-Za-z0-9+/=]+$", leaf, re.M)))
    n, e = _rsa_key(der)
    _CERTS[url] = (min(t1, now + 86400), n, e)
    return n, e


def _verify(raw, headers, body):
    if os.environ.get("BLYATT_ALEXA_NOVERIFY") == "1":   # pruebas locales con peticiones de mentira
        return True
    try:
        key = _cert(headers.get("SignatureCertChainUrl") or "")
        sig = base64.b64decode(headers.get("Signature-256") or "")
        if not key or not sig:
            return False
        n, e = key
        k = (n.bit_length() + 7) // 8
        em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
        want = b"\x00\x01" + b"\xff" * (k - 3 - len(_SHA256_INFO) - 32) + b"\x00" + _SHA256_INFO + hashlib.sha256(raw).digest()
        if not hmac.compare_digest(em, want):
            return False
        ts = datetime.fromisoformat(body["request"]["timestamp"].replace("Z", "+00:00"))
        return abs((datetime.now(timezone.utc) - ts).total_seconds()) <= 150
    except Exception:
        return False


# ---------------------------------------------------------------- audio para el Echo (m4a solo audio)
def _sig(vid):
    return hmac.new(_cfg()["secret"].encode(), vid.encode(), hashlib.sha256).hexdigest()[:24]


def audio_path(vid, tok):
    """Archivo m4a (solo audio, faststart) para servir al Echo; None si la firma no vale."""
    if not re.fullmatch(r"[\w-]{6,20}", vid or "") or not hmac.compare_digest(_sig(vid), tok or ""):
        return None
    return _prepare(vid)


def _prepare(vid):
    out = _path("cache", "alexa", vid + ".m4a")
    if os.path.isfile(out):
        os.utime(out)
        return out
    with APP._id_lock("alexa", vid):
        if os.path.isfile(out):
            return out
        mp4 = APP._audio_path(vid)
        with APP._id_lock("dl", vid):
            if not os.path.isfile(mp4) or not APP._mp4_ok(mp4):
                APP.audio_fetch(vid)
        if not shutil.which("ffmpeg"):
            return mp4   # sin ffmpeg: el mp4 tal cual (lleva una pista de video pequena)
        tmp = out + ".part.m4a"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp4, "-vn", "-c:a", "copy", "-movflags", "+faststart",
                            "-f", "mp4", tmp], capture_output=True, timeout=120)
        if r.returncode or not os.path.isfile(tmp):
            return mp4
        os.replace(tmp, out)
    _evict()
    return out


def _evict():
    d = os.path.dirname(_path("cache", "alexa", "x"))
    try:
        files = sorted((os.stat(os.path.join(d, n)).st_mtime, os.path.getsize(os.path.join(d, n)), os.path.join(d, n))
                       for n in os.listdir(d) if n.endswith(".m4a") and ".part" not in n)
        total = sum(f[1] for f in files)
        for _, size, fp in files:
            if total <= M4A_CACHE_MB * 1024 * 1024:
                break
            os.remove(fp)
            total -= size
    except OSError:
        pass


_POOL = []


def _warm(ids):
    """Prepara en segundo plano el audio de las proximas (el Echo lo pide segundos despues)."""
    def run(v):
        try:
            _prepare(v)
        except Exception:
            pass
    for v in ids:
        if v and not os.path.isfile(_path("cache", "alexa", v + ".m4a")):
            th = threading.Thread(target=run, args=(v,), daemon=True)
            th.start()


# ---------------------------------------------------------------- que poner
def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def _track(x, album=None):
    if not x or not x.get("id"):
        return None
    cover = re.sub(r"=w\d+-h\d+", "=w544-h544", x.get("cover") or (album or {}).get("cover") or "")
    return {"id": x["id"], "title": x.get("title") or "", "artist": x.get("artist") or (album or {}).get("creator") or "",
            "cover": cover}


def _tracks(lst, album=None):
    seen, out = set(), []
    for x in lst or []:
        t = _track(x, album)
        if t and t["id"] not in seen:
            seen.add(t["id"])
            out.append(t)
    return out


def _library():
    lib = APP.cached(APP._sk("alexa:lib"), 300, lambda: APP.yt_library(["lib"]))
    return lib.get("playlists") or [] if isinstance(lib, dict) else []


def _best_playlist(name, cutoff=.72):
    """Playlist propia que mas se parece a lo dicho (o None)."""
    q = _norm(name)
    best, score = None, 0
    for p in _library():
        t = _norm(p.get("title"))
        if not t:
            continue
        sc = difflib.SequenceMatcher(None, q, t).ratio()
        if q == t:
            sc = 1.0
        elif q in t.split() or (len(q) > 3 and (q in t or t in q)):
            sc = max(sc, .85)
        if sc > score:
            best, score = p, sc
    return best if score >= cutoff else None


def _collection_tracks(browse_id):
    c = APP.collection(browse_id)
    return _tracks(c.get("tracks"), c), c.get("title") or ""


def _liked():
    d = APP.yt_library(["head"])
    return _tracks((d or {}).get("liked") if isinstance(d, dict) else [])


def _artist(name):
    """Sus canciones mas escuchadas y mas suyas (busqueda), en orden aleatorio; luego, su radio."""
    found = APP.search(name, "artists") or []
    a = found[0] if found else None
    if not a or not a.get("browseId"):
        return [], ""
    title = a.get("title") or name
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        page = ex.submit(APP.artist, a["browseId"])
        more = ex.submit(APP.search, title, "songs")
        top = []
        for s in page.result().get("sections") or []:
            if s.get("items") and s["items"][0].get("id"):
                top = s["items"]
                break
        mine = [x for x in more.result() or [] if _norm(title) in _norm(x.get("artist"))]
    songs = _tracks(top + mine)
    random.shuffle(songs)
    return songs, title


def _album(name):
    found = APP.search(name, "albums") or []
    if not found or not found[0].get("browseId"):
        return [], ""
    tr, title = _collection_tracks(found[0]["browseId"])
    return tr, title


def _song(name):
    """La cancion; sus recomendaciones se piden cuando haga falta (_extend), la respuesta sale antes."""
    found = APP.search(name, "songs") or []
    t = _track(found[0]) if found else None
    return ([t], t["title"]) if t else ([], "")


def _resolve(query):
    """Lo que se pidio con "pon ..." -> (pistas, frase). Como la busqueda de YT Music: su mejor resultado
    manda (artista, album, playlist o cancion); una playlist propia con ese nombre va primero."""
    q = (query or "").strip()
    if _norm(q) in ("musica", "mi musica", "algo", "lo que sea", "mis favoritas", "mis me gusta", "me gusta"):
        tr = _liked()
        random.shuffle(tr)
        return tr, "tus me gusta"
    p = _best_playlist(q, .9)
    if p:
        tr, title = _collection_tracks(p["browseId"])
        return tr, "tu playlist " + title
    try:
        best = (APP.search_all(q).get("sections") or [{}])[0].get("items", [None])[0] or {}
    except Exception:
        best = {}
    kind, bid = best.get("kind"), best.get("browseId") or ""
    if kind == "artists":
        tr, name = _artist(best.get("title") or q)
        return tr, "música de " + name
    if kind in ("albums", "playlists") and bid:
        tr, title = _collection_tracks(bid)
        return tr, title
    tr, title = _song(q)
    return tr, title


# ---------------------------------------------------------------- cola y directivas
def _st(uid):
    st = _load_state().setdefault(uid, {})
    st.setdefault("q", [])
    st.setdefault("i", 0)
    st.setdefault("v", 0)
    st.setdefault("off", 0)
    st.setdefault("shuffle", False)
    st.setdefault("loop", False)
    return st


def _new_queue(st, tracks, shuffle=False):
    st["v"] += 1
    st["q"], st["i"], st["off"] = tracks[:400], 0, 0
    st["orig"] = [t["id"] for t in st["q"]]
    if shuffle or st.get("shuffle"):
        first, rest = st["q"][:1], st["q"][1:]
        random.shuffle(rest)
        st["q"] = first + rest


def _token(st, i):
    return "%d.%d.%s" % (st["v"], i, st["q"][i]["id"])


def _parse_token(st, tok):
    """Indice en la cola de la pista de un token (aunque la cola se haya reordenado al activar aleatorio)."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.([\w-]+)", tok or "")
    if not m or int(m.group(1)) != st["v"]:
        return None
    i, vid = int(m.group(2)), m.group(3)
    if 0 <= i < len(st["q"]) and st["q"][i]["id"] == vid:
        return i
    hits = [j for j, t in enumerate(st["q"]) if t["id"] == vid]
    return min(hits, key=lambda j: abs(j - st["i"])) if hits else None


def _extend(st):
    """Al final de la lista: recomendaciones de la ultima (como la cola infinita de la app)."""
    if st.get("loop") or len(st["q"]) - st["i"] > 3 or not st["q"]:
        return
    have = {t["id"] for t in st["q"]}
    try:
        more = [t for t in _tracks(APP.radio(st["q"][-1]["id"])) if t["id"] not in have][:20]
    except Exception:
        more = []
    st["q"] += more
    st["orig"] = st.get("orig", []) + [t["id"] for t in more]


def _next_index(st, i):
    if i + 1 < len(st["q"]):
        return i + 1
    if st.get("loop") and st["q"]:
        return 0
    return None


def _play(st, host, i, behavior="REPLACE_ALL", offset=0, prev=None):
    t = st["q"][i]
    stream = {"url": "https://%s/alexa/a/%s.m4a?t=%s" % (host, t["id"], _sig(t["id"])), "token": _token(st, i),
              "offsetInMilliseconds": int(offset)}
    if prev:
        stream["expectedPreviousToken"] = prev
    meta = {"title": t["title"], "subtitle": t["artist"]}
    if t["cover"].startswith("https://"):
        meta["art"] = {"sources": [{"url": t["cover"]}]}
    nxt = [st["q"][j]["id"] for j in (i, i + 1) if j < len(st["q"])]
    _warm(nxt)
    return {"type": "AudioPlayer.Play", "playBehavior": behavior, "audioItem": {"stream": stream, "metadata": meta}}


def _say(text, end=True, reprompt=None, directives=None):
    r = {"shouldEndSession": end}
    if text:
        r["outputSpeech"] = {"type": "PlainText", "text": text}
    if reprompt:
        r["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    if directives:
        r["directives"] = directives
    return r


def _quiet(directives=None):
    return {"directives": directives} if directives else {}


def _stop():
    return {"type": "AudioPlayer.Stop"}


HELP = ("Puedes decirme: pon Bohemian Rhapsody, pon música de Queen, pon el álbum Abbey Road, "
        "pon mi playlist favoritas, o pon mis me gusta. ¿Qué te pongo?")
PAIR_HELP = ("Primero vincula tu cuenta: abre Blyatt en tu móvil, toca tu foto, elige Conectar Alexa "
             "y dime el código de cuatro números. Por ejemplo: el código es uno dos tres cuatro.")


def _start(st, host, tracks, what, shuffle=False):
    if not tracks:
        return _say("No encontré %s." % (what or "nada con ese nombre"), end=False,
                    reprompt="¿Qué otra cosa te pongo?")
    _new_queue(st, tracks, shuffle)
    t = st["q"][0]
    first = t["title"] + (" de " + t["artist"] if t["artist"] else "")
    text = ("Poniendo %s." % what) if what and _norm(what) != _norm(t["title"]) else "Poniendo %s." % first
    return _say(text, directives=[_play(st, host, 0)])


def _slot(req, name):
    s = ((req.get("intent") or {}).get("slots") or {}).get(name) or {}
    return (s.get("value") or "").strip()


def _intent(st, uid, host, req, app_id):
    name = (req.get("intent") or {}).get("name", "")
    if name == "PairIntent":
        code = re.sub(r"\D", "", _slot(req, "code"))
        if code and _pair_use(uid, app_id, code.zfill(4)):
            return _say("Listo, ya está vinculada tu cuenta. ¿Qué te pongo?", end=False, reprompt="¿Qué quieres escuchar?")
        return _say("Ese código no es válido o ya caducó. Pide uno nuevo en la app y dímelo.", end=False,
                    reprompt="Dime el código de cuatro números.")
    if name == "PlayIntent":
        tracks, what = _resolve(_slot(req, "query"))
        return _start(st, host, tracks, what)
    if name == "PlayPlaylistIntent":
        q = _slot(req, "playlist")
        p = _best_playlist(q)
        if p:
            tracks, title = _collection_tracks(p["browseId"])
            return _start(st, host, tracks, "tu playlist " + title)
        found = APP.search(q, "playlists") or []
        if found and found[0].get("browseId"):
            tracks, title = _collection_tracks(found[0]["browseId"])
            return _start(st, host, tracks, "la playlist " + title)
        return _start(st, host, [], "la playlist " + q)
    if name == "PlayLikedIntent":
        return _start(st, host, _liked(), "tus me gusta")
    if name == "PlayArtistIntent":
        tracks, artist = _artist(_slot(req, "artist"))
        return _start(st, host, tracks, "música de " + artist if artist else "")
    if name == "PlayAlbumIntent":
        tracks, title = _album(_slot(req, "album"))
        return _start(st, host, tracks, "el álbum " + title if title else "ese álbum")
    if name == "WhatIsPlayingIntent":
        if not st["q"]:
            return _say("Ahora mismo no está sonando nada.")
        t = st["q"][st["i"]]
        return _say("Está sonando %s%s." % (t["title"], " de " + t["artist"] if t["artist"] else ""))
    if name == "LikeIntent":
        if not st["q"]:
            return _say("No está sonando nada.")
        t = st["q"][st["i"]]
        try:
            APP.rate_song(t["id"], True)
            return _say("Guardada en tus me gusta.")
        except Exception:
            return _say("No pude guardarla ahora.")
    if name == "AMAZON.HelpIntent":
        return _say(HELP, end=False, reprompt="¿Qué quieres escuchar?")
    if name == "AMAZON.FallbackIntent":
        return _say("No te entendí. " + HELP, end=False, reprompt="¿Qué quieres escuchar?")
    if name == "AMAZON.NavigateHomeIntent":
        return _say("")
    if name in ("AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"):
        return _say("", directives=[_stop()])
    if name == "AMAZON.ResumeIntent":
        if not st["q"]:
            return _say("No hay nada para continuar. ¿Qué te pongo?", end=False, reprompt="¿Qué quieres escuchar?")
        return _say("", directives=[_play(st, host, st["i"], offset=st.get("off", 0))])
    return _control(st, host, name, speak=True)


def _control(st, host, name, speak=False):
    """Siguiente/anterior/aleatorio/repetir: por voz o con los botones del Echo (PlaybackController)."""
    if not st["q"]:
        return _say("No está sonando nada.") if speak else _quiet()
    out = (lambda d: _say("", directives=d)) if speak else _quiet
    if name in ("AMAZON.NextIntent", "PlaybackController.NextCommandIssued"):
        _extend(st)
        n = _next_index(st, st["i"])
        if n is None:
            return _say("No hay más canciones en la lista.") if speak else _quiet()
        st["i"], st["off"] = n, 0
        return out([_play(st, host, n)])
    if name in ("AMAZON.PreviousIntent", "PlaybackController.PreviousCommandIssued"):
        st["i"], st["off"] = max(0, st["i"] - 1), 0
        return out([_play(st, host, st["i"])])
    if name in ("AMAZON.StartOverIntent", "AMAZON.RepeatIntent"):
        st["off"] = 0
        return out([_play(st, host, st["i"])])
    if name == "PlaybackController.PlayCommandIssued":
        return _quiet([_play(st, host, st["i"], offset=st.get("off", 0))])
    if name == "PlaybackController.PauseCommandIssued":
        return _quiet([_stop()])
    if name in ("AMAZON.ShuffleOnIntent", "AMAZON.ShuffleOffIntent"):
        on = name.endswith("OnIntent")
        st["shuffle"] = on
        cur, rest = st["q"][:st["i"] + 1], st["q"][st["i"] + 1:]
        if on:
            random.shuffle(rest)
        else:
            order = {v: k for k, v in enumerate(st.get("orig", []))}
            rest.sort(key=lambda t: order.get(t["id"], 1 << 30))
        st["q"] = cur + rest   # la siguiente que se encole ya sale del nuevo orden
        return _say("Aleatorio activado." if on else "Aleatorio desactivado.") if speak else _quiet()
    if name in ("AMAZON.LoopOnIntent", "AMAZON.LoopOffIntent"):
        st["loop"] = name.endswith("OnIntent")
        return _say("Repetiré la lista." if st["loop"] else "Ya no repetiré la lista.") if speak else _quiet()
    return _say("No te entendí. " + HELP, end=False, reprompt="¿Qué quieres escuchar?") if speak else _quiet()


def _event(st, host, req):
    """Eventos del reproductor del Echo (sin voz: solo directivas)."""
    typ = req.get("type", "")
    i = _parse_token(st, req.get("token"))
    if typ == "AudioPlayer.PlaybackStarted" and i is not None:
        st["i"], st["off"] = i, 0
        _extend(st)   # la siguiente ya conocida y preparada mucho antes de que haga falta
        _warm([st["q"][j]["id"] for j in (i + 1, i + 2) if j < len(st["q"])])
        return _quiet()
    if typ == "AudioPlayer.PlaybackNearlyFinished" and i is not None:
        _extend(st)
        n = _next_index(st, i)
        if n is None:
            return _quiet()
        return _quiet([_play(st, host, n, behavior="ENQUEUE", prev=req["token"])])
    if typ == "AudioPlayer.PlaybackStopped" and i is not None:
        st["i"], st["off"] = i, int(req.get("offsetInMilliseconds") or 0)
        return _quiet()
    if typ == "AudioPlayer.PlaybackFailed" and i is not None:   # esa no se pudo: la siguiente
        n = _next_index(st, i)
        if n is None:
            return _quiet()
        st["i"], st["off"] = n, 0
        return _quiet([_play(st, host, n)])
    if typ.startswith("PlaybackController."):
        return _control(st, host, typ)
    return _quiet()


def handle(raw, headers, host):
    """Peticion de Alexa (cuerpo crudo + cabeceras) -> (codigo HTTP, respuesta JSON)."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return 400, {"error": "json"}
    if not _verify(raw, headers, body):
        return 401, {"error": "firma"}
    ctx = (body.get("context") or {}).get("System") or {}
    app_id = ((ctx.get("application") or (body.get("session") or {}).get("application") or {}).get("applicationId") or "")
    uid = (ctx.get("user") or (body.get("session") or {}).get("user") or {}).get("userId") or ""
    req = body.get("request") or {}
    typ = req.get("type", "")
    cfg = _cfg()
    if cfg["skill"] and app_id != cfg["skill"]:
        return 403, {"error": "skill"}
    host = os.environ.get("BLYATT_PUBLIC_HOST") or host
    with _LOCK:
        user = cfg["users"].get(uid)
        if typ == "SessionEndedRequest" or typ == "System.ExceptionEncountered":
            return 200, {"version": "1.0", "response": {}}
        if not user:
            if typ == "IntentRequest" and (req.get("intent") or {}).get("name") == "PairIntent":
                res = _intent({}, uid, host, req, app_id)
            elif typ.startswith("AudioPlayer.") or typ.startswith("PlaybackController."):
                res = _quiet()
            else:
                res = _say(PAIR_HELP, end=False, reprompt="Dime el código de cuatro números.")
            return 200, {"version": "1.0", "response": res}
        APP._REQ.bid = re.sub(r"^browser_|\.json$", "", user["file"])   # la sesion de YouTube Music vinculada
        st = _st(uid)
        try:
            if typ == "LaunchRequest":
                res = _say("¿Qué quieres escuchar?", end=False, reprompt="Por ejemplo: pon mi playlist favoritas.")
            elif typ == "IntentRequest":
                res = _intent(st, uid, host, req, app_id)
            else:
                res = _event(st, host, req)
        except Exception:
            res = _say("Algo falló al buscar eso. Inténtalo de nuevo.") if typ in ("IntentRequest", "LaunchRequest") else _quiet()
        _save_state()
    return 200, {"version": "1.0", "response": res}
