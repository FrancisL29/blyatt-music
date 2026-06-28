#!/usr/bin/env python3
"""Buscador de YouTube Music sin API key. Proxy + estaticos en stdlib."""
import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL

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
    if vid and title:
        out = {"id": vid, "title": title, "artist": artist, "cover": cover, "type": _mv_type(item)}
        if artists: out["artists"] = artists
        if album: out["album"] = album
        if dur: out["duration"] = dur
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
        return {"browseId": bid, "title": title, "subtitle": subtitle, "cover": cover, "kind": kind}
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
    if val:
        _CACHE[key] = (time.time(), val)
    return val


def _search(query, filt):
    body = {"context": CTX, "query": query}
    if filt in _FILTERS:
        body["params"] = _FILTERS[filt]
    req = urllib.request.Request(YTM_URL, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return parse_browse(data, filt) if filt in _BROWSE_KINDS else parse_results(data)


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


def _collection(browse_id):
    data = _yt_browse(browse_id)
    kind = "album" if browse_id.startswith("MPRE") else "playlist"
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


def _album_versions(browse_id):
    # "Other versions" del album: carrusel de musicTwoRowItemRenderer cuyo header menciona "version"
    data = _yt_browse(browse_id)
    out, seen = [], set()
    for sh in _find_all_renderers(data, "musicCarouselShelfRenderer", []):
        hdr = sh.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})
        if "version" not in _runs_text(hdr.get("title")).lower():
            continue
        for c in sh.get("contents", []):
            it = c.get("musicTwoRowItemRenderer")
            if not it:
                continue
            r = _parse_tworow(it)
            if r and r.get("browseId", "").startswith("MPRE") and r["browseId"] != browse_id and r["browseId"] not in seen:
                seen.add(r["browseId"])
                out.append(r)
    return out


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


_YDL_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best", "quiet": True, "no_warnings": True, "skip_download": True,
    # el cliente android es el que devuelve audio de forma fiable (web exige PO token actualmente)
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
}


def _extract_with(video_id, cookies_browser=None):
    opts = dict(_YDL_OPTS)
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)   # cookies del navegador para sortear el age-gate
    with YoutubeDL(opts) as y:
        info = y.extract_info("https://music.youtube.com/watch?v=" + video_id, download=False)
    return (info.get("url") or (info.get("requested_formats") or [{}])[0].get("url")
            or info["formats"][-1]["url"])


def _extract(video_id):
    try:
        return _extract_with(video_id)
    except Exception as e:
        if "age" not in str(e).lower() and "sign in" not in str(e).lower():
            raise
        # restriccion de edad: reintenta con la sesion de YouTube del navegador del usuario
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


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
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

    def log_message(self, *a):
        pass


# ThreadingHTTPServer: la extraccion con yt-dlp tarda; sin hilos una reproduccion bloquearia las busquedas.
def serve(port=8000):
    return ThreadingHTTPServer(("127.0.0.1", port), H)


if __name__ == "__main__":
    print("http://localhost:8000")
    serve().serve_forever()
