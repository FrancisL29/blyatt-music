#!/usr/bin/env python3
"""Buscador de YouTube Music sin API key. Proxy + estaticos en stdlib."""
import base64
import concurrent.futures
import difflib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode, urljoin
from yt_dlp import YoutubeDL

try:
    import ytmusicapi
    from ytmusicapi import YTMusic
except ImportError:   # login con Google opcional: la app funciona sin ytmusicapi
    YTMusic = None

BASE = os.path.dirname(os.path.abspath(__file__))

# cache TTL en memoria (proceso unico, app local). ponytail: sin tope; añadir LRU si la RAM importa.
_CACHE = {}
def cached(key, ttl, producer):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = producer()
    _CACHE[key] = (time.time(), val)
    return val

# Clave "innertube" publica del cliente web de YT Music (igual para todos, no es una API key de Google Cloud).
YTM_KEY = "AIzaSyC9XL3ZjWddXya6X74dJoCTL-WEYFDNX30"
YTM_URL = "https://music.youtube.com/youtubei/v1/search?key=" + YTM_KEY
YTB_URL = "https://music.youtube.com/youtubei/v1/browse?key=" + YTM_KEY
YTN_URL = "https://music.youtube.com/youtubei/v1/next?key=" + YTM_KEY
CTX = {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20240101.01.00", "hl": "es"}}


def _find_video_id(node):
    # ponytail: la respuesta anida el videoId en varios sitios; busqueda recursiva en vez de rutas fijas fragiles.
    if isinstance(node, dict):
        we = node.get("watchEndpoint")
        if isinstance(we, dict) and we.get("videoId"):
            return we["videoId"]
        for v in node.values():
            r = _find_video_id(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_video_id(v)
            if r:
                return r
    return None


def _find_str(node, key):
    if isinstance(node, dict):
        v = node.get(key)
        if isinstance(v, str):
            return v
        for vv in node.values():
            r = _find_str(vv, key)
            if r:
                return r
    elif isinstance(node, list):
        for vv in node:
            r = _find_str(vv, key)
            if r:
                return r
    return ""


def _mv_type(item):
    # ATV = "art track" (audio con caratula); OMV/UGC = music video
    mt = _find_str(item, "musicVideoType")
    return "atv" if mt.endswith("ATV") else ("video" if mt else "")


def _is_explicit(node):
    # badge "Explicit": musicInlineBadgeRenderer con icon MUSIC_EXPLICIT_BADGE
    if isinstance(node, dict):
        if node.get("icon", {}).get("iconType") == "MUSIC_EXPLICIT_BADGE":
            return True
        for v in node.values():
            if _is_explicit(v):
                return True
    elif isinstance(node, list):
        for v in node:
            if _is_explicit(v):
                return True
    return False


def _parse_item(item):
    vid = _find_video_id(item)
    cols = item.get("flexColumns", [])
    def runs(i):
        try:
            return cols[i]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]
        except (IndexError, KeyError, TypeError):
            return []
    title = "".join(r.get("text", "") for r in runs(0))
    # artistas = runs del subtitulo que enlazan a una pagina (descarta separadores/tipo/duracion)
    artists, album = [], None
    for r in runs(1):
        bid = ((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId", "")
        if bid.startswith("UC"):
            artists.append({"name": r.get("text", ""), "id": bid})
        elif (bid.startswith("MPRE") or bid.startswith("VL")) and not album:
            album = {"name": r.get("text", ""), "id": bid}
    artist = ", ".join(a["name"] for a in artists)
    if not artist:
        # sin runs enlazados (YT a veces no linkea al artista): primer run del subtitulo que no sea
        # separador, tipo, duracion ni reproducciones
        for r in runs(1):
            t = (r.get("text") or "").strip()
            if (not t or t in ("•", "·") or t.lower() in ("song", "canción", "cancion", "video", "álbum", "album")
                    or re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", t)
                    or "eproduc" in t or "lays" in t or "stream" in t.lower()):
                continue
            artist = t
            break
    try:
        thumbs = item["thumbnail"]["musicThumbnailRenderer"]["thumbnail"]["thumbnails"]
    except (KeyError, TypeError):
        thumbs = []
    cover = thumbs[-1]["url"] if thumbs else ""
    dur = ""
    for fc in item.get("fixedColumns", []):
        try:
            r = fc["musicResponsiveListItemFixedColumnRenderer"]["text"].get("runs", []) or []
            dur = "".join(x.get("text", "") for x in r) or dur
        except (KeyError, TypeError):
            pass
    if not dur:
        import re as _re
        for ci in range(len(cols)):
            for r in runs(ci):
                t = r.get("text", "") or ""
                if _re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", t.strip()):
                    dur = t.strip(); break
            if dur: break
    plays = ""
    for ci in range(len(cols)):
        t = "".join(r.get("text", "") for r in runs(ci))
        if "eproduc" in t or "lays" in t or "stream" in t.lower():   # "reproducciones" / "plays" / "streams"
            plays = t.strip(); break
    if vid and title:
        out = {"id": vid, "title": title, "artist": artist, "cover": cover, "type": _mv_type(item)}
        if artists: out["artists"] = artists
        if album: out["album"] = album
        if dur: out["duration"] = dur
        if plays:
            out["plays"] = plays
            pn = _plays_num(plays)
            if pn:
                out["plays_n"], out["plays_u"] = pn
        if _is_explicit(item): out["explicit"] = True
        return out
    return None


def _collect_items(node, acc):
    # ponytail: la estructura varia (musicShelf / musicCardShelf); recolecta los items en orden de aparicion.
    if isinstance(node, dict):
        it = node.get("musicResponsiveListItemRenderer")
        if isinstance(it, dict):
            acc.append(it)
        for v in node.values():
            _collect_items(v, acc)
    elif isinstance(node, list):
        for v in node:
            _collect_items(v, acc)


def parse_results(data):
    items = []
    _collect_items(data, items)
    seen, out = set(), []
    for it in items:
        r = _parse_item(it)
        if r and r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def _find_browse_id(node):
    if isinstance(node, dict):
        be = node.get("browseEndpoint")
        if isinstance(be, dict) and be.get("browseId"):
            return be["browseId"]
        for v in node.values():
            r = _find_browse_id(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_browse_id(v)
            if r:
                return r
    return None


def _parse_browse_item(item, kind):
    cols = item.get("flexColumns", [])
    def runs(i):
        try:
            return cols[i]["musicResponsiveListItemFlexColumnRenderer"]["text"]["runs"]
        except (IndexError, KeyError, TypeError):
            return []
    title = "".join(r.get("text", "") for r in runs(0))
    subtitle = "".join(r.get("text", "") for r in runs(1))
    # el browseId propio (album=MPRE, playlist=VL) esta en el nav de nivel superior; _find_browse_id cogeria el del artista
    bid = item.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId") or _find_browse_id(item)
    try:
        thumbs = item["thumbnail"]["musicThumbnailRenderer"]["thumbnail"]["thumbnails"]
    except (KeyError, TypeError):
        thumbs = []
    cover = thumbs[-1]["url"] if thumbs else ""
    if bid and title:
        out = {"browseId": bid, "title": title, "subtitle": subtitle, "cover": cover, "kind": kind}
        if _is_explicit(item): out["explicit"] = True
        return out
    return None


def parse_browse(data, kind):
    items, seen, out = [], set(), []
    _collect_items(data, items)
    for it in items:
        r = _parse_browse_item(it, kind)
        if r and r["browseId"] not in seen:
            seen.add(r["browseId"])
            out.append(r)
    return out


# params de filtro de YouTube Music (verificados contra el endpoint real)
_FILTERS = {
    "songs": "EgWKAQIIAWoKEAkQBRAKEAMQBA==",
    "artists": "EgWKAQIgAWoKEAkQChAFEAMQBA==",
    "albums": "EgWKAQIYAWoKEAkQChAFEAMQBA==",
    "playlists": "EgWKAQIoAWoKEAkQChAFEAMQBA==",
    "profiles": "EgWKAQJYAWoKEAkQChAFEAMQBA==",
}
_BROWSE_KINDS = ("artists", "albums", "playlists", "profiles")


def search(query, filt=""):
    # no cachear vacios: una busqueda transitoriamente vacia no debe quedar pegada 5 min.
    key = "s:%s:%s" % (filt, query.lower().strip())
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    val = _search(query, filt)
    if val and (not isinstance(val, dict) or val.get("sections")):
        _CACHE[key] = (time.time(), val)
    return val


def _ytm_search_raw(query, params=None):
    body = {"context": CTX, "query": query}
    if params:
        body["params"] = params
    req = urllib.request.Request(YTM_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _search(query, filt):
    if not filt:
        return search_all(query)   # "Todo": secciones priorizadas por coincidencia
    data = _ytm_search_raw(query, _FILTERS.get(filt))
    return parse_browse(data, filt) if filt in _BROWSE_KINDS else parse_results(data)


def _kind_of_browse(bid):
    if bid.startswith("UC"):
        return "artists"
    if bid.startswith("MPRE"):
        return "albums"
    if bid.startswith("VL") or bid.startswith("PL"):
        return "playlists"
    return ""


def _parse_any(item):
    # fila de shelf sin filtro: cancion (videoId) o artista/album/playlist (browseId)
    if _find_video_id(item):
        return _parse_item(item)
    bid = item.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId") or ""
    kind = _kind_of_browse(bid)
    return _parse_browse_item(item, kind) if kind else None


def _parse_card(cs):
    # musicCardShelfRenderer = "Mejor resultado" (el propio YT decide si es artista/cancion/album)
    title = _runs_text(cs.get("title"))
    if not title:
        return None
    out = {"title": title, "subtitle": _runs_text(cs.get("subtitle")),
           "cover": _largest_thumb(cs.get("thumbnail", {}))}
    if _is_explicit(cs.get("subtitle")) or _is_explicit(cs.get("subtitleBadges")):
        out["explicit"] = True
    nav = cs.get("title", {}).get("runs", [{}])[0].get("navigationEndpoint", {}) or cs.get("onTap", {})
    vid = nav.get("watchEndpoint", {}).get("videoId")
    bid = nav.get("browseEndpoint", {}).get("browseId", "")
    if vid:
        out["id"] = vid
        # subtitle tipo "Cancion • The Weeknd • 4:23": artista = segmentos sin tipo ni duracion
        parts = [p.strip() for p in out["subtitle"].split("•")]
        skip = {"cancion", "canción", "song", "video", "vídeo"}
        stats = ("visualizac", "views", "reproducc", "streams", "suscriptor", "subscriber")
        arts = [p for p in parts if p and p.lower() not in skip
                and not any(w in p.lower() for w in stats)
                and not re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", p)]
        out["artist"] = ", ".join(arts)
        dur = next((p for p in parts if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", p)), "")
        if dur:
            out["duration"] = dur
        return out
    kind = _kind_of_browse(bid)
    if kind:
        out["browseId"] = bid
        out["kind"] = kind
        return out
    return None


def _find_card(node):
    if isinstance(node, dict):
        cs = node.get("musicCardShelfRenderer")
        if cs:
            return cs
        for v in node.values():
            r = _find_card(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_card(v)
            if r:
                return r
    return None


def search_all(query):
    # "Todo": card "Mejor resultado" (relevancia de YT) + songs/artists/albums/playlists en paralelo.
    # La busqueda de canciones de YT Music tambien matchea por LETRA (query = frase de la letra funciona).
    def top_card():
        try:
            cs = _find_card(_ytm_search_raw(query))
            if not cs:
                return None
            items = []
            c = _parse_card(cs)
            if c:
                items.append(c)
            extra = []
            _collect_items(cs.get("contents"), extra)
            items += [x for x in (_parse_any(i) for i in extra) if x]
            return {"title": "Mejor resultado", "items": items} if items else None
        except Exception:
            return None

    kinds = ("songs", "artists", "albums", "playlists")
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        f_card = ex.submit(top_card)
        futs = {k: ex.submit(search, query, k) for k in kinds}
        card = f_card.result()
        res = {}
        for k in kinds:
            try:
                res[k] = futs[k].result() or []
            except Exception:
                res[k] = []

    nq = _norm_title(query)

    def score(items):
        # mejor coincidencia query<->titulo entre los primeros 3 (asi "blinding lights" pone Canciones arriba)
        return max((difflib.SequenceMatcher(None, nq, _norm_title(x.get("title"))).ratio()
                    for x in items[:3]), default=0)

    titles = {"songs": "Canciones", "artists": "Artistas", "albums": "Álbumes", "playlists": "Playlists"}
    secs = []
    if card:
        secs.append(card)
    top_ids = {x.get("id") or x.get("browseId") for x in (card["items"] if card else [])}
    filtered = {k: [x for x in res[k] if (x.get("id") or x.get("browseId")) not in top_ids][:8] for k in kinds}
    for k in sorted(kinds, key=lambda k: -score(filtered[k])):
        if filtered[k]:
            secs.append({"title": titles[k], "items": filtered[k]})
    return {"sections": secs}


# ---------- pagina de artista (browse) ----------
def _runs_text(obj):
    try:
        return "".join(r.get("text", "") for r in obj["runs"])
    except (KeyError, TypeError):
        return ""


def _largest_thumb(node):
    # busca recursivamente el primer array de thumbnails y devuelve la url mas grande
    if isinstance(node, dict):
        th = node.get("thumbnails")
        if isinstance(th, list) and th and isinstance(th[-1], dict) and th[-1].get("url"):
            return th[-1]["url"]
        for v in node.values():
            r = _largest_thumb(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _largest_thumb(v)
            if r:
                return r
    return ""


def _parse_tworow(it):
    title = _runs_text(it.get("title"))
    subtitle = _runs_text(it.get("subtitle"))
    cover = _largest_thumb(it.get("thumbnailRenderer", {}))
    vid = _find_video_id(it)
    out = {"title": title, "subtitle": subtitle, "cover": cover}
    if _is_explicit(it.get("subtitleBadges")): out["explicit"] = True
    if vid:
        out["id"] = vid
    else:
        bid = _find_browse_id(it)
        if not bid:
            return None
        out["browseId"] = bid
    return out if title else None


def _yt_browse(browse_id, params=None):
    body = {"context": CTX, "browseId": browse_id}
    if params:
        body["params"] = params
    req = urllib.request.Request(YTB_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _find_renderer(node, key):
    if isinstance(node, dict):
        if isinstance(node.get(key), dict):
            return node[key]
        for v in node.values():
            r = _find_renderer(v, key)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_renderer(v, key)
            if r:
                return r
    return None


def _plays_num(text):
    """'3367 M reproducciones' / '1,5 M' / '850 mil' / '3.3B plays' / '8492' -> (valor, precision).
    YT solo da el total REDONDEADO (suma de todas las versiones de la cancion): la precision dice cuanto
    puede diferir el valor real (sirve para aceptar un conteo exacto que caiga dentro)."""
    m = re.match(r"\s*([\d.,\s\u00a0\u202f]*\d)\s*(mil\s*M|MM|B|M|mil|k|K)?", text or "")
    if not m or not re.search(r"\d", m.group(1)):
        return None
    num = re.sub(r"[\s\u00a0\u202f]", "", m.group(1)); unit = (m.group(2) or "").replace(" ", "")
    mult = {"": 1, "k": 1000, "K": 1000, "mil": 1000, "M": 10 ** 6, "milM": 10 ** 9, "MM": 10 ** 9, "B": 10 ** 9}[unit]
    if mult == 1:
        return (int(re.sub(r"[.,]", "", num)), 1)
    if "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".")   # 1.234,5 (es)
    else:
        num = num.replace(",", ".")                    # 1,5 (es) / 3.3 (en)
    dec = len(num.split(".")[1]) if "." in num else 0
    try:
        return (round(float(num) * mult), mult // (10 ** dec) or 1)
    except ValueError:
        return None


def viewcounts(ids):
    """Conteo EXACTO de reproducciones del video (player de YT Music, sin sesion; ~0.4s en paralelo).
    Ojo: es de ESE video; el total de la pagina del artista suma todas las versiones."""
    ids = [i for i in ids if re.fullmatch(r"[\w-]{6,20}", i or "")][:10]

    def one(vid):
        hit = _CACHE.get("vc:" + vid)
        if hit and time.time() - hit[0] < 86400:
            return vid, hit[1]
        try:
            body = {"videoId": vid, "context": CTX}
            req = urllib.request.Request("https://music.youtube.com/youtubei/v1/player?key=" + YTM_KEY,
                                         data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
                                                  "Origin": "https://music.youtube.com"})
            with urllib.request.urlopen(req, timeout=10) as r:
                vc = int((json.loads(r.read().decode()).get("videoDetails") or {}).get("viewCount") or 0)
        except Exception:
            return vid, None
        _CACHE["vc:" + vid] = (time.time(), vc)
        return vid, vc
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        return {v: c for v, c in ex.map(one, ids) if c}


def artist(browse_id):
    return cached("art:" + browse_id, 600, lambda: _artist(browse_id))


def _artist(browse_id):
    data = _yt_browse(browse_id)
    hdr = (data.get("header", {}) or {}).get("musicImmersiveHeaderRenderer", {}) or {}
    name = _runs_text(hdr.get("title"))
    image = _largest_thumb(hdr)
    subtitle = _runs_text(hdr.get("monthlyListenerCount"))   # "89,2 M usuarios mensuales"
    description = _runs_text(hdr.get("description"))
    try:
        sl = data["contents"]["singleColumnBrowseResultsRenderer"]["tabs"][0][
            "tabRenderer"]["content"]["sectionListRenderer"]["contents"]
    except (KeyError, IndexError, TypeError):
        sl = []
    sections = []
    for s in sl:
        if "musicShelfRenderer" in s:
            sh = s["musicShelfRenderer"]
            items = [_parse_item(c["musicResponsiveListItemRenderer"])
                     for c in sh.get("contents", []) if "musicResponsiveListItemRenderer" in c]
            items = [x for x in items if x]
            if items:
                sections.append({"title": _runs_text(sh.get("title")), "kind": "songs", "items": items})
        elif "musicCarouselShelfRenderer" in s:
            cs = s["musicCarouselShelfRenderer"]
            hd = cs.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
            title = _runs_text(hd.get("title"))
            # endpoint "Mas" (catalogo completo) si existe: en el titulo o en moreContentButton
            src = ((hd.get("title", {}).get("runs", [{}]) or [{}])[0].get("navigationEndpoint")
                   or hd.get("moreContentButton", {}).get("buttonRenderer", {}).get("navigationEndpoint"))
            more = None
            be = (src or {}).get("browseEndpoint")
            if be and be.get("browseId"):
                more = {"id": be["browseId"], "params": be.get("params", "")}
            items = [_parse_tworow(c["musicTwoRowItemRenderer"])
                     for c in cs.get("contents", []) if "musicTwoRowItemRenderer" in c]
            items = [x for x in items if x]
            if items:
                sections.append({"title": title, "kind": "items", "items": items, "more": more})
    return {"name": name, "image": image, "subtitle": subtitle, "description": description, "sections": sections}


def artist_list(browse_id, params):
    return cached("alist:%s:%s" % (browse_id, params), 600, lambda: _artist_list(browse_id, params))


def _artist_list(browse_id, params):
    data = _yt_browse(browse_id, params or None)
    items = []
    def walk(x):
        if isinstance(x, dict):
            if "musicTwoRowItemRenderer" in x:
                r = _parse_tworow(x["musicTwoRowItemRenderer"])
                if r:
                    items.append(r)
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    return items


# ---------- album / playlist (browse) ----------
def _parse_col_track(it):
    cols = it.get("flexColumns", [])
    def col_runs(i):
        try:
            return cols[i]["musicResponsiveListItemFlexColumnRenderer"]["text"].get("runs", []) or []
        except (IndexError, KeyError, TypeError):
            return []
    def fx(i):
        return "".join(r.get("text", "") for r in col_runs(i))
    title = fx(0)
    vid = (it.get("playlistItemData", {}) or {}).get("videoId") or _find_video_id(it)
    if not (vid and title):
        return None
    artists, album = [], None
    for r in col_runs(1):
        bid = ((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId", "")
        if bid.startswith("UC"):
            artists.append({"name": r.get("text", ""), "id": bid})
    for r in col_runs(2):
        bid = ((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId", "")
        if (bid.startswith("MPRE") or bid.startswith("VL")) and not album:
            album = {"name": r.get("text", ""), "id": bid}
    dur = ""
    for fc in it.get("fixedColumns", []):
        dur = _runs_text(fc.get("musicResponsiveListItemFixedColumnRenderer", {}).get("text")) or dur
    # sin "type": las pistas de album/playlist son la grabacion correcta y NO deben reemplazarse por un art track
    out = {"index": _runs_text(it.get("index")), "title": title, "artist": fx(1),
           "extra": fx(2), "duration": dur, "id": vid, "cover": _largest_thumb(it.get("thumbnail", {}))}
    if artists: out["artists"] = artists
    if album: out["album"] = album
    if _is_explicit(it): out["explicit"] = True
    return out


def _use_album_audio(hdr, tracks):
    # la lista de audio del album (OLAK5uy_) tiene cada tema como audio (ATV); reemplazamos el id por titulo
    import re
    m = re.search(r'"playlistId":\s*"(OLAK5uy_[\w-]+)"', json.dumps(hdr))
    if not m:
        return
    try:
        ad = _yt_browse("VL" + m.group(1))
    except Exception:
        return
    aitems = []
    _collect_items(ad, aitems)
    by_title = {}
    for x in aitems:
        a = _parse_col_track(x)
        if a:
            by_title.setdefault(a["title"].strip().lower(), a["id"])
    for t in tracks:
        aid = by_title.get(t["title"].strip().lower())
        if aid:
            t["id"] = aid


def collection(browse_id):
    # con sesion el contenido depende de la cuenta (playlists privadas): clave por sesion
    key = _sk("col:" + browse_id) if ytm() else "col:" + browse_id
    return cached(key, 600, lambda: _collection(browse_id))


def _int_or_none(v):   # 116 / "116" / "1.234" / match de regex -> int
    if hasattr(v, "group"):
        v = v.group(1)
    try:
        return int(re.sub(r"[^\d]", "", str(v))) if v is not None and re.search(r"\d", str(v)) else None
    except ValueError:
        return None


def _raw_list_ids(y, browse_id, max_pages=40):
    """shape innertube 2025: videoId ya no viene en playlistItemData (ytmusicapi 1.12 lo parsea None
    en TODAS las pistas) sino en el watchEndpoint de cada fila. Se pagina la lista a mano y se devuelven
    los ids EN ORDEN (None si la fila no es reproducible) para alinear por indice con ytmusicapi."""
    def scan(o, ids, tok):
        if isinstance(o, dict):
            if "musicResponsiveListItemRenderer" in o:
                m, vid = o["musicResponsiveListItemRenderer"], [None]

                def fw(x):
                    if isinstance(x, dict):
                        if "watchEndpoint" in x and x["watchEndpoint"].get("videoId"):
                            vid[0] = vid[0] or x["watchEndpoint"]["videoId"]
                        for v in x.values():
                            fw(v)
                    elif isinstance(x, list):
                        for v in x:
                            fw(v)
                fw(m)
                ids.append(vid[0])
                return
            if "continuationCommand" in o:
                tok[0] = o["continuationCommand"].get("token") or tok[0]
            for v in o.values():
                scan(v, ids, tok)
        elif isinstance(o, list):
            for v in o:
                scan(v, ids, tok)
    ids, pages = [], 0
    r = y._send_request("browse", {"browseId": browse_id})
    while True:
        tok = [None]
        scan(r, ids, tok)
        pages += 1
        if not tok[0] or pages >= max_pages:
            break
        r = y._send_request("browse", {"continuation": tok[0]})
    return ids


def _collection_auth(pid):
    # playlists con sesion: get_playlist autenticado (las PRIVADAS son invisibles al browse anonimo)
    y = ytm()
    if not y:
        return None
    d = y.get_playlist(pid, limit=5000)   # tope de YouTube: 5000 (antes 500 cortaba las grandes)
    raw = d.get("tracks") or []
    if raw and sum(1 for t in raw if t.get("videoId")) < len(raw) / 2:
        # shape nuevo (p.ej. playlists oficiales RDCLAK...): ytmusicapi las da SIN videoId -> 0 canciones
        try:
            ids = _raw_list_ids(y, "VL" + pid, 60)
            if len(ids) >= len(raw):
                for t, vid in zip(raw, ids):
                    t["videoId"] = t.get("videoId") or vid
        except Exception:
            pass
    tracks = []
    for t in raw:
        if not t.get("videoId"):
            continue
        tr = {"index": "", "title": t.get("title", ""), "artist": _yt_artists(t),
              "extra": (t.get("album") or {}).get("name", ""), "duration": t.get("duration") or "",
              "id": t["videoId"], "cover": _yt_thumb(t)}
        arts = [{"name": a.get("name", ""), "id": a.get("id")} for a in (t.get("artists") or []) if a.get("name")]
        if arts:
            tr["artists"] = arts
        if t.get("album") and t["album"].get("id"):
            tr["album"] = {"name": t["album"].get("name", ""), "id": t["album"]["id"]}
        if t.get("isExplicit"):
            tr["explicit"] = True
        tracks.append(tr)
    priv = {"PRIVATE": "Playlist privada", "UNLISTED": "Playlist no listada", "PUBLIC": "Playlist pública"}
    n = d.get("trackCount")
    meta = " • ".join(x for x in [("%s canciones" % n) if n else "", d.get("duration") or ""] if x)
    au = d.get("author") or {}
    return {"kind": "playlist", "title": d.get("title", ""), "subtitle": priv.get(d.get("privacy"), "Playlist"),
            "creator": au.get("name", ""), "creatorId": au.get("id"), "meta": meta, "count": _int_or_none(n),
            "description": d.get("description") or "", "cover": _yt_thumb(d), "tracks": tracks,
            "editable": bool(d.get("owned"))}


def _collection(browse_id):
    kind = "album" if browse_id.startswith("MPRE") else "playlist"
    if kind == "playlist":
        try:
            r = _collection_auth(browse_id[2:] if browse_id.startswith("VL") else browse_id)
            if r is not None:
                return r
        except Exception:
            pass   # sin sesion o fallo: cae al browse anonimo (playlists publicas)
    data = _yt_browse(browse_id)
    hdr = _find_renderer(data, "musicResponsiveHeaderRenderer") or {}
    items = []
    _collect_items(data, items)
    tracks, seen = [], set()
    for it in items:
        t = _parse_col_track(it)
        if t and t["id"] not in seen:
            seen.add(t["id"])
            tracks.append(t)
    if kind == "album":
        _use_album_audio(hdr, tracks)   # reemplaza music videos por el audio (ATV) de la lista de audio del album
    return {
        "kind": kind,
        "title": _runs_text(hdr.get("title")),
        "subtitle": _runs_text(hdr.get("subtitle")),       # "Album . 2013" / "Lista de reproduccion"
        "creator": _runs_text(hdr.get("straplineTextOne")),  # artista o autor
        "creatorId": next((((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId", "")
                           for r in (hdr.get("straplineTextOne", {}) or {}).get("runs", [])
                           if ((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId", "").startswith("UC")), ""),
        "meta": _runs_text(hdr.get("secondSubtitle")),     # "10 canciones . 44 min"
        "count": _int_or_none(re.match(r"\s*([\d.,]+)", _runs_text(hdr.get("secondSubtitle")) or "")),
        "description": _runs_text(hdr.get("description")),
        "cover": _largest_thumb(hdr.get("thumbnail", {})),
        "explicit": _is_explicit(hdr),
        "tracks": tracks,
    }


def _find_all_renderers(node, key, acc):
    if isinstance(node, dict):
        if isinstance(node.get(key), dict):
            acc.append(node[key])
        for v in node.values():
            _find_all_renderers(v, key, acc)
    elif isinstance(node, list):
        for v in node:
            _find_all_renderers(v, key, acc)
    return acc


def album_versions(browse_id):
    return cached("ver:" + browse_id, 1800, lambda: _album_versions(browse_id))


def _header_text(node):
    # texto de cualquier cabecera de carrusel (estructura varia: title.runs en sub-renderers)
    for t in _find_all_renderers(node, "musicCarouselShelfBasicHeaderRenderer", []):
        s = _runs_text(t.get("title"))
        if s:
            return s
    return ""


def _tworow_browse_id(it):
    # el browseId propio del item esta en su navigationEndpoint de nivel superior (no recursivo: cogeria el del artista)
    bid = (((it.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId"))
    if bid:
        return bid
    tc = (it.get("title", {}) or {}).get("runs", [{}])
    if tc:
        bid = (((tc[0].get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId"))
    return bid


def _album_versions(browse_id):
    # "Other versions" del album: carrusel cuyo header menciona "version", items = musicTwoRowItemRenderer (albumes MPRE)
    data = _yt_browse(browse_id)
    out, seen = [], set()
    for sh in _find_all_renderers(data, "musicCarouselShelfRenderer", []):
        if "version" not in _header_text(sh).lower():
            continue
        for c in sh.get("contents", []):
            it = c.get("musicTwoRowItemRenderer")
            if not it:
                continue
            bid = _tworow_browse_id(it)
            if not bid or not bid.startswith("MPRE") or bid == browse_id or bid in seen:
                continue
            seen.add(bid)
            out.append({"browseId": bid, "title": _runs_text(it.get("title")),
                        "subtitle": _runs_text(it.get("subtitle")),
                        "cover": _largest_thumb(it.get("thumbnailRenderer", {})),
                        "explicit": _is_explicit(it.get("subtitleBadges"))})
    return out


def new_releases():
    return cached("newrel", 1800, _new_releases)


def _new_releases():
    # nuevos lanzamientos (albumes/singles): browse FEmusic_new_releases_albums -> carruseles de musicTwoRowItemRenderer
    data = _yt_browse("FEmusic_new_releases_albums")
    out, seen = [], set()
    for sh in _find_all_renderers(data, "musicTwoRowItemRenderer", []):
        bid = _tworow_browse_id(sh)
        title = _runs_text(sh.get("title"))
        if not bid or not title or bid in seen:
            continue
        seen.add(bid)
        out.append({"browseId": bid, "title": title,
                    "subtitle": _runs_text(sh.get("subtitle")),
                    "cover": _largest_thumb(sh.get("thumbnailRenderer", {})),
                    "kind": "albums",
                    "explicit": _is_explicit(sh.get("subtitleBadges"))})
    return out


def radio(video_id):
    return cached("radio:" + video_id, 1800, lambda: _radio(video_id))


def _radio(video_id):
    # radio/recomendaciones de YTM (endpoint next con playlist RDAMVM<id>): canciones similares a la semilla
    body = {"context": CTX, "videoId": video_id, "playlistId": "RDAMVM" + video_id,
            "isAudioOnly": True, "params": "wAEB"}
    req = urllib.request.Request(YTN_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    out, seen = [], {video_id}
    for it in _find_all_renderers(data, "playlistPanelVideoRenderer", []):
        vid = it.get("videoId")
        title = _runs_text(it.get("title"))
        if not vid or not title or vid in seen:
            continue
        seen.add(vid)
        artist = _runs_text(it.get("shortBylineText")) or _runs_text(it.get("longBylineText"))
        artist = artist.split(" • ")[0].strip()
        song = {"id": vid, "title": title, "artist": artist,
                "cover": _largest_thumb(it.get("thumbnail", {})),
                "duration": _runs_text(it.get("lengthText"))}
        arts = []
        for r in (it.get("longBylineText") or {}).get("runs", []):
            b = ((r.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId", "")
            if b.startswith("UC"):
                arts.append({"name": r.get("text", ""), "id": b})
        if arts:
            song["artists"] = arts
        out.append(song)
    return out


def _find_browse_prefix(node, prefix):
    if isinstance(node, dict):
        be = node.get("browseEndpoint")
        if isinstance(be, dict) and str(be.get("browseId", "")).startswith(prefix):
            return be["browseId"]
        for v in node.values():
            r = _find_browse_prefix(v, prefix)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_browse_prefix(v, prefix)
            if r:
                return r
    return None


def related(video_id):
    return cached("rel:" + video_id, 1800, lambda: _related(video_id))


def _related(video_id):
    # pestana "Relacionado" del next (browseId MPTRt...): artistas similares + albumes/playlists recomendados
    body = {"context": CTX, "videoId": video_id, "isAudioOnly": True}
    req = urllib.request.Request(YTN_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    bid = _find_browse_prefix(data, "MPTRt")
    if not bid:
        return {"artists": [], "albums": []}
    rdata = _yt_browse(bid)
    artists, albums, seen = [], [], set()
    for it in _find_all_renderers(rdata, "musicTwoRowItemRenderer", []):
        b = _tworow_browse_id(it)
        if not b or b in seen:
            continue
        seen.add(b)
        item = {"browseId": b, "title": _runs_text(it.get("title")),
                "subtitle": _runs_text(it.get("subtitle")),
                "cover": _largest_thumb(it.get("thumbnailRenderer", {})),
                "explicit": _is_explicit(it.get("subtitleBadges"))}
        if b.startswith("UC"):
            item["kind"] = "artists"; artists.append(item)
        elif b.startswith("MPRE"):
            item["kind"] = "albums"; albums.append(item)
    return {"artists": artists, "albums": albums}


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _lrc_to_lines(lrc):
    # LRC "[mm:ss.xx] texto" -> lineas con tiempo en ms (sin sincronia por palabra)
    import re
    items = []
    for raw in lrc.split("\n"):
        txt = re.sub(r"\[[^\]]*\]", "", raw).strip()
        for m, s in re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", raw):
            items.append({"time": int((int(m) * 60 + float(s)) * 1000), "duration": 0,
                          "text": txt, "syllabus": []})
    items.sort(key=lambda x: x["time"])
    for i in range(len(items) - 1):
        items[i]["duration"] = max(0, items[i + 1]["time"] - items[i]["time"])
    return items


def lyrics(title, artist):
    # letra estable: cache larga (1 dia)
    return cached("l:" + (title + "|" + artist).lower(), 86400, lambda: _lyrics(title, artist))


def _lyrics(title, artist):
    # Estructura unificada: {type: Word|Line|Static, lines:[{time,duration,text,syllabus:[{time,duration,text}]}]}
    from urllib.parse import quote
    t, a = quote(title), quote(artist)
    # 1) KPoe/LyricsPlus: sincronia por palabra (como monochrome). ms en time/duration.
    try:
        k = _get_json("https://lyricsplus.binimum.org/v2/lyrics/get?title=%s&artist=%s&source=%s"
                      % (t, a, quote("apple,lyricsplus,musixmatch-word,musixmatch,spotify")))
        if k.get("lyrics"):
            return {"type": k.get("type", "Line"), "lines": k["lyrics"], "source": "kpoe"}
    except Exception:
        pass
    # 2) LRCLIB: sincronia por linea / texto plano
    d = None
    try:
        d = _get_json("https://lrclib.net/api/get?track_name=%s&artist_name=%s" % (t, a))
    except Exception:
        pass
    if not (d and (d.get("syncedLyrics") or d.get("plainLyrics"))):
        try:
            res = _get_json("https://lrclib.net/api/search?q=%s" % quote((title + " " + artist).strip()))
            d = next((x for x in res if x.get("syncedLyrics")), res[0] if res else None)
        except Exception:
            d = None
    if d and d.get("syncedLyrics"):
        return {"type": "Line", "lines": _lrc_to_lines(d["syncedLyrics"]), "source": "lrclib"}
    if d and d.get("plainLyrics"):
        return {"type": "Static", "source": "lrclib",
                "lines": [{"time": 0, "duration": 0, "text": x, "syllabus": []}
                          for x in d["plainLyrics"].split("\n")]}
    return {"type": "Static", "lines": [], "source": None}


class _SilentLogger:
    # los fallos ya viajan como excepciones; sin esto yt-dlp spamea ERROR en consola por cada intento
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


_YDL_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best", "quiet": True, "no_warnings": True, "skip_download": True,
    "logger": _SilentLogger(),
    # SOLO android: es el cliente fiable (web exige PO token) y pedir dos clientes duplica la
    # latencia (yt-dlp los consulta en serie: ~3.2s vs ~1.5s). Fallback a web en _extract.
    # player_skip: android no necesita la pagina web, los configs ni el JS del player (no firma).
    # Medido en el Tecno: 1.5s -> ~1s por extraccion (y sin la pagina web, una peticion menos)
    "extractor_args": {"youtube": {"player_client": ["android"], "player_skip": ["webpage", "configs", "js"]}},
    "noplaylist": True,
}
# FORMATO (sep-2026): YouTube sirve el audio-solo (140/251) del cliente android SOLO por SABR ->
# bestaudio cae al itag 18 (mp4 360p con audio, ~1.7x mas bytes). android_vr SI lista el 140
# pero sin PO token googlevideo da 403 pasado el primer MB (probado con range). Se queda el 18.

# instancias YoutubeDL reutilizables (crear una por extraccion cuesta ~0.3-0.5s en el Tecno).
# ThreadingHTTPServer abre un hilo por peticion -> pool compartido, una instancia por uso a la vez
import queue as _queue
_ydl_pool = _queue.LifoQueue()


def _extract_with(video_id, cookies_browser=None, clients=None, cookiefile=None):
    url = "https://music.youtube.com/watch?v=" + video_id
    if clients or cookies_browser or cookiefile:   # variantes raras (fallback/age-gate): instancia propia
        opts = dict(_YDL_OPTS)
        if clients:
            opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}
        if cookies_browser:
            opts["cookiesfrombrowser"] = (cookies_browser,)   # cookies del navegador para sortear el age-gate
        if cookiefile:
            # con cuenta los clientes validos (web_music/tv) firman los links con el JS del player:
            # hace falta un runtime JS (node/deno) + el paquete yt-dlp-ejs; android no admite cookies
            opts["cookiefile"] = cookiefile
            opts["js_runtimes"] = {"deno": {}, "node": {}}
        with YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    else:
        try:
            y = _ydl_pool.get_nowait()
        except _queue.Empty:
            y = YoutubeDL(dict(_YDL_OPTS))
        try:
            info = y.extract_info(url, download=False)
        finally:
            _ydl_pool.put(y)
    return (info.get("url") or (info.get("requested_formats") or [{}])[0].get("url")
            or info["formats"][-1]["url"])


def _norm_title(t):
    # sin parentesis/corchetes (Official Video, Audio...) ni signos; minusculas alfanumericas
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", (t or "").lower())
    return re.sub(r"[^a-z0-9]+", "", t)


_ALT_BAD = ("slowed", "sped", "reverb", "8d", "live", "cover", "remix", "mashup",
            "instrumental", "karaoke", "nightcore", "loop", "hour", "fanmade", "concert")


def _dur_secs(s):
    try:
        out = 0
        for v in str(s).split(":"):
            out = out * 60 + int(v)
        return out or None
    except Exception:
        return None


def _alt_ids(video_id):
    # age-gated: busca el MISMO tema en subida alternativa NO restringida (Art Track / audio/lyrics de YouTube)
    try:
        req = urllib.request.Request(
            "https://www.youtube.com/oembed?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D" + video_id + "&format=json",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        title = d.get("title", "")
        author = d.get("author_name", "").replace(" - Topic", "").strip()
        if not title:
            return []
        want = _norm_title(title)
        na = _norm_title(author)

        def sim(t):
            nt = _norm_title(t)
            if na:
                if nt.startswith(na):
                    nt = nt[len(na):]
                elif nt.endswith(na):
                    nt = nt[:-len(na)]
            return difflib.SequenceMatcher(None, want, nt).ratio()

        base = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title).strip()

        def _ytsearch():
            with YoutubeDL({"quiet": True, "no_warnings": True, "logger": _SilentLogger(),
                            "extract_flat": True, "skip_download": True}) as y:
                return y.extract_info("ytsearch12:" + (author + " " + base).strip(), download=False)

        # YT Music (songs) y YouTube normal en paralelo
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_res = ex.submit(search, (base + " " + author).strip(), "songs")
            f_yd = ex.submit(_ytsearch)
            res = f_res.result()
            try:
                yd = f_yd.result()
            except Exception:
                yd = {}
        exp = None   # duracion esperada segun YT Music (el propio id restringido aparece en songs)
        for x in res:
            if x.get("id") == video_id:
                exp = _dur_secs(x.get("duration"))
                break
        cand = [(sim(x.get("title")), x["id"]) for x in res[:8]
                if x.get("id") and x["id"] != video_id and sim(x.get("title")) >= 0.85]
        # YouTube normal: canales lyric/audio suelen tener el tema sin restriccion
        try:
            for e in yd.get("entries") or []:
                t = e.get("title") or ""
                if not e.get("id") or e["id"] == video_id or any(b in t.lower() for b in _ALT_BAD):
                    continue
                s = sim(t)
                dur = e.get("duration")
                if s >= 0.85 and not (exp and dur and abs(dur - exp) > 5):
                    cand.append((s, e["id"]))
        except Exception:
            pass
        seen, out = set(), []
        for s, i in sorted(cand, key=lambda z: -z[0]):
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out[:5]
    except Exception:
        return []


def _session_files():
    """Sesiones de YouTube guardadas (auth/browser*.json). Primero la del dispositivo que pide;
    luego cualquier otra (solo se usan para sacar el audio de temas restringidos/rotos)."""
    own = getattr(_REQ, "bid", "")
    return sorted((os.path.join(AUTH_DIR, n) for n in os.listdir(AUTH_DIR)
                   if re.fullmatch(r"browser(_[0-9a-f]{16})?\.json", n)) if os.path.isdir(AUTH_DIR) else [],
                  key=lambda p: not (own and p.endswith("_%s.json" % own)))


def _session_cookiefiles():
    """Las mismas sesiones en formato Netscape (cookiefile de yt-dlp)."""
    out = []
    for src in _session_files():
        dst = src[:-5] + ".ytdlp.txt"
        try:
            if not os.path.isfile(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
                with open(src, encoding="utf8") as f:
                    ck = {k.lower(): v for k, v in json.load(f).items()}.get("cookie", "")
                rows = ["# Netscape HTTP Cookie File"]
                for p in ck.split(";"):
                    k, _, v = p.strip().partition("=")
                    if k and v:
                        rows.append("\t".join((".youtube.com", "TRUE", "/", "TRUE", "2147483647", k, v)))
                if len(rows) == 1:
                    continue
                with open(dst, "w", encoding="utf8") as f:
                    f.write("\n".join(rows) + "\n")
            out.append(dst)
        except Exception:
            continue
    return out


_RESTRICTED = ("age", "sign in", "error code: 152", "inappropriate", "confirm your")


class _Restricted(RuntimeError):
    """Explicita / restriccion de edad: el cliente android no la da; audio_fetch va por la sesion."""


def _extract(video_id):
    gated = _CACHE.get("gated:" + video_id)   # ya se sabe restringido: sin el intento android (~1s)
    if gated and time.time() - gated[0] < 7 * 86400:
        raise _Restricted("age")
    try:
        return _extract_with(video_id)
    except Exception as e:
        if not any(s in str(e).lower() for s in _RESTRICTED):
            return _extract_with(video_id, clients=["web", "android"])   # raro: android fallo sin age-gate
        _CACHE["gated:" + video_id] = (time.time(), 1)
        raise _Restricted(str(e))


# ---------- via rapida con la sesion: player de web_safari directo + HLS ----------
# Para temas explicitos (android no los da) y para los de itag 18 roto. yt-dlp tarda ~14s en el Tecno
# (pide pagina, configs, otro cliente y lanza node en frio para resolver el reto "n"); aqui: 1 POST
# a /player (~0.7s) + el reto "n" del manifest en un node PERSISTENTE que ya tiene el player
# preprocesado (~0.2s) + segmentos en paralelo + ffmpeg solo remuxa el audio.
_JSCW_JS = r"""
const vm = require("vm"), rl = require("readline").createInterface({ input: process.stdin });
let ready = false;
rl.on("line", (l) => {
  let out;
  try {
    const m = JSON.parse(l);
    if (!ready) { vm.runInThisContext(m.lib + "\nObject.assign(globalThis, lib);\n" + m.core); ready = true; out = { type: "ready" }; }
    else out = globalThis.jsc(m);
  } catch (e) { out = { type: "error", error: String(e && e.stack || e) }; }
  process.stdout.write(JSON.stringify(out) + "\n");
});
"""
_PLAYER = {"t": 0, "pid": None, "url": None, "sts": None, "js": None}
_player_lock = threading.Lock()


def _yt_get(url, ua="Mozilla/5.0", timeout=15):
    if not re.match(r"https://([\w-]+\.)*(youtube\.com|googlevideo\.com)/", url):   # solo YouTube (anti-SSRF)
        raise IOError("host no permitido")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=timeout) as r:
        return r.read()


def _player_info():
    """id del player JS vigente + su signatureTimestamp (se revisa cada 6h; cambia ~semanal)."""
    with _player_lock:
        if time.time() - _PLAYER["t"] < 6 * 3600 and _PLAYER["sts"]:
            return dict(_PLAYER)
        ifr = _yt_get("https://www.youtube.com/iframe_api").decode()
        pid = re.search(r"/s/player/([0-9a-fA-F]{8})/", ifr.replace("\\/", "/")).group(1)
        if pid != _PLAYER["pid"] or not _PLAYER["sts"]:
            url = "https://www.youtube.com/s/player/%s/player_ias.vflset/en_US/base.js" % pid
            js = _yt_get(url, timeout=30).decode()
            _PLAYER.update(pid=pid, url=url, js=js,
                           sts=int(re.search(r"(?:signatureTimestamp|sts)\s*:\s*(\d{5})", js).group(1)))
        _PLAYER["t"] = time.time()
        return dict(_PLAYER)


class _JscWorker:
    """node persistente (sandbox --permission: sin disco ni red) con el solver de yt-dlp-ejs."""
    def __init__(self):
        self.p, self.lock, self.pre = None, threading.Lock(), {}

    def _call(self, o):
        self.p.stdin.write(json.dumps(o) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise IOError("solver cerrado")
        return json.loads(line)

    def _start(self):
        import shutil
        import yt_dlp_ejs.yt.solver as ejs_solver
        node = shutil.which("node")
        if not node:
            raise IOError("sin node")
        path = os.path.join(CACHE_DIR, "jscw.js")
        with open(path, "w", encoding="utf8") as f:
            f.write(_JSCW_JS)
        self.p = subprocess.Popen([node, "--permission", path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf8")
        self._call({"lib": ejs_solver.lib(), "core": ejs_solver.core()})

    def _preprocessed(self, pi):
        pre = self.pre.get(pi["pid"])
        if pre:
            return pre
        fp = os.path.join(CACHE_DIR, "jsc_%s.js" % pi["pid"])   # sobrevive reinicios (~4MB, solo el vigente)
        if os.path.isfile(fp):
            with open(fp, encoding="utf8") as f:
                pre = f.read()
        else:
            js = pi["js"] or _yt_get(pi["url"], timeout=30).decode()
            r = self._call({"type": "player", "player": js, "requests": [], "output_preprocessed": True})
            pre = r.get("preprocessed_player")
            if not pre:
                raise IOError("preprocesado: %s" % str(r)[:200])
            for n in os.listdir(CACHE_DIR):
                if n.startswith("jsc_") and n.endswith(".js"):
                    os.remove(os.path.join(CACHE_DIR, n))
            with open(fp, "w", encoding="utf8") as f:
                f.write(pre)
            _PLAYER["js"] = None   # ya no hace falta en memoria
        self.pre = {pi["pid"]: pre}
        return pre

    def solve_n(self, pi, challenge):
        with self.lock:
            try:
                if not self.p or self.p.poll() is not None:
                    self._start()
                r = self._call({"type": "preprocessed", "preprocessed_player": self._preprocessed(pi),
                                "requests": [{"type": "n", "challenges": [challenge]}]})
                res = (r.get("responses") or [{}])[0]
                if res.get("type") != "result":
                    raise IOError("reto n: %s" % str(r)[:200])
                return res["data"][challenge]
            except Exception:
                if self.p:
                    self.p.kill()
                self.p = None
                raise


_jsc = _JscWorker()


def _jsc_warm():
    # al arrancar el server: player + node + preprocesado listos -> el 1er tema restringido no los paga
    try:
        _jsc.solve_n(_player_info(), "aaaaaaaaaaaaaaaa")
    except Exception:
        pass


def _web_hls_url(video_id, session_file):
    """hlsManifestUrl del cliente web_safari con la sesion (explicitas incluidas), reto n resuelto."""
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        client = dict(INNERTUBE_CLIENTS["web_safari"]["INNERTUBE_CONTEXT"]["client"])   # version al dia con yt-dlp
    except Exception:
        client = {"clientName": "WEB", "clientVersion": "2.20250925.01.00", "userAgent":
                  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.5 Safari/605.1.15,gzip(gfe)"}
    with open(session_file, encoding="utf8") as f:
        h = {k.lower(): v for k, v in json.load(f).items()}
    ck = h.get("cookie", "")
    m = re.search(r"(?:^|;\s*)(?:__Secure-3PAPISID|SAPISID)=([^;]+)", ck)
    if not m:
        raise IOError("sesion sin SAPISID")
    pi = _player_info()
    origin, ts, ua = "https://www.youtube.com", str(int(time.time())), client.get("userAgent") or "Mozilla/5.0"
    auth = "SAPISIDHASH %s_%s" % (ts, hashlib.sha1(("%s %s %s" % (ts, m.group(1), origin)).encode()).hexdigest())
    body = {"context": {"client": dict(client, hl="es")}, "videoId": video_id, "contentCheckOk": True, "racyCheckOk": True,
            "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS",
                                                           "signatureTimestamp": pi["sts"]}}}
    hdrs = {"Content-Type": "application/json", "Cookie": ck, "Authorization": auth, "X-Origin": origin,
            "Origin": origin, "X-Goog-AuthUser": "0", "User-Agent": ua, "X-Youtube-Client-Name": "1",
            "X-Youtube-Client-Version": client.get("clientVersion", "")}
    if h.get("x-goog-visitor-id"):
        hdrs["X-Goog-Visitor-Id"] = h["x-goog-visitor-id"]
    req = urllib.request.Request(origin + "/youtubei/v1/player?prettyPrint=false", data=json.dumps(body).encode(), headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    hls = (d.get("streamingData") or {}).get("hlsManifestUrl")
    if not hls:
        raise IOError("sin HLS: %s" % (d.get("playabilityStatus") or {}).get("status"))
    n = re.search(r"/n/([^/]+)/", urlparse(hls).path)
    if n:
        hls = hls.replace("/n/%s/" % n.group(1), "/n/%s/" % _jsc.solve_n(pi, n.group(1)), 1)
    return hls, ua, time.time() + _ad_wait(d)


def _ad_wait(d):
    """Cuenta sin Premium: con anuncio previo googlevideo da 403 a los segmentos hasta que el anuncio
    se podria saltar (~5s desde la peticion al player). Mismo calculo que yt-dlp (available_at)."""
    rends = []
    for p in d.get("adPlacements") or []:
        c = (p.get("adPlacementRenderer") or {})
        if ((c.get("config") or {}).get("adPlacementConfig") or {}).get("kind") == "AD_PLACEMENT_KIND_START":
            rends.append((c.get("renderer") or {}).get("instreamVideoAdRenderer"))
    for sl in d.get("adSlots") or []:
        r = sl.get("adSlotRenderer") or {}
        if (r.get("adSlotMetadata") or {}).get("triggerEvent") != "SLOT_TRIGGER_EVENT_BEFORE_CONTENT":
            continue
        rc = ((((r.get("fulfillmentContent") or {}).get("fulfilledLayout") or {})
               .get("playerBytesAdLayoutRenderer") or {}).get("renderingContent") or {})
        rends.append(rc.get("instreamVideoAdRenderer"))
        for lay in (rc.get("playerBytesSequentialLayoutRenderer") or {}).get("sequentialLayouts") or []:
            rends.append(((lay.get("playerBytesAdLayoutRenderer") or {}).get("renderingContent") or {})
                         .get("instreamVideoAdRenderer"))
    wait = 0.0
    for r in rends:
        if not isinstance(r, dict):
            continue
        if r.get("skipOffsetMilliseconds") is not None:
            wait += float(r["skipOffsetMilliseconds"]) / 1000
        else:
            try:
                wait += int(parse_qs(r.get("playerVars") or "")["length_seconds"][-1])
            except Exception:
                pass
    return min(wait, 60)


def _hls_download(master, ua, available_at, dst):
    """Variante 360p (AAC-LC 128k, el mismo audio del itag 18) -> segmentos en paralelo -> ffmpeg
    se queda con el audio sin recodificar. Las playlists se pueden pedir ya; los segmentos, cuando
    pase el anuncio (available_at)."""
    lines = _yt_get(master, ua).decode().splitlines()
    vars_ = [l for l in lines if l.startswith("http")]
    var = next((v for it in ("93", "94", "92", "91", "95") for v in vars_ if "/itag/%s/" % it in v), None)
    if not var:
        raise IOError("sin variante")
    segs = [urljoin(var, l) for l in _yt_get(var, ua).decode().splitlines() if l and not l.startswith("#")]
    if not segs or len(segs) > 2000:
        raise IOError("playlist rara")
    wait = available_at - time.time()
    if wait > 0:
        time.sleep(wait + 0.5)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        data = list(ex.map(lambda u: _yt_get(u, ua, timeout=30), segs))
    if sum(map(len, data)) > 4 * AUDIO_MAX_MB * 1024 * 1024:
        raise IOError("demasiado grande")
    ts = dst + ".ts"
    try:
        with open(ts, "wb") as f:
            for b in data:
                f.write(b)
        del data
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", ts, "-vn", "-c:a", "copy",
                            "-movflags", "+faststart", "-f", "mp4", dst], capture_output=True, timeout=120)
        if r.returncode:
            raise IOError("ffmpeg: %s" % r.stderr[-200:])
    finally:
        try:
            os.remove(ts)
        except OSError:
            pass


def song_id(title, artist):
    # devuelve el videoId del "art track" (audio) para reemplazar un music video
    res = search((title + " " + artist).strip(), "songs")
    return res[0]["id"] if res else ""


# ---------- cache de audio: links (con su caducidad real) + bytes en disco ----------
# Links de googlevideo: persistidos en cache/urls.json con el `expire` que trae la propia URL (~6h)
# menos un margen; sobreviven a reinicios del server. Bytes: cache/audio/<id>.mp4 (LRU por mtime,
# tope en MB) -> una cancion repetida no toca YouTube (ni extraccion ni descarga).
CACHE_DIR = os.path.join(BASE, "cache")
AUDIO_DIR = os.path.join(CACHE_DIR, "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)
_URLS_FILE = os.path.join(CACHE_DIR, "urls.json")
_URL_MARGIN = 900   # no servir links a <15min de caducar: la descarga podria cortarse a mitad
AUDIO_MAX_MB = 60   # una cancion en itag 18 ~4-10MB; mas que esto no es musica (o es abuso)
AUDIO_CACHE_MB = int(os.environ.get("BLYATT_AUDIO_CACHE_MB") or (2048 if os.environ.get("BLYATT_HOST") else 1024))
_urls_lock = threading.Lock()
_id_locks, _id_locks_lock = {}, threading.Lock()


def _id_lock(kind, vid):   # un lock por (tipo, cancion): play + precarga de la misma no duplican trabajo
    with _id_locks_lock:
        return _id_locks.setdefault((kind, vid), threading.Lock())


def _url_expiry(u):
    try:
        return int(parse_qs(urlparse(u).query)["expire"][0])
    except Exception:
        return int(time.time()) + 3 * 3600


def _load_urls():
    try:
        with open(_URLS_FILE, encoding="utf8") as f:
            now = time.time()
            return {k: tuple(v) for k, v in json.load(f).items() if v[1] - _URL_MARGIN > now}
    except Exception:
        return {}


_URLS = _load_urls()


def _save_urls():
    with _urls_lock:
        now = time.time()
        for k in [k for k, v in _URLS.items() if v[1] - _URL_MARGIN <= now]:
            _URLS.pop(k, None)
        data = dict(_URLS)
    try:
        tmp = _URLS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf8") as f:
            json.dump(data, f)
        os.replace(tmp, _URLS_FILE)
    except Exception:
        pass


def audio_url(video_id, fresh=False):
    # fresh=True: la URL guardada fallo (403/410: caducada o de otra IP) -> se purga y se re-extrae
    if fresh:
        _URLS.pop(video_id, None)
    hit = _URLS.get(video_id)
    if hit and hit[1] - _URL_MARGIN > time.time():
        return hit[0]
    with _id_lock("url", video_id):
        hit = _URLS.get(video_id)
        if hit and hit[1] - _URL_MARGIN > time.time():
            return hit[0]
        u = _extract(video_id)
        _URLS[video_id] = (u, _url_expiry(u))
    _save_urls()
    return u


def _audio_path(vid):
    return os.path.join(AUDIO_DIR, re.sub(r"[^\w-]", "_", vid) + ".mp4")


def _audio_evict():
    try:
        files = []
        for n in os.listdir(AUDIO_DIR):
            fp = os.path.join(AUDIO_DIR, n)
            if n.endswith(".part"):
                if time.time() - os.path.getmtime(fp) > 3600:   # restos de descargas cortadas
                    os.remove(fp)
                continue
            st = os.stat(fp)
            files.append((st.st_mtime, st.st_size, fp))
        total, cap = sum(f[1] for f in files), AUDIO_CACHE_MB * 1024 * 1024
        for _, size, fp in sorted(files):   # mas antiguo (menos usado) primero
            if total <= cap:
                break
            os.remove(fp)
            total -= size
    except Exception:
        pass


def _mp4_ok(path):
    # un mp4 reproducible tiene datos (mdat/moof) ademas de la cabecera (moov): los "stubs" no
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if f.read(8)[4:] != b"ftyp":
                return True   # no es mp4 (webm...): no se juzga
            f.seek(0)
            while True:
                pos = f.tell()
                h = f.read(8)
                if len(h) < 8:
                    return False
                n, t = struct.unpack(">I4s", h)
                if n == 1:
                    n = struct.unpack(">Q", f.read(8))[0] - 8
                if t == b"moof":
                    return True
                if t == b"mdat":   # el stub declara un mdat de MB pero el archivo acaba en su cabecera
                    return size - pos > 4096 and (n == 0 or size - pos >= n)
                if n < 8:
                    return False
                f.seek(n - 8, 1)
    except Exception:
        return False


def _is_stub(u):
    # sep-2026: en algunos temas YouTube sirve el itag 18 SOLO con la cabecera (~130KB para 4 min,
    # sin audio: ese audio ya solo va por SABR) -> "Unable to decode audio data" en el cliente.
    # El link trae clen (bytes) y dur (s): un mp4 real ronda 16KB/s
    try:
        q = parse_qs(urlparse(u).query)
        clen, dur = int(q["clen"][0]), float(q["dur"][0])
        return dur > 0 and clen / dur < 4000
    except Exception:
        return False


def _hls_audio(video_id, dst):
    """Audio del tema via la sesion guardada: primero la via rapida (~5s), si falla yt-dlp (~14s)."""
    for sf in _session_files()[:2]:
        try:
            _hls_download(*_web_hls_url(video_id, sf), dst)
            if _mp4_ok(dst):
                return True
        except Exception:
            pass
    for cf in _session_cookiefiles()[:2]:
        try:
            opts = dict(_YDL_OPTS)
            opts.update(format="93/94/92/91/95/96", cookiefile=cf, js_runtimes={"deno": {}, "node": {}},
                        extractor_args={"youtube": {"player_client": ["web_safari"]}})
            with YoutubeDL(opts) as y:   # www: con music.youtube.com yt-dlp consulta ademas web_music (+2.5s)
                m3u8 = y.extract_info("https://www.youtube.com/watch?v=" + video_id, download=False)["url"]
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", m3u8, "-vn", "-c:a", "copy",
                                "-movflags", "+faststart", "-f", "mp4", dst], capture_output=True, timeout=240)
            if r.returncode == 0 and _mp4_ok(dst):
                return True
        except Exception:
            pass
    return False


def _stub_fallback(vid, part):
    """Tema restringido o con itag 18 roto. Devuelve un link alternativo, o None si ya dejo el
    audio en `part` (via HLS con la sesion)."""
    if _hls_audio(vid, part):
        return None
    alts = _alt_ids(vid)[:4]   # sin sesion/node/ffmpeg: otra subida del mismo tema, en paralelo
    if alts:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(alts)) as ex:
            futs = [ex.submit(_extract_with, a) for a in alts]
            for f in futs:
                try:
                    u = f.result()
                    if not _is_stub(u):
                        return u
                except Exception:
                    continue
    for br in ("edge", "chrome", "firefox", "brave"):   # escritorio: sesion del navegador del usuario
        try:
            u = _extract_with(vid, br)
            if not _is_stub(u):
                return u
        except Exception:
            continue
    raise IOError("audio no disponible")


def audio_fetch(vid, on_start=None, on_chunk=None):
    """Baja el audio a la cache de disco reenviando cada trozo a on_chunk (streaming al cliente).
    on_start(ctype, length) se llama antes del primer trozo. Si el link guardado da 403/410 se
    re-extrae una vez. Si el cliente se va a mitad (on_chunk lanza) se termina igual la descarga:
    la siguiente vez sale de disco."""
    fp = _audio_path(vid)
    part = fp + ".%d.part" % threading.get_ident()
    for attempt in range(2):
        try:
            src = audio_url(vid, fresh=attempt > 0)
        except _Restricted:
            src = None
        if src is None or _is_stub(src):
            src = _stub_fallback(vid, part)
            if src is None:   # HLS ya en disco: se sirve desde ahi
                os.replace(part, fp)
                _audio_evict()
                if on_start:
                    on_start("audio/mp4", os.path.getsize(fp))
                if on_chunk:
                    with open(fp, "rb") as f:
                        for chunk in iter(lambda: f.read(262144), b""):
                            on_chunk(chunk)
                return fp
        try:
            up = urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"}), timeout=30)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (403, 410) or attempt:
                raise
    with up:
        ctype = up.headers.get("Content-Type", "audio/mp4")
        clen = up.headers.get("Content-Length")
        if clen and int(clen) > AUDIO_MAX_MB * 1024 * 1024:
            raise IOError("demasiado grande")
        client_ok = True
        if on_start:
            on_start(ctype, clen)
        got = 0
        try:
            with open(part, "wb") as f:
                while True:
                    chunk = up.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if got > AUDIO_MAX_MB * 1024 * 1024:   # sin Content-Length fiable: corta igual
                        raise IOError("demasiado grande")
                    if client_ok and on_chunk:
                        try:
                            on_chunk(chunk)
                        except Exception:
                            client_ok = False
            if clen and got != int(clen):
                raise IOError("descarga incompleta")
            if not _mp4_ok(part):   # cabecera sin audio: no se cachea (el cliente no la puede decodificar)
                raise IOError("audio sin datos")
        except Exception:
            try:
                os.remove(part)   # incompleta: no se cachea
            except OSError:
                pass
            raise
    os.replace(part, fp)
    _audio_evict()
    return fp


# ---------- migracion de origen (ngrok -> blyatt.stream) ----------
# El WebView guarda localStorage y la cookie `bid` POR ORIGEN: al cambiar la URL de la app todo
# quedaria en el origen viejo. La app visita el origen viejo, sube su localStorage (/migrate/push,
# que ademas conoce su bid por la cookie) y vuelve al nuevo con un token de un solo uso;
# /migrate/pull devuelve los datos y fija la MISMA bid en el origen nuevo (misma sesion de Google).
_MIGR = {}


def migrate_push(bid, ls):
    now = time.time()
    for k in [k for k, v in _MIGR.items() if now - v[0] > 600]:
        _MIGR.pop(k, None)
    while len(_MIGR) >= 20:   # tope de memoria: fuera la mas antigua
        _MIGR.pop(min(_MIGR, key=lambda k: _MIGR[k][0]), None)
    tok = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    _MIGR[tok] = (now, bid, ls if isinstance(ls, dict) else {})
    return tok


def migrate_pull(tok):
    v = _MIGR.pop(tok or "", None)
    if not v or time.time() - v[0] > 600:
        return None
    return {"bid": v[1], "ls": v[2]}


_prewarm_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
_prewarm_seen = {}


def prewarm(ids):
    """Precarga en segundo plano (link + bytes a disco) de las proximas canciones de la cola.
    No gasta datos del tunel (server <-> googlevideo). Dedup 10min por id."""
    now = time.time()
    for vid in ids[:4]:
        if not re.fullmatch(r"[\w-]{6,20}", vid or "") or os.path.isfile(_audio_path(vid)):
            continue
        if now - _prewarm_seen.get(vid, 0) < 600:
            continue
        _prewarm_seen[vid] = now

        def job(v=vid):
            with _id_lock("dl", v):
                if not os.path.isfile(_audio_path(v)):
                    try:
                        audio_fetch(v)
                    except Exception:
                        pass
        _prewarm_pool.submit(job)


# ---------- login con Google (headers de music.youtube.com via ytmusicapi) ----------
# OAuth device-flow descartado: YouTube rechaza tokens de cliente TV en la API interna de
# YT Music (HTTP 400 en todos los endpoints desde finales de 2024). Los headers del navegador
# son la via soportada por ytmusicapi y la cookie dura anios.
AUTH_DIR = os.path.join(BASE, "auth")
BROWSER_FILE = os.path.join(AUTH_DIR, "browser.json")
WEBLOGIN = None   # main.py (pywebview) inyecta aqui el launcher de la ventana de login de Google
WEBLOGOUT = None  # main.py: borra las cookies de Google del perfil WebView2 (para poder cambiar de cuenta)

# --- sesiones por dispositivo (modo servidor) ---
# Cada dispositivo lleva una cookie "bid"; si existe auth/browser_<bid>.json esa es SU sesion.
# Sin sesion propia cae a BROWSER_FILE (la sesion "de la casa", que escribe el weblogin de escritorio).
SERVER_MODE = bool(os.environ.get("BLYATT_HOST"))
_REQ = threading.local()
_ytm_by = {}   # ruta de archivo -> instancia YTMusic


def _bid_path(bid):
    return os.path.join(AUTH_DIR, "browser_%s.json" % bid)


def _bid_file():
    b = getattr(_REQ, "bid", "")
    if b and re.fullmatch(r"[0-9a-f]{16}", b):
        p = _bid_path(b)
        if os.path.exists(p):
            return p
    if SERVER_MODE:
        # sin sesion propia = invitado: en servidor NO se cae a browser.json (esa cuenta es solo del escritorio)
        return _bid_path(b or "anon")
    return BROWSER_FILE


def _purge_session(file):
    # todo lo cacheado con esa sesion (ytlib, acct, sess_alive, col:<playlist privada>...): prefijo "<archivo>|"
    pre = file + "|"
    for k in [k for k in list(_CACHE) if isinstance(k, str) and k.startswith(pre)]:
        _CACHE.pop(k, None)


def _sk(key, file=None):
    # clave de cache ligada a la sesion efectiva (los datos con sesion no se comparten entre cuentas)
    return "%s|%s" % (file or _bid_file(), key)


def _probe_session(cookie_header, user_agent):
    # browse crudo con SAPISIDHASH propio: devuelve (logged_in, visitorData reales de la sesion)
    import hashlib
    try:
        sapisid = next(p.split("=", 1)[1] for p in cookie_header.split("; ") if p.startswith("SAPISID="))
    except StopIteration:
        return False, ""
    origin = "https://music.youtube.com"
    ts = str(int(time.time()))
    sash = hashlib.sha1((ts + " " + sapisid + " " + origin).encode()).hexdigest()
    body = {"context": CTX, "browseId": "FEmusic_liked_playlists"}
    req = urllib.request.Request(
        "https://music.youtube.com/youtubei/v1/browse?prettyPrint=false",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Cookie": cookie_header, "Origin": origin,
                 "X-Origin": origin, "X-Goog-AuthUser": "0",
                 "Authorization": "SAPISIDHASH %s_%s" % (ts, sash), "User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except Exception:
        return False, ""
    rc = d.get("responseContext", {})
    logged = any(p.get("key") == "logged_in" and p.get("value") == "1"
                 for s in rc.get("serviceTrackingParams", []) for p in s.get("params", []))
    return logged, rc.get("visitorData", "")


# ---------- vincular dispositivo (iPhone / navegador: Safari no puede capturar la cookie de Google) ----------
# Un dispositivo con sesion propia genera un codigo de 6 cifras (10 min, un solo uso); el otro lo
# introduce y recibe una COPIA de esa sesion en su propio archivo (cerrar sesion en uno no afecta al otro).
_LINKS, _link_lock, _link_fails = {}, threading.Lock(), []


def link_new():
    src = _bid_file()
    if not SERVER_MODE or not os.path.isfile(src) or src == BROWSER_FILE:
        return {"error": "Este dispositivo no tiene una sesión propia que compartir"}
    import secrets
    now = time.time()
    with _link_lock:
        for c in [c for c, v in _LINKS.items() if v[0] < now or v[1] == src]:   # caducados + el anterior de este
            _LINKS.pop(c, None)
        code = "%06d" % secrets.randbelow(10 ** 6)
        while code in _LINKS:
            code = "%06d" % secrets.randbelow(10 ** 6)
        _LINKS[code] = (now + 600, src)
    return {"code": code, "expires": 600}


def link_use(code):
    code, now = re.sub(r"\D", "", str(code or "")), time.time()
    b = getattr(_REQ, "bid", "")
    if not SERVER_MODE or not re.fullmatch(r"[0-9a-f]{16}", b or ""):
        return {"error": "No disponible"}
    with _link_lock:
        _link_fails[:] = [t for t in _link_fails if now - t < 600]
        if len(_link_fails) >= 20:   # 1e6 codigos, 20 fallos/10min en TOTAL: fuerza bruta inviable
            return {"error": "Demasiados intentos. Espera unos minutos."}
        v = _LINKS.pop(code, None) if len(code) == 6 else None
        if not v or v[0] < now or not os.path.isfile(v[1]):
            _link_fails.append(now)
            return {"error": "Código inválido o caducado"}
    tgt = _bid_path(b)
    if os.path.abspath(tgt) != os.path.abspath(v[1]):
        import shutil
        shutil.copyfile(v[1], tgt)
        _ytm_by.pop(tgt, None)
        _purge_session(tgt)
    return {"ok": True}


def save_browser_cookie(cookie_header, user_agent=None, target=None):
    # cookies frescas extraidas del perfil WebView2 -> browser.json; devuelve True si la sesion vale.
    # CRITICO: guardar x-goog-visitor-id REAL de la sesion; si falta, ytmusicapi inyecta uno anonimo
    # (get_visitor_id sin auth) y YouTube trata todo como deslogueado (logged_in=0).
    ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    logged = visitor = None
    for _ in range(3):   # YT responde logged_in=0 esporadicamente para cookies validas: reintentar
        logged, visitor = _probe_session(cookie_header, ua)
        if logged:
            break
        time.sleep(1.5)
    if not logged:
        return False
    os.makedirs(AUTH_DIR, exist_ok=True)
    hdrs = {
        # "authorization" DEBE existir: ytmusicapi.is_browser exige {authorization, cookie} o trata
        # el archivo como oauth. El valor real (SAPISIDHASH) lo recalcula en cada request; este placeholder solo marca el tipo.
        "authorization": "SAPISIDHASH",
        "cookie": cookie_header,
        "user-agent": ua,
        "origin": "https://music.youtube.com",
        "x-origin": "https://music.youtube.com",
        "x-goog-authuser": "0",
        "accept": "*/*",
        "accept-language": "es-419,es;q=0.9",
        "content-type": "application/json",
    }
    if visitor:
        hdrs["x-goog-visitor-id"] = visitor
    tgt = target or BROWSER_FILE
    with open(tgt, "w", encoding="utf8") as f:
        json.dump(hdrs, f, indent=1)
    _ytm_by.pop(tgt, None)
    _purge_session(tgt)   # puede ser OTRA cuenta en el mismo archivo: nada de la anterior debe sobrevivir
    return _session_alive()


def ytm():
    f = _bid_file()
    y = _ytm_by.get(f)
    if y:
        return y
    if not (YTMusic and os.path.exists(f)):
        return None
    try:
        _ytm_by[f] = YTMusic(f, language="es")
    except Exception:
        return None
    return _ytm_by.get(f)


def _session_alive():
    # las cookies pueden morir cuando Google las rota: el flag logged_in del responseContext no miente
    y = ytm()
    if not y:
        return False
    try:
        r = y._send_request("browse", {"browseId": "FEmusic_liked_playlists"})
        for s in r.get("responseContext", {}).get("serviceTrackingParams", []):
            for p in s.get("params", []):
                if p.get("key") == "logged_in":
                    return p.get("value") == "1"
    except Exception:
        pass
    return False


def _account_info():
    y = ytm()
    try:
        i = y.get_account_info()
        return {"name": i.get("accountName", ""), "photo": i.get("accountPhotoUrl", "")}
    except Exception:   # el parser de account_menu se rompe a veces con cuentas sin canal
        return {"name": "", "photo": ""}


def auth_status():
    f = _bid_file()
    has_file = os.path.exists(f)
    alive = bool(has_file and cached(_sk("sess_alive"), 300, _session_alive))
    st = {"logged_in": alive, "stale": has_file and not alive, "available": YTMusic is not None,
          # propia del dispositivo (o escritorio, donde el global ES del usuario); false = compartida de la casa
          "own": (not SERVER_MODE) or f != BROWSER_FILE}
    if alive:
        st.update(cached(_sk("acct"), 3600, _account_info))
    return st


def _headers_from_any(raw):
    # acepta: headers crudos (Firefox), "Copy as cURL" cmd/bash (Chromium) y "Copy as fetch"
    t = raw.strip()
    if t.lower().startswith("curl") or re.search(r"-H\s+['\"]", t):
        s = re.sub(r"\^\s*\n", "\n", t).replace("^", "")   # des-escapa la variante cmd (^ de continuacion/escape)
        hdrs = [m[1] for m in re.findall(r"-H\s+(['\"])(.*?)\1", s, re.S)]
        mb = re.search(r"(?:-b|--cookie)\s+(['\"])(.*?)\1", s, re.S)
        if mb and not any(h.lower().startswith("cookie:") for h in hdrs):
            hdrs.append("cookie: " + mb.group(2))
        if hdrs:
            return "\n".join(hdrs)
    if "fetch(" in t and '"headers"' in t:
        m = re.search(r'"headers"\s*:\s*(\{.*?\})', t, re.S)
        if m:
            try:
                h = json.loads(m.group(1))
                return "\n".join("%s: %s" % (k, v) for k, v in h.items())
            except Exception:
                pass
    return t


def auth_set_headers(raw):
    if not YTMusic:
        return {"error": "ytmusicapi no instalado"}
    os.makedirs(AUTH_DIR, exist_ok=True)
    b = getattr(_REQ, "bid", "")
    # en modo servidor cada dispositivo escribe SU archivo; en escritorio se mantiene el global
    tgt = _bid_path(b) if (SERVER_MODE and b) else BROWSER_FILE
    try:
        ytmusicapi.setup(filepath=tgt, headers_raw=_headers_from_any(raw))
        _ytm_by.pop(tgt, None)
        _CACHE.pop(_sk("sess_alive", tgt), None)
        if not _session_alive():   # valida contra el flag logged_in real, no solo formato
            raise ValueError("YouTube no reconoce la sesión (headers viejos o de una petición sin login)")
        _CACHE.pop(_sk("ytlib", tgt), None)
        return {"ok": True}
    except Exception as e:
        _ytm_by.pop(tgt, None)
        try:
            os.remove(tgt)
        except OSError:
            pass
        return {"error": "Headers inválidos o sesión caducada: " + str(e)[:120]}


def auth_logout():
    f = _bid_file()
    if SERVER_MODE and f == BROWSER_FILE:
        # dispositivo sin sesion propia: no puede borrar la sesion de la casa (compartida)
        return {"ok": True, "shared": True}
    _ytm_by.pop(f, None)
    _purge_session(f)
    if not SERVER_MODE and WEBLOGOUT:
        WEBLOGOUT()
    try:
        os.remove(f)
    except OSError:
        pass
    return {"ok": True}


def lib_save(bid, kind, save):
    # guardar/quitar album, playlist o artista en la biblioteca de YT Music del usuario
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    if kind == "artists":
        (y.subscribe_artists if save else y.unsubscribe_artists)([bid])
    elif kind == "albums":
        pid = (y.get_album(bid) or {}).get("audioPlaylistId")   # rate_playlist necesita la lista OLAK5uy_, no el MPRE
        if not pid:
            return {"error": "Álbum sin audioPlaylistId"}
        y.rate_playlist(pid, "LIKE" if save else "INDIFFERENT")
    else:
        y.rate_playlist(bid[2:] if bid.startswith("VL") else bid, "LIKE" if save else "INDIFFERENT")
    _CACHE.pop(_sk("ytlib"), None)
    return {"ok": True}


def pl_create(title, desc, privacy):
    # crea playlist REAL en la cuenta de YT Music (privacy: PRIVATE|UNLISTED|PUBLIC)
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    pid = y.create_playlist(title, desc or "", privacy_status=privacy or "PRIVATE")
    if not isinstance(pid, str):
        return {"error": "YT Music rechazó la creación"}
    _CACHE.pop(_sk("ytlib"), None)
    return {"id": pid}


def pl_edit(pid, title, desc, privacy):
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    y.edit_playlist(pid, title=title or None, description=desc if desc is not None else None,
                    privacyStatus=privacy or None)
    _CACHE.pop(_sk("ytlib"), None)
    _CACHE.pop(_sk("col:VL" + pid), None)
    return {"ok": True}


def pl_delete(pid):
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    y.delete_playlist(pid)
    _CACHE.pop(_sk("ytlib"), None)
    return {"ok": True}


def pl_add(pid, vid):
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    r = y.add_playlist_items(pid, [vid], duplicates=False)
    st = (r or {}).get("status", "")
    if "SUCCEEDED" not in str(st):
        return {"error": "Ya está en la playlist" if "FAILED" in str(st) else "No se pudo añadir"}
    _CACHE.pop(_sk("ytlib"), None)
    _CACHE.pop(_sk("col:VL" + pid), None)
    out = {"ok": True}
    try:
        # consistencia eventual de YT (~1-3s): espera a que la pista aparezca antes de leer la cover fresca
        for _ in range(4):
            d = y.get_playlist(pid, limit=50) or {}
            if any(t.get("videoId") == vid for t in d.get("tracks") or []):
                break
            time.sleep(1)
        th = d.get("thumbnails") or []
        if th:
            out["cover"] = th[-1]["url"]
        _CACHE.pop(_sk("col:VL" + pid), None)   # re-purga: el fetch de arriba pudo repoblar via /collection concurrente
    except Exception:
        pass
    return out


def _find_key(node, key):
    # busqueda recursiva en respuestas innertube (shape no documentado)
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def pl_collab(pid, on):
    # activa/desactiva colaboracion; al activar YT devuelve joinCollaborationToken -> enlace de invitacion
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    r = y.edit_playlist(pid, collaboration=on)
    _CACHE.pop(_sk("ytlib"), None)
    _CACHE.pop(_sk("col:VL" + pid), None)
    if not on:
        return {"ok": True}
    tok = _find_key(r, "joinCollaborationToken") if isinstance(r, (dict, list)) else None
    if not tok:
        return {"error": "YT no devolvió token de invitación (¿playlist privada? Colaboración requiere No listada o Pública)"}
    return {"ok": True, "link": "https://music.youtube.com/playlist?list=%s&jct=%s" % (pid, tok)}


def pl_join(link):
    # unirse a playlist colaborativa con enlace de invitacion (list=<pid>&jct=<token>)
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    qs = parse_qs(urlparse(link).query)
    pid, tok = qs.get("list", [""])[0], qs.get("jct", [""])[0]
    if not pid or not tok:
        return {"error": "Enlace inválido: falta list= o jct="}
    y.join_collaborative_playlist(pid, tok)
    _CACHE.pop(_sk("ytlib"), None)
    d = {}
    try:
        d = y.get_playlist(pid, limit=1) or {}
    except Exception:
        pass
    th = d.get("thumbnails") or []
    return {"ok": True, "id": pid, "title": d.get("title", "Playlist colaborativa"),
            "cover": th[-1]["url"] if th else "",
            "creator": (d.get("author") or {}).get("name", "")}


def pl_remove(pid, vid):
    # remove necesita setVideoId: lo buscamos en la playlist
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    d = y.get_playlist(pid, limit=None)
    hit = next((t for t in d.get("tracks") or [] if t.get("videoId") == vid and t.get("setVideoId")), None)
    if not hit:
        return {"error": "Pista no encontrada en la playlist"}
    y.remove_playlist_items(pid, [{"videoId": vid, "setVideoId": hit["setVideoId"]}])
    _CACHE.pop(_sk("ytlib"), None)
    _CACHE.pop(_sk("col:VL" + pid), None)
    return {"ok": True}


# ---------- importacion desde Spotify ----------
# Sin API key. main.py abre el web player, el usuario inicia sesion y extraemos la cookie sp_dc
# (via get_cookies, API oficial de pywebview). Con sp_dc generamos el access token nosotros:
# Spotify exige un TOTP (time-based) en /api/token -> lo calculamos en Python (algoritmo del web
# player). sp_dc dura ~1 anio -> se persiste y regeneramos el token bajo demanda: LOGIN PERSISTENTE,
# sin re-loguear entre reinicios. El token de acceso vive ~1h y se refresca solo desde sp_dc.
SPOT_API = "https://api.spotify.com/v1/"
SPOT_FILE = os.path.join(AUTH_DIR, "spotify.json")
# El secreto TOTP del web player ROTA cada pocos dias -> se carga de una fuente remota mantenida
# (auto-actualiza) con fallback local. Formato {version: cifrado[]}. Se elige la version mas alta.
SPOT_SECRETS_URL = "https://raw.githubusercontent.com/xyloflake/spot-secrets-go/refs/heads/main/secrets/secretDict.json"
SPOT_SECRET_FALLBACK = {"61": [44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75, 76, 94, 102, 43, 69, 49, 120, 118, 80, 64, 78]}
SPOTLOGIN = None
_spot_tok = ""
_spot_exp = 0.0   # epoch de caducidad del access token (~1h)
_spot_dc = ""     # cookie sp_dc (LEGACY: Spotify devuelve 429 permanente a tokens web-player en /v1)
_spot_cid = ""    # client_id de la app Spotify del usuario (OAuth PKCE, metodo exportify)
_spot_rt = ""     # refresh_token OAuth: renueva el access token sin re-login
_spot_pkce = {}   # verifier/state del flujo authorize en curso
_imp_prog = {"active": False, "label": "", "done": 0, "total": 0}
_imp_cancel = False   # /spot/cancel lo activa; los bucles de import lo consultan y abortan

# OAuth PKCE (metodo exportify): el DEV registra UNA app en developer.spotify.com (gratis) y pone
# su Client ID aqui -> los usuarios solo inician sesion, exactamente como exportify (que tambien
# lleva el client_id de su dev en el codigo). Redirect URI de la app: http://127.0.0.1:8000/spot/callback
SPOT_CLIENT_ID = ""
SPOT_REDIRECT = "http://127.0.0.1:8000/spot/callback"
SPOT_SCOPES = "user-library-read user-follow-read playlist-read-private playlist-read-collaborative"


def _spot_load():
    global _spot_tok, _spot_exp, _spot_cid, _spot_rt
    _spot_cid = SPOT_CLIENT_ID
    try:
        with open(SPOT_FILE, encoding="utf-8") as f:
            d = json.load(f)
        _spot_cid = d.get("client_id") or SPOT_CLIENT_ID
        _spot_rt = d.get("refresh_token", "") or ""
        # tokens legacy (sp_dc/web-player) se IGNORAN: Spotify les da 429 permanente en /v1
        if _spot_rt and d.get("token") and float(d.get("exp", 0)) > time.time() + 60:
            _spot_tok, _spot_exp = d["token"], float(d["exp"])
    except Exception:
        pass


def _spot_save():
    try:
        os.makedirs(AUTH_DIR, exist_ok=True)
        with open(SPOT_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": _spot_tok, "exp": _spot_exp,
                       "client_id": _spot_cid, "refresh_token": _spot_rt}, f)
    except Exception:
        pass


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _spot_token_post(data):
    req = urllib.request.Request("https://accounts.spotify.com/api/token",
                                 data=urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def spot_set_client(cid):
    global _spot_cid
    cid = (cid or "").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", cid):
        return {"error": "Client ID inválido (32 caracteres hexadecimales)"}
    _spot_cid = cid
    _spot_save()
    return {"ok": True}


def spot_login():
    # abre el navegador del sistema en accounts.spotify.com (misma UX que exportify: si ya hay
    # sesion en el navegador, autoriza con un click). El callback vuelve a este server local.
    if not _spot_cid:
        return {"error": "need_client"}
    verifier = _b64url(os.urandom(48))
    _spot_pkce.update(verifier=verifier, state=_b64url(os.urandom(12)))
    url = "https://accounts.spotify.com/authorize?" + urlencode({
        "client_id": _spot_cid, "response_type": "code", "redirect_uri": SPOT_REDIRECT,
        "scope": SPOT_SCOPES, "state": _spot_pkce["state"],
        "code_challenge_method": "S256",
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest())})
    webbrowser.open(url)
    return {"ok": True}


def spot_callback(code, state):
    global _spot_tok, _spot_exp, _spot_rt
    if not code or state != _spot_pkce.get("state"):
        return False, "Estado OAuth inválido: reintenta desde Blyatt"
    try:
        d = _spot_token_post({"grant_type": "authorization_code", "code": code,
                              "redirect_uri": SPOT_REDIRECT, "client_id": _spot_cid,
                              "code_verifier": _spot_pkce.get("verifier", "")})
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        _spot_dbg("callback HTTP %s: %s" % (e.code, body))
        return False, "Spotify rechazó el código (HTTP %s). Verifica que la Redirect URI de tu app sea exactamente %s" % (e.code, SPOT_REDIRECT)
    except Exception as e:
        return False, str(e)[:150]
    if not d.get("access_token"):
        return False, "Sin access_token en la respuesta"
    _spot_tok = d["access_token"]
    _spot_exp = time.time() + int(d.get("expires_in", 3600)) - 60
    _spot_rt = d.get("refresh_token", "") or _spot_rt
    _spot_save()
    _spot_dbg("OAUTH OK -> sesion Spotify lista (PKCE)")
    return True, ""


def _spot_refresh():
    global _spot_tok, _spot_exp, _spot_rt
    if not (_spot_rt and _spot_cid):
        return False
    try:
        d = _spot_token_post({"grant_type": "refresh_token", "refresh_token": _spot_rt,
                              "client_id": _spot_cid})
        if d.get("access_token"):
            _spot_tok = d["access_token"]
            _spot_exp = time.time() + int(d.get("expires_in", 3600)) - 60
            _spot_rt = d.get("refresh_token", "") or _spot_rt
            _spot_save()
            return True
    except Exception as e:
        _spot_dbg("refresh err: " + str(e)[:100])
    return False


def _spot_secrets():
    # {version: cifrado[]} desde la fuente remota (cache 1h) con fallback local si falla la descarga
    def fetch():
        req = urllib.request.Request(SPOT_SECRETS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
        return d if isinstance(d, dict) and d else SPOT_SECRET_FALLBACK
    try:
        return cached("spot_secrets", 3600, fetch) or SPOT_SECRET_FALLBACK
    except Exception:
        return SPOT_SECRET_FALLBACK


def _spot_totp(ts, cipher):
    # TOTP del web player de Spotify: XOR del cifrado -> clave HMAC-SHA1, 6 digitos, ventana 30s
    key = "".join(str(e ^ ((i % 33) + 9)) for i, e in enumerate(cipher)).encode()
    h = hmac.new(key, struct.pack(">Q", int(ts) // 30), hashlib.sha1).digest()
    o = h[-1] & 15
    return "%06d" % ((int.from_bytes(h[o:o + 4], "big") & 0x7fffffff) % 1000000)


def _spot_cookie_get(url, sp_dc):
    req = urllib.request.Request(url, headers={
        "Cookie": "sp_dc=" + sp_dc, "User-Agent": "Mozilla/5.0",
        "Accept": "application/json", "App-Platform": "WebPlayer",
        "Referer": "https://open.spotify.com/",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _spot_token_from_dc(sp_dc):
    # genera un access token a partir de sp_dc + TOTP (mismo flujo que el web player)
    secrets = _spot_secrets()
    ver = max(secrets.keys(), key=lambda x: int(x))   # version mas alta = la vigente
    cipher = secrets[ver]
    try:
        st = _spot_cookie_get("https://open.spotify.com/server-time", sp_dc)
        ts = int((st or {}).get("serverTime") or time.time())
    except Exception:
        ts = int(time.time())
    otp = _spot_totp(ts, cipher)
    url = ("https://open.spotify.com/api/token?reason=transport&productType=web-player"
           "&totp=%s&totpServer=%s&totpVer=%s" % (otp, otp, ver))
    d = _spot_cookie_get(url, sp_dc)
    return (d or {}).get("accessToken", ""), (d or {}).get("accessTokenExpirationTimestampMs", 0)


def _spot_dbg(msg):
    # diagnostico del flujo sp_dc -> token (auth/spot_debug.log). Se puede borrar cuando funcione.
    try:
        os.makedirs(AUTH_DIR, exist_ok=True)
        with open(os.path.join(AUTH_DIR, "spot_debug.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def spot_set_dc(sp_dc):
    # main.py entrega la cookie sp_dc extraida de la ventana de login; genera y valida el token
    global _spot_tok, _spot_exp, _spot_dc
    if not sp_dc:
        _spot_dbg("sp_dc vacio (login aun no completo)")
        return False
    try:
        tok, exp_ms = _spot_token_from_dc(sp_dc)
        if not tok:
            _spot_dbg("api/token no devolvio accessToken")
            return False
        # token de api/token ya es valido; NO validar con /me (el poll cada 2s spammea -> 429)
        _spot_tok = tok
        _spot_exp = (exp_ms / 1000.0) if exp_ms else (time.time() + 3300)
        _spot_dc = sp_dc
        _spot_save()
        _spot_dbg("TOKEN OK -> sesion Spotify lista")
        return True
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:120]
        except Exception:
            pass
        _spot_dbg("HTTP %s en flujo token: %s" % (e.code, body))
    except Exception as e:
        _spot_dbg("err: " + str(e)[:120])
    return False


def _spot_ensure():
    # asegura un access token OAuth vivo (refresh_token -> login persistente sin re-autorizar).
    # Tokens derivados de sp_dc NO cuentan: Spotify les devuelve 429 permanente en /v1.
    if not _spot_rt:
        return False
    if _spot_tok and _spot_exp > time.time() + 30:
        return True
    return _spot_refresh()


def _spot_req(path, tok=None):
    # con reintento en 429 (Retry-After) al estilo exportify: biblioteca completa sin fallar por rate limit
    url = SPOT_API + path if not path.startswith("http") else path
    for _ in range(3):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + (tok or _spot_tok),
            "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 429:
                try:
                    e.spot_body = e.read().decode("utf-8", "replace")[:300]   # spot_lib lo usa para explicar el error
                except Exception:
                    e.spot_body = ""
                _spot_dbg("req %s HTTP %s: %s" % (path[:30], e.code, e.spot_body[:100]))
                raise
            ra = int(e.headers.get("Retry-After") or 2)
            time.sleep(min(ra, 60) + 3)   # esperar el RA COMPLETO: insistir antes lo re-arma a 60s
    raise RuntimeError("Spotify rate limit persistente")


def spot_status():
    ok = _spot_ensure()
    _spot_dbg("status logged_in=%s tok=%s" % (ok, bool(_spot_tok)))
    return {"logged_in": bool(ok), "name": ""}   # sin /me (429 lo cuelga)


def _spot_all(path, key=None, cap=100000):
    out, url = [], path
    while url and len(out) < cap and not _imp_cancel:
        d = _spot_req(url)
        if key:
            d = d.get(key) or {}
        out += d.get("items") or []
        url = d.get("next") or None
    return out[:cap]


def _spot_img(x):
    im = (x or {}).get("images") or []
    return im[-1]["url"] if im else ""


def spot_lib():
    if not _spot_ensure():
        return {"error": "Sin sesión de Spotify"}
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fpl = ex.submit(_spot_all, "me/playlists?limit=50")
        fal = ex.submit(_spot_all, "me/albums?limit=50")
        far = ex.submit(_spot_all, "me/following?type=artist&limit=50", "artists")
        fli = ex.submit(_spot_req, "me/tracks?limit=1")
    out = {"playlists": [], "albums": [], "artists": [], "liked": 0}
    errs = []
    for f in (fpl, fal, far, fli):
        e = f.exception()
        if e is not None:
            errs.append(e)
    if errs and len(errs) == 4:
        body = ""
        body = getattr(errs[0], "spot_body", "") or str(errs[0])
        if "premium" in body.lower():
            # 2026: Spotify exige Premium al DUENO de la app de desarrollador para usar su API
            return {"error": "premium", "detail": "Spotify exige que la cuenta dueña de la app de desarrollador tenga Premium"}
        return {"error": "Spotify rechazó la petición: " + (body or repr(errs[0]))[:160]}
    try:
        # shape dev-mode 2025: el total viaja en "items.total" (ya no existe "tracks" en me/playlists)
        out["playlists"] = [{"id": p["id"], "title": p.get("name") or "(sin nombre)", "cover": _spot_img(p),
                             "count": ((p.get("tracks") or p.get("items") or {}).get("total", 0)),
                             "public": bool(p.get("public")),
                             "owner": (p.get("owner") or {}).get("display_name", "")}
                            for p in fpl.result() if p and p.get("id")]
    except Exception as e:
        _spot_dbg("lib playlists err: " + repr(e)[:150])
    try:
        out["albums"] = [{"id": a["album"]["id"], "title": a["album"].get("name", ""),
                          "cover": _spot_img(a["album"]),
                          "count": a["album"].get("total_tracks", 0),
                          "artist": ", ".join(x.get("name", "") for x in a["album"].get("artists") or [])}
                         for a in fal.result() if a and a.get("album", {}).get("id")]
    except Exception:
        pass
    try:
        out["artists"] = [{"id": a["id"], "title": a.get("name", ""), "cover": _spot_img(a)}
                          for a in far.result() if a and a.get("id")]
    except Exception:
        pass
    try:
        out["liked"] = (fli.result() or {}).get("total", 0)
    except Exception:
        pass
    return out


def _mnorm(t):
    # comparable: sin acentos, sin "(feat. X)" / "- Remastered 2011" / "(Radio Edit)", solo alfanumerico
    import unicodedata
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[\(\[](feat|ft|with|con|prod)\.?\s[^\)\]]*[\)\]]", " ", t)
    t = re.sub(r"\s-\s.*\b(remaster(ed)?|version|edit|mix|mono|stereo|live|en vivo|acoustic)\b.*$", " ", t)
    t = re.sub(r"[\(\[][^\)\]]*\b(remaster(ed)?|version|edit|mono|stereo)\b[^\)\]]*[\)\]]", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _spot_match(y, title, artists, dur_ms=None):
    """Mejor match de YT Music para una pista de Spotify/CSV. Usa la busqueda PROPIA de la app: desde
    2026 ytmusicapi (search con sesion) devuelve videoId=None y el album como artista -> 0 matches.
    Puntua titulo (similitud normalizada) + artista principal + duracion si el CSV la trae.
    Devuelve {id,title,artist,cover,duration} o None (el resolutor manual muestra lo elegido)."""
    want_t = _mnorm(title)
    want_a = [_mnorm(x) for x in re.split(r"\s*[,;&]\s*|\s+(?:feat\.?|ft\.?|x)\s+", artists or "") if x.strip()]
    first = (artists or "").split(",")[0].split(";")[0].strip()

    def score(r):
        rt = _mnorm(r.get("title"))
        sc = difflib.SequenceMatcher(None, want_t, rt).ratio()
        if want_t and (want_t in rt or rt in want_t):
            sc = max(sc, .85)
        names = [_mnorm(a.get("name", "")) for a in r.get("artists") or []] or [_mnorm(r.get("artist"))]
        if want_a and any(w and (w == n or w in n or n in w) for w in want_a for n in names if n):
            sc += .3
        elif want_a:
            sc -= .35   # mismo titulo de OTRO artista: no (el resolutor deja elegirlo a mano)
        rd, cd = _dur_secs(r.get("duration")), (dur_ms or 0) / 1000.0
        if rd and cd:
            sc += .15 if abs(rd - cd) <= 4 else (-.25 if abs(rd - cd) > 20 else 0)
        return sc

    best, bs = None, 0.0
    for q in ((title + " " + first).strip(), (title + " " + (artists or "")).strip(), title):
        try:
            res = [r for r in (search(q, "songs") or [])[:6] if r.get("id")]
        except Exception:
            res = []
        for r in res:
            sc = score(r)
            if sc > bs:
                bs, best = sc, r
        if bs >= 1.0:   # titulo + artista claros: no hace falta otra busqueda
            break
    if not best or bs < .75:
        return None
    return {"id": best["id"], "title": best.get("title", ""), "artist": best.get("artist", ""),
            "cover": best.get("cover", ""), "duration": best.get("duration") or ""}


def _spot_pl_items(sid):
    # /playlists/{id}/tracks devuelve 403 a apps dev-mode nuevas (restriccion Spotify 2025).
    # El meta endpoint SI trae las pistas embebidas de playlists PROPIAS, en shape nuevo:
    # top-level "items" = paging cuyas entradas llevan "item" (no "track"). Las ajenas vienen sin pistas.
    d = _spot_req("playlists/" + sid)
    pg = d.get("tracks") or d.get("items") or {}
    items = list(pg.get("items") or [])
    nxt = pg.get("next")
    while nxt:
        try:
            d = _spot_req(nxt)
        except urllib.error.HTTPError as e:
            _spot_dbg("paginacion de playlist %s bloqueada (HTTP %s) tras %d pistas" % (sid, e.code, len(items)))
            break
        pg = d.get("tracks") or d.get("items") or d
        items += pg.get("items") or []
        nxt = pg.get("next")
    if not items:
        raise RuntimeError("Spotify oculta las pistas de esta playlist a apps en modo desarrollo (solo playlists creadas por ti son legibles)")
    return items


def _spot_tracks_of(kind, sid):
    # biblioteca COMPLETA: paginacion sin tope (429 manejado en _spot_req)
    if kind == "liked":
        items = _spot_all("me/tracks?limit=50")
    elif kind == "playlist":
        items = _spot_pl_items(sid)
    else:
        return []
    out = []
    for it in items:
        t = (it or {}).get("track") or (it or {}).get("item") or {}
        if t.get("name"):
            out.append((t["name"], ", ".join(a.get("name", "") for a in t.get("artists") or [])))
    return out


def _match_all(y, pairs):
    # matching en paralelo conservando el ORDEN ORIGINAL; actualiza _imp_prog.
    # Devuelve el reporte completo: [{title, artists, match: {...}|None}] (el resolutor lo pinta entero)
    rep = [None] * len(pairs)
    def work(i):
        if _imp_cancel:
            _imp_prog["done"] += 1
            return
        rep[i] = _spot_match(y, pairs[i][0], pairs[i][1], pairs[i][2] if len(pairs[i]) > 2 else None)
        _imp_prog["done"] += 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, range(len(pairs))))
    return [{"title": pairs[i][0], "artists": pairs[i][1], "match": rep[i]} for i in range(len(pairs))]   # (pares con 3er elem = ms)


def _import_liked(y, vids):
    """Da "me gusta" a vids en la cuenta. YT descarta rate_song en silencio bajo cuota (rafagas
    pierden ~90%). Estrategia: pasadas convergentes — likear lo pendiente (3 workers, pausa corta),
    esperar a que YT materialice (consistencia eventual, espera creciente), re-verificar contra la
    cuenta y repetir SOLO lo que falta. Re-import reanuda gratis (skip de ya-likeados)."""
    def _liked_now():
        try:
            return {t.get("videoId") for t in (y.get_liked_songs(limit=None).get("tracks") or [])
                    if t.get("videoId")}
        except Exception:
            return None
    have = _liked_now() or set()
    todo = [v for v in vids if v not in have]
    after = have

    def like(v):
        if not _imp_cancel:
            try:
                y.rate_song(v, "LIKE")
            except Exception:
                pass
            _imp_prog["done"] += 1
            time.sleep(0.1)
    for pase in range(1, 7):
        if not todo or _imp_cancel:
            break
        _imp_prog.update(done=0, total=len(todo))
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(like, todo))
        time.sleep(min(5 * pase, 20))   # deja materializar antes de verificar
        chk = _liked_now()
        if chk is None:
            break
        after = chk
        remaining = [v for v in todo if v not in after]
        if len(remaining) == len(todo):   # pase sin avance: cuota dura, enfriar y reintentar
            time.sleep(40)
            after = _liked_now() or after
            remaining = [v for v in todo if v not in after]
            if len(remaining) == len(todo):
                break
        todo = remaining
    # purga ytlib de TODAS las sesiones del mismo usuario: el proximo sync de cualquier
    # dispositivo (movil incluido) ve el estado final, no una foto a mitad de import
    for k in [k for k in list(_CACHE) if str(k).endswith("|ytlib")]:
        _CACHE.pop(k, None)
    return sum(1 for v in vids if v in after or v in have)


def spot_import(kind, sid, title, cover=""):
    global _imp_cancel
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    if not _spot_ensure():
        return {"error": "Sin sesión de Spotify"}
    _imp_cancel = False
    _imp_prog.update(active=True, label=title or kind, done=0, total=0)
    try:
        if kind == "artist":
            bid = next((r.get("browseId") for r in search(title, "artists") or [] if r.get("browseId")), None)
            if not bid:
                return {"error": "No encontrado en YT Music: " + title}
            y.subscribe_artists([bid])
            _CACHE.pop(_sk("ytlib"), None)
            return {"ok": True, "added": 1, "missed": []}
        if kind == "album":
            bid = next((r.get("browseId") for r in search(title, "albums") or [] if r.get("browseId")), None)
            if not bid:
                return {"error": "No encontrado en YT Music: " + title}
            lib_save(bid, "albums", True)
            return {"ok": True, "added": 1, "missed": []}
        pairs = _spot_tracks_of(kind, sid)
        if not pairs:
            return {"error": "Sin canciones que importar"}
        _imp_prog.update(total=len(pairs))
        report = _match_all(y, pairs)
        if _imp_cancel:
            return {"error": "Importación cancelada"}
        vids = [t["match"]["id"] for t in report if t["match"]]
        pid = None
        added = len(vids)
        if kind == "liked":
            added = _import_liked(y, vids)
        else:
            if not vids:
                return {"error": "Ninguna canción encontrada en YT Music"}
            pid = _yt_make_playlist(y, title or "Importada de Spotify", vids)
            if cover:
                try:
                    img, mime = _cover_from_url(cover)
                    pl_set_cover(pid, img, mime)   # portada original de Spotify tambien en YT
                except Exception as e:
                    _spot_dbg("cover upload err: " + str(e)[:100])
        _CACHE.pop(_sk("ytlib"), None)
        return {"ok": True, "added": added, "missed": len(report) - len(vids),
                "tracks": report, "playlist_id": pid,
                "title": title or ("Me gusta" if kind == "liked" else "Importada de Spotify")}
    finally:
        _imp_prog.update(active=False)


def pl_set_cover(pid, img, mime):
    # portada CUSTOM real en YT Music (protocolo del web player, ytmusicapi PR #866 no mergeado):
    # 1) handshake resumable a playlist_image_upload -> X-Goog-Upload-URL, 2) binario -> blobId,
    # 3) browse/edit_playlist con ACTION_SET_CUSTOM_THUMBNAIL. Reusa sesion/SAPISIDHASH de ytmusicapi.
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    up = "https://music.youtube.com/playlist_image_upload/playlist_custom_thumbnail"
    h = dict(y.headers)
    h.update({"Content-Type": "text/plain; charset=utf-8", "origin": "https://music.youtube.com",
              "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(len(img)),
              "X-Goog-Upload-Protocol": "resumable", "x-goog-authuser": "0"})
    r = y._session.post(up, data="playlistId=" + pid, headers=h, cookies=y.cookies, proxies=y.proxies)
    real = r.headers.get("X-Goog-Upload-URL")
    if not real:
        return {"error": "YT rechazó el upload de portada (HTTP %s)" % r.status_code}
    h2 = dict(y.headers)
    h2.update({"Content-Type": mime or "image/jpeg", "X-Goog-Upload-Command": "upload, finalize",
               "X-Goog-Upload-Offset": "0", "x-goog-authuser": "0"})
    r2 = y._session.post(real, data=img, headers=h2, cookies=y.cookies, proxies=y.proxies)
    try:
        d = r2.json()
    except Exception:
        d = {}
    blob = d.get("playlistScottyEncryptedBlobId") or d.get("encryptedBlobId")
    if not blob:
        return {"error": "Upload sin blobId (HTTP %s): %s" % (r2.status_code, str(d)[:120])}
    y._send_request("browse/edit_playlist", {"playlistId": pid, "actions": [{
        "action": "ACTION_SET_CUSTOM_THUMBNAIL",
        "addedCustomThumbnail": {
            "imageKey": {"type": "PLAYLIST_IMAGE_TYPE_CUSTOM_THUMBNAIL", "name": "studio_square_thumbnail"},
            "playlistScottyEncryptedBlobId": blob}}]})
    _CACHE.pop(_sk("ytlib"), None)
    _CACHE.pop(_sk("col:VL" + pid), None)
    return {"ok": True}


# ---------- descargas de URLs que vienen del cliente (anti-SSRF) ----------
# El server es publico (blyatt.stream): una URL controlada por el visitante no debe poder hacer que el
# telefono pida paginas de la red de casa (router, PC...). Solo https, solo dominios exactos permitidos
# (sufijo con punto: "x-ytimg.com" NO pasa), IP resuelta publica, sin redirecciones y con tope de tamano.
IMG_HOSTS = ("ytimg.com", "ggpht.com", "googleusercontent.com")
COVER_HOSTS = IMG_HOSTS + ("scdn.co", "spotifycdn.com")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None   # una redireccion podria apuntar a la red interna


_safe_opener = urllib.request.build_opener(_NoRedirect)


def _host_allowed(host, allowed):
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowed)


def _public_host(host):
    try:
        for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:   # privada, loopback, link-local, reservada...
                return False
        return True
    except Exception:
        return False


def safe_fetch(url, allowed, timeout=15, max_bytes=8 * 1024 * 1024):
    """(bytes, content-type) de una URL externa validada; ValueError si no es aceptable."""
    pu = urlparse(url or "")
    if pu.scheme != "https" or not _host_allowed(pu.hostname, allowed) or not _public_host(pu.hostname):
        raise ValueError("URL no permitida")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _safe_opener.open(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError("respuesta %s" % r.status)
        data = r.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("demasiado grande")
        return data, r.headers.get_content_type() or "image/jpeg"


def _cover_from_url(url):
    # descarga una portada externa (p.ej. i.scdn.co de Spotify) para subirla a YT
    return safe_fetch(url, COVER_HOSTS, timeout=20)


def imp_replace(pid, vids):
    # reescribe la playlist con la lista final del resolutor manual, en el ORDEN ORIGINAL de la
    # fuente (add_playlist_items solo apendiza: para respetar orden hay que vaciar y re-anadir)
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    if not (pid and vids):
        return {"error": "Sin datos"}
    pl = y.get_playlist(pid, limit=None) or {}
    old = [{"videoId": t.get("videoId"), "setVideoId": t.get("setVideoId")}
           for t in pl.get("tracks") or [] if t.get("setVideoId")]
    if old:
        y.remove_playlist_items(pid, old)
    for i in range(0, len(vids), 100):
        y.add_playlist_items(pid, vids[i:i + 100], duplicates=True)
    _CACHE.pop(_sk("ytlib"), None)
    _CACHE.pop(_sk("col:VL" + pid), None)
    return {"ok": True, "count": len(vids)}


def _yt_make_playlist(y, title, vids):
    # playlists grandes: crear con el primer lote y anadir el resto en tandas de 100 (YT rechaza creates enormes)
    pid = y.create_playlist(title, "Importada", privacy_status="PRIVATE", video_ids=vids[:100])
    if not isinstance(pid, str):
        raise RuntimeError("YT Music rechazó la creación")
    for i in range(100, len(vids), 100):
        try:
            y.add_playlist_items(pid, vids[i:i + 100], duplicates=True)
        except Exception:
            pass
    return pid


def import_csv(body):
    # CSV de exportify (o compatible): {title, tracks:[[titulo, artistas], ...]} -> playlist en YT Music
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    try:
        d = json.loads(body)
        title = (d.get("title") or "Importada").strip()
        liked = bool(d.get("liked"))
        pairs = [(str(t[0]), str(t[1]) if len(t) > 1 else "", _int_or_none(t[2]) if len(t) > 2 else None)
                 for t in d.get("tracks") or [] if t and t[0]][:10000]
    except Exception:
        return {"error": "CSV inválido"}
    if not pairs:
        return {"error": "Sin canciones en el CSV"}
    global _imp_cancel
    _imp_cancel = False
    _imp_prog.update(active=True, label=title, done=0, total=len(pairs))
    try:
        report = _match_all(y, pairs)
        if _imp_cancel:
            return {"error": "Importación cancelada"}
        vids = [t["match"]["id"] for t in report if t["match"]]
        if not vids:
            return {"error": "Ninguna canción encontrada en YT Music"}
        if liked:
            added = _import_liked(y, vids)
            _CACHE.pop(_sk("ytlib"), None)
            return {"ok": True, "added": added, "missed": len(report) - len(vids),
                    "tracks": report, "playlist_id": None, "title": "Me gusta"}
        pid = _yt_make_playlist(y, title, vids)
        _CACHE.pop(_sk("ytlib"), None)
        return {"ok": True, "added": len(vids), "missed": len(report) - len(vids),
                "tracks": report, "playlist_id": pid, "title": title}
    finally:
        _imp_prog.update(active=False)


def rate_song(video_id, like):
    # like en la app -> me gusta en la cuenta de YT Music del usuario
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    y.rate_song(video_id, "LIKE" if like else "INDIFFERENT")
    _CACHE.pop(_sk("ytlib"), None)   # la biblioteca cambio: proximo /ytlib re-fetch
    return {"ok": True}


def _yt_thumb(x):
    th = x.get("thumbnails") or []
    return th[-1]["url"] if th else ""


def _yt_artists(x):
    return ", ".join(a.get("name", "") for a in (x.get("artists") or []) if a.get("name"))


def yt_library(parts=None):
    # biblioteca real de YT Music del usuario: me gusta + playlists + artistas + albumes (en paralelo).
    # parts (carga por etapas tras iniciar sesion): "lib" = playlists+artistas+albumes (~1s en el Tecno);
    # "head" = solo los ~100 me gusta mas recientes (siempre marcado parcial); "liked" = todos (~10s).
    # Sin parts = todo, como siempre.
    want = {"liked", "playlists", "artists", "albums"}
    if parts:
        want = set()
        if "lib" in parts:
            want |= {"playlists", "artists", "albums"}
        want |= {p for p in parts if p in ("liked", "head")}
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}

    # nombre de cuenta ANTES del executor: _sk/_REQ son del hilo de la request, no de los workers
    acct_name = ""
    try:
        acct_name = (cached(_sk("acct"), 3600, _account_info) or {}).get("name", "")
    except Exception:
        pass

    truncated = []

    def liked(head=False):
        # En el server la sesion suele recibir el shape nuevo (videoId en null): el paginado crudo del
        # rescate corre EN PARALELO con ytmusicapi en vez de despues (Tecno: ~20s -> ~10s)
        rescue = None
        if SERVER_MODE:
            rescue = concurrent.futures.ThreadPoolExecutor(max_workers=1).submit(_raw_list_ids, y, "VLLM", 1 if head else 40)
        d = None
        if head:
            d = y.get_liked_songs(limit=100)
            truncated.append("liked")   # solo la cabeza: nunca reconciliar bajas con ella
        else:
            for _ in range(3):   # TODOS los likes (paginado por continuations; fallan esporadicamente)
                try:
                    d = y.get_liked_songs(limit=None)
                    break
                except Exception:
                    time.sleep(1)
            if d is None:
                d = y.get_liked_songs(limit=200)   # ultimo recurso: mejor 200 que nada
                truncated.append("liked")   # lista INCOMPLETA: el frontend no debe reconciliar bajas con ella
        tracks = d.get("tracks") or []
        if tracks and sum(1 for t in tracks if t.get("videoId")) < len(tracks) / 2:
            try:
                ids = rescue.result() if rescue else _raw_list_ids(y, "VLLM", 1 if head else 40)
                if head:
                    tracks = tracks[:len(ids)]   # la 1a pagina cruda trae ~100: se alinea el prefijo
                if len(ids) == len(tracks):
                    for t, vid in zip(tracks, ids):
                        t["videoId"] = t.get("videoId") or vid
                else:
                    truncated.append("liked")   # no se pudo alinear: no reconciliar bajas
            except Exception:
                truncated.append("liked")
        out = []
        for t in tracks:
            if not t.get("videoId"):
                continue
            s = {"id": t["videoId"], "title": t.get("title", ""), "artist": _yt_artists(t),
                 "cover": _yt_thumb(t), "duration": t.get("duration") or ""}
            arts = [{"name": a.get("name", ""), "id": a.get("id")}
                    for a in (t.get("artists") or []) if a.get("name")]
            if arts:
                s["artists"] = arts
            if t.get("album") and t["album"].get("id"):
                s["album"] = {"name": t["album"].get("name", ""), "id": t["album"]["id"]}
            if t.get("isExplicit"):
                s["explicit"] = True
            out.append(s)
        return out

    def playlists():
        acct = acct_name
        out = []
        for p in y.get_library_playlists(limit=50):
            pid = p.get("playlistId", "")
            if not pid or pid in ("LM", "SE"):   # LM = me gusta (seccion propia), SE = episodios
                continue
            n = p.get("count")
            aus = p.get("author") or []
            if isinstance(aus, dict):
                aus = [aus]
            # propia si no expone autor o el autor es la cuenta; ajenas guardadas -> editable False
            own = not aus or any((a.get("name") or "") == acct for a in aus if isinstance(a, dict))
            creator = ", ".join(a.get("name", "") for a in aus if isinstance(a, dict) and a.get("name")) or acct
            out.append({"browseId": "VL" + pid, "kind": "playlists", "title": p.get("title", ""),
                        "subtitle": ("%s canciones" % n) if n else "Playlist", "cover": _yt_thumb(p),
                        "editable": own, "creator": creator})
        return out

    def artists():
        return [{"browseId": a["browseId"], "kind": "artists", "title": a.get("artist", ""),
                 "subtitle": a.get("subscribers", "") or "Artista", "cover": _yt_thumb(a)}
                for a in y.get_library_subscriptions(limit=50) if a.get("browseId")]

    def albums():
        return [{"browseId": a["browseId"], "kind": "albums", "title": a.get("title", ""),
                 "subtitle": _yt_artists(a) or str(a.get("year", "")), "cover": _yt_thumb(a),
                 "creator": _yt_artists(a)}
                for a in y.get_library_albums(limit=50) if a.get("browseId")]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        jobs = [(k, f) for k, f in (("liked", liked), ("playlists", playlists), ("artists", artists),
                                    ("albums", albums)) if k in want]
        if "head" in want and "liked" not in want:
            jobs.append(("liked", lambda: liked(True)))
        futs = {k: ex.submit(f) for k, f in jobs}
        out = {}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception:
                out[k] = []
                out.setdefault("_partial", []).append(k)   # señal: NO cachear; el frontend sabe QUE fallo
    for k in truncated:
        if k not in out.get("_partial", []):
            out.setdefault("_partial", []).append(k)
    return out


class H(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._setup_bid()
        u = urlparse(self.path)
        if u.path == "/migrate/pull":
            d = migrate_pull(parse_qs(u.query).get("token", [""])[0])
            payload = json.dumps({"ls": d["ls"]} if d else {"error": "token invalido o caducado"}).encode()
            self.send_response(200 if d else 404)
            self.send_header("Content-Type", "application/json")
            if d and re.fullmatch(r"[0-9a-f]{16}", d["bid"] or ""):
                self.send_header("Set-Cookie", "bid=%s; Path=/; Max-Age=63072000; SameSite=Lax" % d["bid"])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if u.path.startswith("/auth/") or u.path.startswith("/pl") or u.path.startswith("/spot/") or u.path.startswith("/imp/") or u.path in ("/ytlib", "/rate", "/libsave"):
            try:
                if u.path == "/auth/status":
                    if parse_qs(u.query).get("fresh"):
                        _CACHE.pop(_sk("sess_alive"), None)   # el poll post-login necesita el estado real, no el cacheado
                    return self._json(auth_status())
                if u.path == "/auth/weblogin":
                    if not WEBLOGIN:
                        return self._json({"error": "Solo disponible en la app de escritorio (Blyatt.bat / py main.py)"})
                    WEBLOGIN(parse_qs(u.query).get("silent", ["0"])[0] == "1")
                    return self._json({"ok": True})
                if u.path == "/auth/logout":
                    return self._json(auth_logout())
                if u.path == "/ytlib":
                    parts = [p for p in parse_qs(u.query).get("parts", [""])[0].split(",") if p]
                    if parts:   # carga por etapas (primer login): siempre fresca, no toca la cache completa
                        return self._json(yt_library(parts))
                    k = _sk("ytlib")
                    if parse_qs(u.query).get("fresh"):
                        _CACHE.pop(k, None)   # abrir "Me gusta" fuerza re-fetch real de la cuenta
                    hit = _CACHE.get(k)
                    if hit and time.time() - hit[0] < 300:
                        return self._json(hit[1])
                    d = yt_library()
                    if not d.get("error") and not d.get("_partial"):
                        _CACHE[k] = (time.time(), d)
                    return self._json(d)
                if u.path == "/rate":
                    qs = parse_qs(u.query)
                    return self._json(rate_song(qs.get("id", [""])[0],
                                                qs.get("like", ["1"])[0] == "1"))
                if u.path == "/libsave":
                    qs = parse_qs(u.query)
                    return self._json(lib_save(qs.get("id", [""])[0],
                                               qs.get("kind", [""])[0],
                                               qs.get("save", ["1"])[0] == "1"))
                if u.path == "/imp/progress":
                    return self._json(_imp_prog)
                if u.path == "/imp/cancel":
                    globals()["_imp_cancel"] = True
                    return self._json({"ok": True})
                if u.path.startswith("/spot/") and SERVER_MODE:
                    # la sesion de Spotify es GLOBAL (auth/spotify.json), no por dispositivo: en el server
                    # publico cualquiera podria configurarla o usarla. El import de Spotify es de escritorio
                    if u.path == "/spot/status":
                        return self._json({"logged_in": False, "disabled": True})
                    return self._json({"error": "Importar de Spotify solo está disponible en la app de escritorio"}, 403)
                if u.path.startswith("/spot/"):
                    qs = parse_qs(u.query)
                    g = lambda k: qs.get(k, [""])[0]
                    if u.path == "/spot/login":
                        return self._json(spot_login())
                    if u.path == "/spot/setclient":
                        return self._json(spot_set_client(g("id")))
                    if u.path == "/spot/callback":
                        ok, err = spot_callback(g("code"), g("state"))
                        page = ("<html><body style='font-family:sans-serif;background:#0b0b0f;color:#eee;"
                                "display:grid;place-items:center;height:100vh'><div style='text-align:center'>"
                                + ("<h2>Spotify conectado</h2><p>Vuelve a Blyatt, esta pestaña ya puede cerrarse.</p>"
                                   if ok else "<h2>Error</h2><p>%s</p>" % err)
                                + "</div></body></html>").encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(page)))
                        self.end_headers()
                        self.wfile.write(page)
                        return
                    if u.path == "/spot/status":
                        return self._json(spot_status())
                    if u.path == "/spot/lib":
                        return self._json(spot_lib())
                    if u.path == "/spot/cancel":
                        globals()["_imp_cancel"] = True
                        return self._json({"ok": True})
                    if u.path == "/spot/progress":
                        return self._json(_imp_prog)
                    if u.path == "/spot/import":
                        return self._json(spot_import(g("kind"), g("id"), g("title"), g("cover")))
                if u.path.startswith("/pl"):
                    qs = parse_qs(u.query)
                    g = lambda k: qs.get(k, [""])[0]
                    if u.path == "/plcreate":
                        return self._json(pl_create(g("title"), g("desc"), g("privacy")))
                    if u.path == "/pledit":
                        return self._json(pl_edit(g("id"), g("title"),
                                                  qs.get("desc", [None])[0], g("privacy")))
                    if u.path == "/pldelete":
                        return self._json(pl_delete(g("id")))
                    if u.path == "/pladd":
                        return self._json(pl_add(g("id"), g("vid")))
                    if u.path == "/plremove":
                        return self._json(pl_remove(g("id"), g("vid")))
                    if u.path == "/plcollab":
                        return self._json(pl_collab(g("id"), g("on") == "1"))
                    if u.path == "/pljoin":
                        return self._json(pl_join(g("link")))
            except Exception as e:
                return self._json({"error": str(e)}, 502)
        if u.path == "/search":
            qs = parse_qs(u.query)
            q = qs.get("q", [""])[0]
            filt = qs.get("filter", [""])[0]
            try:
                payload = json.dumps(search(q, filt)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/related":
            try:
                payload = json.dumps(related(parse_qs(u.query).get("id", [""])[0])).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/radio":
            try:
                payload = json.dumps(radio(parse_qs(u.query).get("id", [""])[0])).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/newreleases":
            try:
                payload = json.dumps(new_releases()).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/img":
            # proxy de caratulas: mismo origen para poder leer el color promedio en canvas sin taint de CORS.
            src = parse_qs(u.query).get("u", [""])[0]
            if not _host_allowed(urlparse(src).hostname, IMG_HOSTS):
                self.send_response(403); self.end_headers(); return
            try:
                payload, ctype = safe_fetch(src, IMG_HOSTS, timeout=10, max_bytes=4 * 1024 * 1024)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "max-age=86400")
            except Exception:
                self.send_response(502); self.send_header("Content-Type", "text/plain")
                payload = b""
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        elif u.path == "/songid":
            sq = parse_qs(u.query)
            try:
                payload = json.dumps({"id": song_id(sq.get("title", [""])[0], sq.get("artist", [""])[0])}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/artist":
            try:
                payload = json.dumps(artist(parse_qs(u.query).get("id", [""])[0])).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/artistlist":
            aq = parse_qs(u.query)
            try:
                payload = json.dumps(artist_list(aq.get("id", [""])[0], aq.get("params", [""])[0])).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/collection":
            try:
                payload = json.dumps(collection(parse_qs(u.query).get("id", [""])[0])).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/versions":
            try:
                payload = json.dumps(album_versions(parse_qs(u.query).get("id", [""])[0])).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/lyrics":
            qs = parse_qs(u.query)
            try:
                payload = json.dumps(lyrics(qs.get("title", [""])[0], qs.get("artist", [""])[0])).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload); return
        elif u.path == "/stream":
            # proxy de los bytes de audio (mismo origen) para decodificar con Web Audio (gapless real).
            # Cache en disco: repetida = lectura local; si no, se baja a disco reenviando por trozos
            vid = parse_qs(u.query).get("id", [""])[0]
            if not re.fullmatch(r"[\w-]{6,20}", vid):
                self.send_response(400); self.end_headers(); return
            fp = _audio_path(vid)
            sent = [False]

            def start(ctype, clen):
                self.send_response(200)
                self.send_header("Content-Type", ctype or "audio/mp4")
                self.send_header("Cache-Control", "max-age=86400")
                if clen:
                    self.send_header("Content-Length", str(clen))
                self.end_headers()
                sent[0] = True
            try:
                with _id_lock("dl", vid):   # si la precarga ya la esta bajando, espera y sale de disco
                    if os.path.isfile(fp) and not _mp4_ok(fp):   # stub cacheado antes del fix: se re-baja
                        os.remove(fp)
                    if os.path.isfile(fp):
                        os.utime(fp)   # LRU: usada ahora
                        start("audio/mp4", os.path.getsize(fp))
                        with open(fp, "rb") as f:
                            while True:
                                chunk = f.read(262144)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                    else:
                        audio_fetch(vid, start, self.wfile.write)
            except Exception:
                if not sent[0]:
                    try:
                        self.send_response(502); self.end_headers()
                    except Exception:
                        pass
            return
        elif u.path == "/viewcounts":
            payload = json.dumps(viewcounts(parse_qs(u.query).get("ids", [""])[0].split(","))).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
        elif u.path == "/prewarm":
            prewarm([x for x in parse_qs(u.query).get("ids", [""])[0].split(",") if x])
            payload = b'{"ok": true}'
            self.send_response(200); self.send_header("Content-Type", "application/json")
        elif u.path == "/audio":
            aqs = parse_qs(u.query)
            vid = aqs.get("id", [""])[0]
            fresh = aqs.get("fresh", [""])[0] == "1"
            try:
                payload = json.dumps({"url": audio_url(vid, fresh)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            except Exception as e:
                payload = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
        elif u.path.startswith("/assets/"):
            fp = os.path.normpath(os.path.join(BASE, u.path.lstrip("/")))
            if not fp.startswith(os.path.join(BASE, "assets") + os.sep) or not os.path.isfile(fp):  # evita path traversal
                self.send_response(404); self.end_headers(); return
            ctype = {".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff",
                     ".js": "text/javascript", ".png": "image/png", ".svg": "image/svg+xml",
                     ".json": "application/manifest+json"}.get(
                         os.path.splitext(fp)[1], "application/octet-stream")
            with open(fp, "rb") as f:
                payload = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "max-age=604800")
        else:
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                payload = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if getattr(self, "_new_bid", ""):   # identidad del dispositivo para sesiones independientes
                self.send_header("Set-Cookie", "bid=%s; Path=/; Max-Age=63072000; SameSite=Lax" % self._new_bid)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _setup_bid(self):
        m = re.search(r"(?:^|;\s*)bid=([0-9a-f]{16})", self.headers.get("Cookie") or "")
        self._new_bid = "" if m else os.urandom(8).hex()
        _REQ.bid = m.group(1) if m else self._new_bid

    def do_POST(self):
        self._setup_bid()
        u = urlparse(self.path)
        if u.path == "/migrate/push":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 2 * 1024 * 1024:   # un localStorage real ronda cientos de KB
                return self._json({"error": "demasiado grande"}, 413)
            try:
                d = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
            except Exception:
                d = {}
            ls = {k: v for k, v in (d.get("ls") or {}).items()
                  if isinstance(k, str) and k.startswith("lightning.") and isinstance(v, str)}
            # sin cookie previa (_new_bid) no hay sesion que heredar
            return self._json({"token": migrate_push("" if self._new_bid else _REQ.bid, ls)})
        if u.path == "/auth/headers":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            return self._json(auth_set_headers(raw))
        if u.path == "/auth/link/new":
            return self._json(link_new())
        if u.path == "/auth/link/use":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1024:
                return self._json({"error": "demasiado grande"}, 413)
            try:
                d = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
            except Exception:
                d = {}
            if self._new_bid:   # sin cookie bid el dispositivo no sabria que sesion es la suya
                return self._json({"error": "Recarga la página e inténtalo de nuevo"})
            return self._json(link_use(d.get("code")))
        if u.path == "/auth/cookie":
            # login nativo (app Capacitor): el WebView captura la cookie de music.youtube.com y la manda aqui
            n = int(self.headers.get("Content-Length") or 0)
            try:
                d = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
                b = getattr(_REQ, "bid", "")
                tgt = _bid_path(b) if (SERVER_MODE and b) else BROWSER_FILE
                os.makedirs(AUTH_DIR, exist_ok=True)
                ok = save_browser_cookie(d.get("cookie") or "", d.get("ua") or None, target=tgt)
                if not ok:
                    try: os.remove(tgt)
                    except OSError: pass
                return self._json({"ok": bool(ok)} if ok else {"error": "YouTube no reconoce la sesión"})
            except Exception as e:
                return self._json({"error": str(e)[:120]}, 502)
        if u.path == "/plcover":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            try:
                d = json.loads(raw)
                head, b64 = (d.get("data") or "").split(",", 1)
                mime = head.split(":")[1].split(";")[0] if ":" in head else "image/jpeg"
                return self._json(pl_set_cover(d.get("id") or "", base64.b64decode(b64), mime))
            except Exception as e:
                return self._json({"error": str(e)}, 502)
        if u.path == "/implace":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            try:
                d = json.loads(raw)
                return self._json(imp_replace(d.get("id") or "", d.get("vids") or []))
            except Exception as e:
                return self._json({"error": str(e)}, 502)
        if u.path == "/impcsv":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            try:
                return self._json(import_csv(raw))
            except Exception as e:
                return self._json({"error": str(e)}, 502)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


# ThreadingHTTPServer: la extraccion con yt-dlp tarda; sin hilos una reproduccion bloquearia las busquedas.
def serve(port=8000):
    _spot_load()   # restaura el token de Spotify persistido (si sigue vivo)
    # host configurable: escritorio usa 127.0.0.1 (privado); en servidor BLYATT_HOST=0.0.0.0 lo expone
    host = os.environ.get("BLYATT_HOST", "127.0.0.1")

    def _warm():   # la 1a extraccion carga los extractores de yt-dlp (+~1s): que no la pague el usuario
        try:
            _ydl_pool.put(YoutubeDL(dict(_YDL_OPTS)))
            _extract_with("jNQXAC9IVRw")
        except Exception:
            pass
        _audio_evict()
        if _session_files():   # con cuenta: solver JS listo para explicitas / itag 18 roto
            _jsc_warm()
    threading.Thread(target=_warm, daemon=True).start()
    return ThreadingHTTPServer((host, port), H)


if __name__ == "__main__":
    print("http://localhost:8000")
    serve().serve_forever()
