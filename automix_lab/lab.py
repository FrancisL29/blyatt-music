"""Laboratorio de Automix (PC). Ejecutar:  py automix_lab/lab.py   ->  http://localhost:8040

Busca dos canciones (con el buscador, la descarga, la letra y el genero de Wave), las analiza, propone
transiciones ordenadas por puntuacion y las renderiza a un clip para escucharlas y compararlas.
Todo con numpy + scipy + onnxruntime: lo mismo que puede correr el servidor (Termux) despues.
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import app                      # noqa: E402  (buscador, audio, letra, genero de Wave)
import analysis as an           # noqa: E402
import mix                      # noqa: E402
import planner                  # noqa: E402

CACHE = os.path.join(HERE, "cache")
os.makedirs(CACHE, exist_ok=True)
SPL_DIR = os.path.join(ROOT, "models", "spleeter-2stems")
_locks, _llock = {}, threading.Lock()
_audio = {}          # id -> (mezcla, instrumental): las ultimas canciones en memoria
_spl = None


def _jload(p):
    with open(p, encoding="utf8") as f:
        return json.load(f)


def _lock(k):
    with _llock:
        return _locks.setdefault(k, threading.Lock())


def _spleeter():
    global _spl
    if _spl is None:
        _spl = an.Spleeter(SPL_DIR)
    return _spl


def get_audio(vid):
    fp = app._audio_path(vid)
    with app._id_lock("dl", vid):
        if os.path.isfile(fp) and not app._mp4_ok(fp):
            os.remove(fp)
        if not os.path.isfile(fp):
            app.audio_fetch(vid)
    return fp


def load(vid, inst=True):
    if vid in _audio and (_audio[vid][1] is not None or not inst):
        return _audio[vid]
    x = an.decode(get_audio(vid))
    ip = os.path.join(CACHE, vid + "_inst.npy")
    I = np.load(ip).astype(np.float32) if inst and os.path.isfile(ip) else None
    if len(_audio) > 6:
        _audio.pop(next(iter(_audio)))
    _audio[vid] = (x, I)
    return _audio[vid]


def family(r, bpm, genres):
    four, even, conf = r["four"], r["even"], r["conf"]
    fam = "live" if conf < 1.12 else \
        "club" if ((((four > 1.45 and even > .6 and conf > 1.3) or (conf > 2 and even > .5 and four > 1.2)) and bpm >= 112) or (even > .7 and conf > 1.4 and bpm >= 128)) else \
        "urban" if (even < .55 or four < 1.2 or (bpm <= 108 and conf > 2)) else "pop"
    g = " ".join(genres or []).lower()

    def cat(x):
        if re.search(r"techno|house|electro|dance|trance|edm|hardstyle|drum|dubstep|club", x):
            return "club"
        if re.search(r"rap|hip ?hop|latin|reggae|trap|dancehall|r&b|afro|dembow|urban", x):
            return "urban"
        if re.search(r"rock|jazz|classical|folk|blues|metal|country|singer|acoustic|alternative|indie|soul", x):
            return "live"
        return "pop" if "pop" in x else None
    cats = [c for c in (cat(x.lower()) for x in genres or []) if c]
    if not cats:
        return fam
    if "club" in cats and conf > 1.1 and (four > 1.15 or even > .5):
        return "club"
    if cats[0] == "urban" or ("urban" in cats and "club" not in cats):
        return fam if (fam == "club" and bpm >= 118 and conf > 2 and four > 1.45) or fam == "live" else "urban"
    if cats[0] == "live":
        return "live" if conf < 1.35 else ("club" if fam == "club" and four > 1.6 else "pop")
    if cats[0] == "pop" and fam == "club" and not (four > 1.45 and even > .6 and conf > 1.5):
        return "pop"
    return fam


def lyrics_lines(vid, title, artist, dur):
    try:
        d = app.lyrics(title, artist, dur, vid)
    except Exception:
        return [], None
    if d.get("type") not in ("Word", "Line"):
        return [], d.get("source")
    try:
        off = float(app.lyrics_align(vid, title, artist, d.get("source", ""), dur).get("offset") or 0)
    except Exception:
        off = 0
    L = [l for l in d.get("lines") or [] if isinstance(l.get("time"), (int, float)) and re.sub(r"[♪♫♬~\s]", "", l.get("text") or "")]
    out = []
    for i, l in enumerate(L):
        s = (l["time"] + off) / 1000
        nx = (L[i + 1]["time"] + off) / 1000 if i + 1 < len(L) else 1e9
        syl = [w for w in l.get("syllabus") or [] if isinstance(w.get("time"), (int, float))]
        if syl:
            e = (syl[-1]["time"] + (syl[-1].get("duration") or 300) + off) / 1000
        elif l.get("duration"):
            e = s + l["duration"] / 1000
        else:
            e = s + max(1.2, min(6, len((l.get("text") or "").split()) * .42))
        out.append({"t": round(s, 2), "e": round(max(s + .3, min(e, nx - .05)), 2), "text": l.get("text")})
    return out, d.get("source")


# ------------------------------------------------------------------ informacion que ya existe en otros sitios
_GLAB = {"verse": "estrofa", "pre-chorus": "pre-estribillo", "prechorus": "pre-estribillo", "chorus": "estribillo",
         "post-chorus": "post-estribillo", "hook": "estribillo", "refrain": "estribillo", "bridge": "puente",
         "intro": "intro", "outro": "outro", "break": "break", "breakdown": "break", "drop": "drop",
         "interlude": "interludio", "instrumental": "instrumental", "build": "subida", "build-up": "subida"}
_nzt = lambda x: re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", unicodedata.normalize("NFKD", (x or "").lower()).encode("ascii", "ignore").decode())).strip()


def genius_sections(title, artist):
    """Las letras de Genius marcan las secciones ([Verse 1], [Pre-Chorus], [Chorus], [Bridge], [Outro]...).
    Devuelve [(etiqueta, [lineas])] o []"""
    import html as H
    import urllib.request
    t0 = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "").strip()
    try:
        r = app._genius("search/multi", per_page=5, q="%s %s" % (artist.split(",")[0], t0))
    except Exception:
        return []
    hits = [h["result"] for sec in r.get("sections", []) if sec.get("type") == "song" for h in sec.get("hits", [])]
    na = _nzt(artist.split(",")[0])
    hit = next((h for h in hits if _nzt(h.get("title")).startswith(_nzt(t0)[:12]) and na[:6] in _nzt(h.get("artist_names"))
                and not re.search(r"remix|edit|live|version|translation|traduc", h.get("title", ""), re.I)), None)
    if not hit:
        return []
    try:
        req = urllib.request.Request(hit["url"], headers={"User-Agent": app._GENIUS_UA["User-Agent"]})
        page = urllib.request.urlopen(req, timeout=15).read().decode("utf8", "replace")
    except Exception:
        return []
    txt = []
    for m in re.finditer(r'<div[^>]*data-lyrics-container="true"[^>]*>', page):   # bloques con divs anidados
        i, depth = m.end(), 1
        for t in re.finditer(r"<(/?)div\b[^>]*>", page[i:]):
            depth += -1 if t.group(1) else 1
            if depth == 0:
                txt.append(page[i:i + t.start()])
                break
    body = re.sub(r"<br\s*/?>", "\n", "\n".join(txt))
    body = H.unescape(re.sub(r"<[^>]+>", "", body))
    out, cur = [], None
    for ln in body.split("\n"):
        ln = ln.strip()
        mh = re.match(r"^\[([^\]:]+?)(?:\s*\d+)?(?::[^\]]*)?\]$", ln)
        if mh:
            key = re.sub(r"\s*\d+$", "", mh.group(1).strip().lower())
            cur = [_GLAB.get(key, key), []]
            out.append(cur)
        elif ln and cur is not None:
            cur[1].append(ln)
    return out


def map_sections(gsecs, lines):
    """Cruza las secciones de Genius con la letra sincronizada: cada seccion empieza en su primera linea"""
    import difflib
    if not gsecs or not lines:
        return []
    res, j = [], 0
    for lab_, gl in gsecs:
        idx = []
        for g in gl:
            ng = _nzt(g)
            best = None
            for k in range(j, min(len(lines), j + 10)):
                sc = difflib.SequenceMatcher(None, ng, _nzt(lines[k]["text"])).ratio()
                if sc > .72 and (best is None or sc > best[0]):
                    best = (sc, k)
            if best:
                idx.append(best[1])
                j = best[1] + 1
        if idx:
            res.append({"label": lab_, "t0": lines[idx[0]]["t"], "t1": lines[idx[-1]]["e"]})
    return res


def repeat_runs(lines, min_rep=3):
    """Lineas que se repiten seguidas (un estribillo que repite "We're up all night to get lucky" x5): bucles
    naturales de voz"""
    out, i = [], 0
    while i < len(lines):
        k = i
        while k + 1 < len(lines) and _nzt(lines[k + 1]["text"]) == _nzt(lines[i]["text"]) and _nzt(lines[i]["text"]):
            k += 1
        if k - i + 1 >= min_rep:
            out.append({"t0": lines[i]["t"], "t1": lines[k]["e"], "n": k - i + 1, "text": lines[i]["text"]})
        i = k + 1
    return out


def deezer_info(title, artist):
    """BPM y volumen de referencia de Deezer (publico, sin clave); bpm 0 = no lo tiene"""
    from urllib.parse import urlencode
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "").strip()
    a = (artist or "").split(",")[0].strip()
    try:
        res = app._lhttp("https://api.deezer.com/search?" + urlencode({"q": 'artist:"%s" track:"%s"' % (a, t), "limit": 3})).get("data") or []
        if not res:
            return {}
        d = app._lhttp("https://api.deezer.com/track/%s" % res[0]["id"])
        return {"bpm": float(d.get("bpm") or 0), "gain": d.get("gain"), "dur": d.get("duration"), "isrc": d.get("isrc")}
    except Exception:
        return {}


def chorus_times(lines):
    nz = lambda x: re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", (x or "").lower())).strip()
    ls = [l for l in lines if len(nz(l["text"]).split()) >= 3]
    cnt = {}
    for l in ls:
        cnt[nz(l["text"])] = cnt.get(nz(l["text"]), 0) + 1
    out = []
    for i in range(len(ls) - 1):
        if cnt[nz(ls[i]["text"])] >= 2 and cnt[nz(ls[i + 1]["text"])] >= 2 and (not out or ls[i]["t"] - out[-1] > 20):
            out.append(ls[i]["t"])
    return out


def analyze(vid, title, artist, dur, stems=True):
    jp = os.path.join(CACHE, vid + ".json")
    with _lock("an:" + vid):
        if os.path.isfile(jp):
            d = _jload(jp)
            if d.get("vocals") is not None or not stems:
                return d
        tm = {}
        t = time.time()
        x = an.decode(get_audio(vid))
        tm["descarga+decodificar"] = round(time.time() - t, 2)
        mono = x.mean(0)
        dz = deezer_info(title, artist)
        t = time.time(); r = an.rhythm(mono, dz.get("bpm")); tm["tempo/golpes"] = round(time.time() - t, 2)
        t = time.time(); k = an.key(mono); tm["tonalidad"] = round(time.time() - t, 2)
        t = time.time(); L = an.lufs(x); tm["volumen"] = round(time.time() - t, 2)
        lvl, ex = an.energy_level(L, r["onsets"], r["envLow"], r["env"], r["bpm"])
        e = r["env"]
        med = float(np.median(e))
        sec = np.convolve(e, np.ones(100) / 100, "same")
        on = np.where(sec > med * .5)[0]
        mstart = float(on[0] / an.FPS) if len(on) else 0.0
        end = np.where(sec > med * .12)[0]
        mend = float(end[-1] / an.FPS) if len(end) else len(e) / an.FPS
        dur_s = x.shape[1] / an.SR
        lines, lsrc = lyrics_lines(vid, title, artist, dur or dur_s)
        vocals, vact, sep_t = None, None, None
        if stems and os.path.isdir(SPL_DIR):
            t = time.time()
            inst = _spleeter().instrumental(x)
            sep_t = time.time() - t
            tm["voz separada (Spleeter)"] = round(sep_t, 2)
            np.save(os.path.join(CACHE, vid + "_inst.npy"), inst.astype(np.float16))
            vact, _ = an.vocal_activity(x, inst)
            vocals = an.spans(vact)
            _audio[vid] = (x, inst)
        t = time.time()
        t = time.time()
        gsec = map_sections(genius_sections(title, artist), lines)
        tm["Genius"] = round(time.time() - t, 2)
        ch = [g["t0"] for g in gsec if g["label"] == "estribillo"] or chorus_times(lines)
        secs, ebar, reps = an.sections(r, k, dur_s, vact, ch)
        for sct in secs:   # etiqueta de Genius si cubre la mayor parte de la seccion
            best = max(gsec, key=lambda g: min(g["t1"], sct["t1"]) - max(g["t0"], sct["t0"]), default=None)
            if best and min(best["t1"], sct["t1"]) - max(best["t0"], sct["t0"]) > .4 * (sct["t1"] - sct["t0"]):
                sct["label"] = best["label"]
        tm["secciones"] = round(time.time() - t, 2)
        try:
            genres = app.mix_genre(title, artist).get("genres") or []
        except Exception:
            genres = []
        d = {"id": vid, "title": title, "artist": artist, "dur": round(dur_s, 2),
             "bpm": round(r["bpm"], 2), "per": r["per"], "phase": r["phase"], "bar0": r["bar0"],
             "conf": round(r["conf"], 3), "four": round(r["four"], 3), "even": round(r["even"], 3),
             "key": {kk: k[kk] for kk in ("name", "camelot", "cam", "letter", "conf")},
             "lufs": round(L, 1), "energy": lvl, "energyInfo": ex, "musicStart": round(mstart, 2), "musicEnd": round(mend, 2),
             "genres": genres, "family": None, "sections": secs, "energyBars": np.round(ebar, 3).tolist(),
             "kick": r["kick"].tolist(), "rep4": reps["rep4"], "rep8": reps["rep8"], "barStart": reps["start"],
             "gsections": gsec, "repeats": repeat_runs(lines), "deezer": dz,
             "vocals": vocals, "lyrics": lines, "lyricsSource": lsrc, "peaks": an.peaks(x),
             "times": tm, "sepXRT": round(dur_s / sep_t, 1) if sep_t else None}
        d["family"] = family(r, r["bpm"], genres)
        d = json.loads(json.dumps(d, default=float))   # numpy -> tipos de JSON
        json.dump(d, open(jp, "w", encoding="utf8"))
        return d


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send(200, open(os.path.join(HERE, "lab.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/api/search":
                res = app.search(q.get("q", ""))
                items = []
                for s in (res or {}).get("sections", []):
                    for it in s.get("items") or []:
                        if it.get("id") and it.get("duration") and re.fullmatch(r"[\w-]{11}", it["id"]):
                            items.append({k: it.get(k) for k in ("id", "title", "artist", "duration", "cover", "subtitle")})
                return self._send(200, items[:12])
            if u.path == "/api/analyze":
                dur = sum(int(x) * 60 ** i for i, x in enumerate(reversed((q.get("dur") or "0").split(":"))) if x.isdigit())
                return self._send(200, analyze(q["id"], q.get("title", ""), q.get("artist", ""), dur, q.get("stems", "1") == "1"))
            if u.path == "/api/plan":
                A = _jload(os.path.join(CACHE, q["a"] + ".json"))
                B = _jload(os.path.join(CACHE, q["b"] + ".json"))
                bars = int(q["bars"]) if q.get("bars", "").isdigit() else None
                return self._send(200, planner.plan(A, B, stems=q.get("stems", "1") == "1", bars=bars))
            if u.path.startswith("/clip/") and re.fullmatch(r"/clip/[0-9a-f]{16}\.wav", u.path):
                return self._send(200, open(os.path.join(CACHE, u.path[6:]), "rb").read(), "audio/wav")
            return self._send(404, {"error": "no"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": str(e)[:300]})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if u.path == "/api/render":
                A = _jload(os.path.join(CACHE, d["a"] + ".json"))
                B = _jload(os.path.join(CACHE, d["b"] + ".json"))
                stems = bool(d.get("stems", True))
                xa, ia = load(d["a"], stems)
                xb, ib = load(d["b"], stems)
                t = time.time()
                wav, marks, info = mix.render(A, B, {"A": {"mix": xa, "inst": ia}, "B": {"mix": xb, "inst": ib}}, d["plan"], stems)
                info["renderSecs"] = round(time.time() - t, 2)
                name = hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16] + ".wav"
                open(os.path.join(CACHE, name), "wb").write(wav)
                return self._send(200, {"url": "/clip/" + name, "marks": marks, "info": info})
            return self._send(404, {"error": "no"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": str(e)[:300]})


def main(port=8040, open_browser=True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print("Laboratorio de Automix: http://localhost:%d" % port, flush=True)
    if open_browser:
        threading.Timer(.8, lambda: webbrowser.open("http://localhost:%d" % port)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main(open_browser="--no-browser" not in sys.argv)
