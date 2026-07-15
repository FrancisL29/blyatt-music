#!/usr/bin/env python3
"""Buscador de YouTube Music sin API key. Proxy + estaticos en stdlib."""
import concurrent.futures
import difflib
import json
import os
import re
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
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
    artist = ", ".join(a["name"] for a in artists) or "".join(r.get("text", "") for r in runs(1)[2:3])
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
    return cached("col:" + browse_id, 600, lambda: _collection(browse_id))


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
_ytm_inst = None
WEBLOGIN = None   # main.py (pywebview) inyecta aqui el launcher de la ventana de login de Google


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


def save_browser_cookie(cookie_header, user_agent=None):
    # cookies frescas extraidas del perfil WebView2 -> browser.json; devuelve True si la sesion vale.
    # CRITICO: guardar x-goog-visitor-id REAL de la sesion; si falta, ytmusicapi inyecta uno anonimo
    # (get_visitor_id sin auth) y YouTube trata todo como deslogueado (logged_in=0).
    global _ytm_inst
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
    with open(BROWSER_FILE, "w", encoding="utf8") as f:
        json.dump(hdrs, f, indent=1)
    _ytm_inst = None
    _CACHE.pop("sess_alive", None)
    _CACHE.pop("ytlib", None)
    return _session_alive()


def ytm():
    global _ytm_inst
    if _ytm_inst:
        return _ytm_inst
    if not (YTMusic and os.path.exists(BROWSER_FILE)):
        return None
    try:
        _ytm_inst = YTMusic(BROWSER_FILE, language="es")
    except Exception:
        _ytm_inst = None
    return _ytm_inst


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
    has_file = os.path.exists(BROWSER_FILE)
    alive = bool(has_file and cached("sess_alive", 300, _session_alive))
    st = {"logged_in": alive, "stale": has_file and not alive, "available": YTMusic is not None}
    if alive:
        st.update(cached("acct", 3600, _account_info))
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
    global _ytm_inst
    if not YTMusic:
        return {"error": "ytmusicapi no instalado"}
    os.makedirs(AUTH_DIR, exist_ok=True)
    try:
        ytmusicapi.setup(filepath=BROWSER_FILE, headers_raw=_headers_from_any(raw))
        _ytm_inst = None
        _CACHE.pop("sess_alive", None)
        if not _session_alive():   # valida contra el flag logged_in real, no solo formato
            raise ValueError("YouTube no reconoce la sesión (headers viejos o de una petición sin login)")
        _CACHE.pop("ytlib", None)
        return {"ok": True}
    except Exception as e:
        _ytm_inst = None
        try:
            os.remove(BROWSER_FILE)
        except OSError:
            pass
        return {"error": "Headers inválidos o sesión caducada: " + str(e)[:120]}


def auth_logout():
    global _ytm_inst
    _ytm_inst = None
    _CACHE.pop("ytlib", None)
    _CACHE.pop("sess_alive", None)
    _CACHE.pop("acct", None)
    try:
        os.remove(BROWSER_FILE)
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
    _CACHE.pop("ytlib", None)
    return {"ok": True}


def pl_create(title, desc, privacy):
    # crea playlist REAL en la cuenta de YT Music (privacy: PRIVATE|UNLISTED|PUBLIC)
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    pid = y.create_playlist(title, desc or "", privacy_status=privacy or "PRIVATE")
    if not isinstance(pid, str):
        return {"error": "YT Music rechazó la creación"}
    _CACHE.pop("ytlib", None)
    return {"id": pid}


def pl_edit(pid, title, desc, privacy):
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    y.edit_playlist(pid, title=title or None, description=desc if desc is not None else None,
                    privacyStatus=privacy or None)
    _CACHE.pop("ytlib", None)
    _CACHE.pop("col:VL" + pid, None)
    return {"ok": True}


def pl_delete(pid):
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    y.delete_playlist(pid)
    _CACHE.pop("ytlib", None)
    return {"ok": True}


def pl_add(pid, vid):
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    r = y.add_playlist_items(pid, [vid], duplicates=False)
    st = (r or {}).get("status", "")
    if "SUCCEEDED" not in str(st):
        return {"error": "Ya está en la playlist" if "FAILED" in str(st) else "No se pudo añadir"}
    _CACHE.pop("ytlib", None)
    _CACHE.pop("col:VL" + pid, None)
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
        _CACHE.pop("col:VL" + pid, None)   # re-purga: el fetch de arriba pudo repoblar via /collection concurrente
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
    _CACHE.pop("ytlib", None)
    _CACHE.pop("col:VL" + pid, None)
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
    _CACHE.pop("ytlib", None)
    d = {}
    try:
        d = y.get_playlist(pid, limit=1) or {}
    except Exception:
        pass
    th = d.get("thumbnails") or []
    return {"ok": True, "id": pid, "title": d.get("title", "Playlist colaborativa"),
            "cover": th[-1]["url"] if th else ""}


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
    _CACHE.pop("ytlib", None)
    _CACHE.pop("col:VL" + pid, None)
    return {"ok": True}


def rate_song(video_id, like):
    # like en la app -> me gusta en la cuenta de YT Music del usuario
    y = ytm()
    if not y:
        return {"error": "Sin sesión de Google"}
    y.rate_song(video_id, "LIKE" if like else "INDIFFERENT")
    _CACHE.pop("ytlib", None)   # la biblioteca cambio: proximo /ytlib re-fetch
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

    def liked():
        d = y.get_liked_songs(limit=200)
        out = []
        for t in d.get("tracks") or []:
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
        acct = ""
        try:
            acct = (cached("acct", 3600, _account_info) or {}).get("name", "")
        except Exception:
            pass
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
            out.append({"browseId": "VL" + pid, "kind": "playlists", "title": p.get("title", ""),
                        "subtitle": ("%s canciones" % n) if n else "Playlist", "cover": _yt_thumb(p),
                        "editable": own})
        return out

    def artists():
        return [{"browseId": a["browseId"], "kind": "artists", "title": a.get("artist", ""),
                 "subtitle": a.get("subscribers", "") or "Artista", "cover": _yt_thumb(a)}
                for a in y.get_library_subscriptions(limit=50) if a.get("browseId")]

    def albums():
        return [{"browseId": a["browseId"], "kind": "albums", "title": a.get("title", ""),
                 "subtitle": _yt_artists(a) or str(a.get("year", "")), "cover": _yt_thumb(a)}
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
        u = urlparse(self.path)
        if u.path.startswith("/auth/") or u.path.startswith("/pl") or u.path in ("/ytlib", "/rate", "/libsave"):
            try:
                if u.path == "/auth/status":
                    if parse_qs(u.query).get("fresh"):
                        _CACHE.pop("sess_alive", None)   # el poll post-login necesita el estado real, no el cacheado
                    return self._json(auth_status())
                if u.path == "/auth/weblogin":
                    if not WEBLOGIN:
                        return self._json({"error": "Solo disponible en la app de escritorio (Blyatt.bat / py main.py)"})
                    WEBLOGIN(parse_qs(u.query).get("silent", ["0"])[0] == "1")
                    return self._json({"ok": True})
                if u.path == "/auth/logout":
                    return self._json(auth_logout())
                if u.path == "/ytlib":
                    return self._json(cached("ytlib", 300, yt_library))
                if u.path == "/rate":
                    qs = parse_qs(u.query)
                    return self._json(rate_song(qs.get("id", [""])[0],
                                                qs.get("like", ["1"])[0] == "1"))
                if u.path == "/libsave":
                    qs = parse_qs(u.query)
                    return self._json(lib_save(qs.get("id", [""])[0],
                                               qs.get("kind", [""])[0],
                                               qs.get("save", ["1"])[0] == "1"))
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
                     ".js": "text/javascript", ".png": "image/png", ".svg": "image/svg+xml"}.get(
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
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/auth/headers":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            return self._json(auth_set_headers(raw))
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


# ThreadingHTTPServer: la extraccion con yt-dlp tarda; sin hilos una reproduccion bloquearia las busquedas.
def serve(port=8000):
    return ThreadingHTTPServer(("127.0.0.1", port), H)


if __name__ == "__main__":
    print("http://localhost:8000")
    serve().serve_forever()
