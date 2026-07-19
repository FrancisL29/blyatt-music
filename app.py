#!/usr/bin/env python3
"""Buscador de YouTube Music sin API key. Proxy + estaticos en stdlib."""
import base64
import concurrent.futures
import difflib
import hashlib
import hmac
import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
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
        if plays: out["plays"] = plays
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


def _collection_auth(pid):
    # playlists con sesion: get_playlist autenticado (las PRIVADAS son invisibles al browse anonimo)
    y = ytm()
    if not y:
        return None
    d = y.get_playlist(pid, limit=500)
    tracks = []
    for t in d.get("tracks") or []:
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
            "creator": au.get("name", ""), "creatorId": au.get("id"), "meta": meta,
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
    # el cliente android es el que devuelve audio de forma fiable (web exige PO token actualmente)
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
}


def _extract_with(video_id, cookies_browser=None, clients=None):
    opts = dict(_YDL_OPTS)
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)   # cookies del navegador para sortear el age-gate
    with YoutubeDL(opts) as y:
        info = y.extract_info("https://music.youtube.com/watch?v=" + video_id, download=False)
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


def _extract(video_id):
    # id ya conocido como age-gated: directo al alt que funciono (evita ~5s del intento fallido)
    hit = _CACHE.get("alt:" + video_id)
    if hit and time.time() - hit[0] < 86400:
        try:
            return _extract_with(hit[1])
        except Exception:
            _CACHE.pop("alt:" + video_id, None)
    try:
        return _extract_with(video_id)
    except Exception as e:
        if "age" not in str(e).lower() and "sign in" not in str(e).lower():
            raise
        # subidas alternativas del mismo tema: extrae en paralelo, gana la de mayor similitud que funcione
        alts = _alt_ids(video_id)
        if alts:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(alts))) as ex:
                futs = [(a, ex.submit(_extract_with, a)) for a in alts[:4]]
                for a, f in futs:
                    try:
                        url = f.result()
                        _CACHE["alt:" + video_id] = (time.time(), a)
                        return url
                    except Exception:
                        continue
        # ultimo recurso: sesion de YouTube del navegador del usuario
        for br in ("edge", "chrome", "firefox", "brave"):
            try:
                return _extract_with(video_id, br)
            except Exception:
                continue
        raise RuntimeError("Cancion con restriccion de edad: requiere sesion de YouTube")


def song_id(title, artist):
    # devuelve el videoId del "art track" (audio) para reemplazar un music video
    res = search((title + " " + artist).strip(), "songs")
    return res[0]["id"] if res else ""


def audio_url(video_id, fresh=False):
    # cache 3h: la URL de googlevideo esta ligada a IP/tiempo y caduca en horas; evita re-extraer al repetir.
    # fresh=True: el cliente reporto que la URL en cache no cargo -> la purgamos y re-extraemos.
    if fresh:
        _CACHE.pop("a:" + video_id, None)
    return cached("a:" + video_id, 10800, lambda: _extract(video_id))


# ---------- login con Google (headers de music.youtube.com via ytmusicapi) ----------
# OAuth device-flow descartado: YouTube rechaza tokens de cliente TV en la API interna de
# YT Music (HTTP 400 en todos los endpoints desde finales de 2024). Los headers del navegador
# son la via soportada por ytmusicapi y la cookie dura anios.
AUTH_DIR = os.path.join(BASE, "auth")
BROWSER_FILE = os.path.join(AUTH_DIR, "browser.json")
WEBLOGIN = None   # main.py (pywebview) inyecta aqui el launcher de la ventana de login de Google

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
    _CACHE.pop(_sk("sess_alive", tgt), None)
    _CACHE.pop(_sk("ytlib", tgt), None)
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
    for k in ("ytlib", "sess_alive", "acct"):
        _CACHE.pop(_sk(k, f), None)
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
                _spot_dbg("req %s HTTP %s: %s" % (path[:30], e.code, e.read().decode("utf-8", "replace")[:100]))
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


def _spot_match(y, title, artists):
    # mejor match de YT Music para una pista de Spotify (titulo+artista, similitud difflib).
    # Devuelve {id,title,artist,cover,duration} o None (el resolutor manual muestra que se eligio)
    try:
        res = y.search((title + " " + artists).strip(), filter="songs", limit=5) or []
    except Exception:
        return None
    tl, al = title.lower(), artists.lower()
    best, bs = None, 0.0
    for r in res[:5]:
        vid = r.get("videoId")
        if not vid:
            continue
        s = difflib.SequenceMatcher(None, tl, (r.get("title") or "").lower()).ratio()
        ra = ", ".join(a.get("name", "") for a in r.get("artists") or []).lower()
        if ra and al and (ra.split(",")[0].strip() in al or al.split(",")[0].strip() in ra):
            s += .25
        if s > bs:
            bs, best = s, r
    if bs < .55 or not best:
        return None
    return {"id": best["videoId"], "title": best.get("title", ""),
            "artist": ", ".join(a.get("name", "") for a in best.get("artists") or []),
            "cover": (best.get("thumbnails") or [{}])[-1].get("url", ""),
            "duration": best.get("duration") or ""}


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
        rep[i] = _spot_match(y, pairs[i][0], pairs[i][1])
        _imp_prog["done"] += 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, range(len(pairs))))
    return [{"title": pairs[i][0], "artists": pairs[i][1], "match": rep[i]} for i in range(len(pairs))]


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
            res = y.search(title, filter="artists", limit=5) or []
            bid = next((r.get("browseId") for r in res if r.get("browseId")), None)
            if not bid:
                return {"error": "No encontrado en YT Music: " + title}
            y.subscribe_artists([bid])
            _CACHE.pop(_sk("ytlib"), None)
            return {"ok": True, "added": 1, "missed": []}
        if kind == "album":
            r = (y.search(title, filter="albums", limit=5) or [{}])[0]
            bid = r.get("browseId")
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
            # YT descarta rate_song en silencio bajo cuota (rafagas pierden ~90%). Estrategia:
            # pasadas convergentes — likear lo pendiente (3 workers, pausa corta), esperar a que
            # YT materialice (consistencia eventual, espera creciente), re-verificar contra la
            # cuenta y repetir SOLO lo que falta. Re-import reanuda gratis (skip de ya-likeados).
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
            added = sum(1 for v in vids if v in after or v in have)
            # purga ytlib de TODAS las sesiones del mismo usuario: el proximo sync de cualquier
            # dispositivo (movil incluido) ve el estado final, no una foto a mitad de import
            for k in [k for k in list(_CACHE) if str(k).endswith("|ytlib")]:
                _CACHE.pop(k, None)
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


def _cover_from_url(url):
    # descarga una portada externa (p.ej. i.scdn.co de Spotify) para subirla a YT
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(), r.headers.get_content_type() or "image/jpeg"


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
        pairs = [(t[0], t[1] if len(t) > 1 else "") for t in d.get("tracks") or [] if t and t[0]]
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


def yt_library():
    # biblioteca real de YT Music del usuario: me gusta + playlists + artistas + albumes (4 fetch en paralelo)
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

    def _liked_ids_raw():
        # shape innertube 2025: videoId ya no viene en playlistItemData (ytmusicapi 1.12 lo parsea
        # None en TODAS las pistas) sino en el watchEndpoint de cada fila. Se pagina VLLM a mano y
        # se devuelven los ids EN ORDEN (None si la fila no es reproducible) para alinear por indice.
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
        r = y._send_request("browse", {"browseId": "VLLM"})
        while True:
            tok = [None]
            scan(r, ids, tok)
            pages += 1
            if not tok[0] or pages > 40:
                break
            r = y._send_request("browse", {"continuation": tok[0]})
        return ids

    def liked():
        d = None
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
                ids = _liked_ids_raw()
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
        futs = {k: ex.submit(f) for k, f in
                (("liked", liked), ("playlists", playlists), ("artists", artists), ("albums", albums))}
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
        if u.path.startswith("/auth/") or u.path.startswith("/pl") or u.path.startswith("/spot/") or u.path in ("/ytlib", "/rate", "/libsave"):
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
            host = urlparse(src).hostname or ""
            if not host.endswith(("ytimg.com", "ggpht.com", "googleusercontent.com")):
                self.send_response(403); self.end_headers(); return
            try:
                with urllib.request.urlopen(src, timeout=10) as resp:
                    payload = resp.read()
                    ctype = resp.headers.get("Content-Type", "image/jpeg")
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
            # proxy de los bytes de audio (mismo origen) para poder decodificar con Web Audio (gapless real)
            vid = parse_qs(u.query).get("id", [""])[0]
            try:
                src = audio_url(vid)
                req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as up:
                    data = up.read()
                    ctype = up.headers.get("Content-Type", "audio/mp4")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "max-age=3600")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(502); self.end_headers()
            return
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
        if u.path == "/auth/headers":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            return self._json(auth_set_headers(raw))
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
    return ThreadingHTTPServer((host, port), H)


if __name__ == "__main__":
    print("http://localhost:8000")
    serve().serve_forever()
