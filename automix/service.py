"""Servicio del Automix: lo que corre en el servidor de Wave (y el laboratorio usa igual).

  configure(api, cache_dir)   api = el modulo app (buscador, audio, letra, Genius, Deezer); cache_dir = donde guardar
  analyze(vid, title, artist, dur)   analisis de una cancion (una vez; JSON en cache)
  prep(a, b)                  pide la transicion a -> b (dicts id/title/artist/dur). No bloquea: la calcula en segundo
                              plano (una a la vez) y devuelve su estado; cuando esta lista, la info para enlazar
  clip_path(key)              el audio del clip (m4a; wav si no hay ffmpeg)

El clip contiene SOLO la mezcla: empieza un poco antes de que entre B (A tal cual) y acaba con B ya a su tempo. El
reproductor suena A -> clip -> B y salta de un archivo a otro donde suenan igual.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import unicodedata

import numpy as np

from . import analysis as an
from . import mix, planner

ENGINE = 1          # sube si cambia el motor o el planificador: las transiciones guardadas se rehacen
AN_VER = 4          # sube cuando cambia el analisis de ritmo: lo guardado se rehace (solo esa parte)
API = None
CACHE = None
_locks, _llock = {}, threading.Lock()
_audio = {}         # id -> audio descodificado (las ultimas canciones)
_jobs = {}          # clave -> {"state": "working"|"error", "t": ..., "err": ...}
_jlock = threading.Lock()
_work = threading.Semaphore(1)     # una transicion a la vez (el servidor es un movil)


def configure(api, cache_dir):
    global API, CACHE
    API, CACHE = api, cache_dir
    os.makedirs(CACHE, exist_ok=True)


def _lock(k):
    with _llock:
        return _locks.setdefault(k, threading.Lock())


def _jload(p):
    with open(p, encoding="utf8") as f:
        return json.load(f)


def _jsave(p, d):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(d, f)
    os.replace(tmp, p)


# ------------------------------------------------------------------ audio
def audio_file(vid):
    fp = API._audio_path(vid)
    with API._id_lock("dl", vid):
        if os.path.isfile(fp) and not API._mp4_ok(fp):
            os.remove(fp)
        if not os.path.isfile(fp):
            API.audio_fetch(vid)
    return fp


def load(vid):
    if vid in _audio:
        return _audio[vid]
    x = an.decode(audio_file(vid))
    while len(_audio) >= 3:
        _audio.pop(next(iter(_audio)))
    _audio[vid] = x
    return x


# ------------------------------------------------------------------ informacion de cada cancion
def family(r, bpm, genres):
    four, even, conf = r["four"], r["even"], r["conf"]
    fam = "live" if conf < 1.12 else \
        "club" if ((((four > 1.45 and even > .6 and conf > 1.3) or (conf > 2 and even > .5 and four > 1.2)) and bpm >= 112) or (even > .7 and conf > 1.4 and bpm >= 128)) else \
        "urban" if (even < .55 or four < 1.2 or (bpm <= 108 and conf > 2)) else "pop"

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
        d = API.lyrics(title, artist, dur, vid)
    except Exception:
        return [], None
    if d.get("type") not in ("Word", "Line"):
        return [], d.get("source")
    try:
        off = float(API.lyrics_align(vid, title, artist, d.get("source", ""), dur).get("offset") or 0)
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
        r = API._genius("search/multi", per_page=5, q="%s %s" % ((artist or "").split(",")[0], t0))
    except Exception:
        return []
    hits = [h["result"] for sec in r.get("sections", []) if sec.get("type") == "song" for h in sec.get("hits", [])]
    na = _nzt((artist or "").split(",")[0])
    hit = next((h for h in hits if _nzt(h.get("title")).startswith(_nzt(t0)[:12]) and na[:6] in _nzt(h.get("artist_names"))
                and not re.search(r"remix|edit|live|version|translation|traduc", h.get("title", ""), re.I)), None)
    if not hit:
        return []
    try:
        req = urllib.request.Request(hit["url"], headers={"User-Agent": API._GENIUS_UA["User-Agent"]})
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
        res = API._lhttp("https://api.deezer.com/search?" + urlencode({"q": 'artist:"%s" track:"%s"' % (a, t), "limit": 3})).get("data") or []
        if not res:
            return {}
        d = API._lhttp("https://api.deezer.com/track/%s" % res[0]["id"])
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


def rhythm_part(x, dz, lines, gsec, genres, vact, L):
    """todo lo que depende del ritmo (tempo, golpes, bombo, tonalidad, secciones, energia)"""
    mono = x.mean(0)
    dur_s = x.shape[1] / an.SR
    r = an.rhythm(mono, (dz or {}).get("bpm"))
    k = an.key(mono)
    lvl, ex = an.energy_level(L, r["onsets"], r["envLow"], r["env"], r["bpm"])
    ch = [g["t0"] for g in gsec if g["label"] == "estribillo"] or chorus_times(lines)
    secs, ebar, reps = an.sections(r, k, dur_s, vact, ch)
    for sct in secs:   # etiqueta de Genius si cubre la mayor parte de la seccion
        best = max(gsec, key=lambda g: min(g["t1"], sct["t1"]) - max(g["t0"], sct["t0"]), default=None)
        if best and min(best["t1"], sct["t1"]) - max(best["t0"], sct["t0"]) > .4 * (sct["t1"] - sct["t0"]):
            sct["label"] = best["label"]
    d = {"bpm": round(r["bpm"], 2), "per": r["per"], "phase": r["phase"], "bar0": r["bar0"],
         "conf": round(r["conf"], 3), "four": round(r["four"], 3), "even": round(r["even"], 3),
         "key": {kk: k[kk] for kk in ("name", "camelot", "cam", "letter", "conf")},
         "energy": lvl, "energyInfo": ex, "sections": secs, "energyBars": np.round(ebar, 3).tolist(),
         "kick": r["kick"].tolist(), "kpow": r["kpow"].tolist(), "rep4": reps["rep4"], "rep8": reps["rep8"],
         "barStart": reps["start"], "family": family(r, r["bpm"], genres), "v": AN_VER}
    return d, r


def analyze(vid, title, artist, dur, sep=None, x=None):
    """Analisis de una cancion (una vez: JSON en cache). sep(x) -> instrumental (2, n): voz separada, opcional (el
    laboratorio); sin ella, donde canta sale de la letra sincronizada"""
    jp = os.path.join(CACHE, vid + ".json")
    with _lock("an:" + vid):
        d = _jload(jp) if os.path.isfile(jp) else None
        if d and (d.get("vocals") is not None or not sep):
            if d.get("v") == AN_VER:
                return d
            # analisis viejo: se rehace solo el ritmo (letra, Genius, Deezer y voz separada se reutilizan)
            x = load(vid) if x is None else x
            ip = os.path.join(CACHE, vid + "_inst.npy")
            vact = an.vocal_activity(x, np.load(ip).astype(np.float32))[0] if os.path.isfile(ip) else None
            d.update(rhythm_part(x, d.get("deezer"), d.get("lyrics") or [], d.get("gsections") or [],
                                 d.get("genres") or [], vact, d["lufs"])[0])
            d = json.loads(json.dumps(d, default=float))
            _jsave(jp, d)
            return d
        tm = {}
        t = time.time()
        x = load(vid) if x is None else x
        tm["descarga+decodificar"] = round(time.time() - t, 2)
        dz = deezer_info(title, artist)
        L = an.lufs(x)
        lines, lsrc = lyrics_lines(vid, title, artist, dur or x.shape[1] / an.SR)
        vocals, vact, sep_t = None, None, None
        if sep:
            t = time.time()
            inst = sep(x)
            sep_t = time.time() - t
            tm["voz separada"] = round(sep_t, 2)
            np.save(os.path.join(CACHE, vid + "_inst.npy"), inst.astype(np.float16))
            vact, _ = an.vocal_activity(x, inst)
            vocals = an.spans(vact)
        t = time.time()
        gsec = map_sections(genius_sections(title, artist), lines)
        tm["Genius"] = round(time.time() - t, 2)
        try:
            genres = API.mix_genre(title, artist).get("genres") or []
        except Exception:
            genres = []
        t = time.time()
        rd, r = rhythm_part(x, dz, lines, gsec, genres, vact, L)
        tm["ritmo/tono/secciones"] = round(time.time() - t, 2)
        e = r["env"]
        med = float(np.median(e))
        sec = np.convolve(e, np.ones(100) / 100, "same")
        on = np.where(sec > med * .5)[0]
        end = np.where(sec > med * .12)[0]
        dur_s = x.shape[1] / an.SR
        d = {"id": vid, "title": title, "artist": artist, "dur": round(dur_s, 2), "lufs": round(L, 1),
             "musicStart": round(float(on[0] / an.FPS) if len(on) else 0.0, 2),
             "musicEnd": round(float(end[-1] / an.FPS) if len(end) else dur_s, 2),
             "genres": genres, "gsections": gsec, "repeats": repeat_runs(lines), "deezer": dz,
             "vocals": vocals, "lyrics": lines, "lyricsSource": lsrc, "peaks": an.peaks(x),
             "times": tm, "sepXRT": round(dur_s / sep_t, 1) if sep_t else None}
        d.update(rd)
        d = json.loads(json.dumps(d, default=float))   # numpy -> tipos de JSON
        _jsave(jp, d)
        return d


# ------------------------------------------------------------------ transiciones (servidor de Wave)
def key(a, b):
    return hashlib.sha1(("%s>%s|%d|%d" % (a, b, ENGINE, AN_VER)).encode()).hexdigest()[:16]


def _encode(wav, out_base):
    """wav -> m4a (AAC 192k, ~1/8 del tamano); si ffmpeg falla, se queda en wav"""
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "wav", "-i", "-", "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart", out_base + ".m4a"], input=wav, capture_output=True, timeout=120,
                           creationflags=flags)
        if r.returncode == 0 and os.path.getsize(out_base + ".m4a") > 1000:
            return out_base + ".m4a"
    except Exception:
        pass
    with open(out_base + ".wav", "wb") as f:
        f.write(wav)
    return out_base + ".wav"


def _prune(keep=40):
    try:
        fs = sorted((os.path.getmtime(os.path.join(CACHE, f)), f) for f in os.listdir(CACHE)
                    if re.fullmatch(r"x_[0-9a-f]{16}\.(m4a|wav|json)", f))
        for _, f in fs[: max(0, len(fs) - 3 * keep)]:
            os.remove(os.path.join(CACHE, f))
    except Exception:
        pass


def _meta_path(k):
    return os.path.join(CACHE, "x_%s.json" % k)


def _build(k, a, b):
    with _work:
        t = time.time()
        A = analyze(a["id"], a.get("title", ""), a.get("artist", ""), a.get("dur") or 0)
        B = analyze(b["id"], b.get("title", ""), b.get("artist", ""), b.get("dur") or 0)
        ta = time.time() - t
        plans = planner.plan(A, B, stems=False)
        p = plans[0]
        xa, xb = load(a["id"]), load(b["id"])
        t = time.time()
        wav, marks, info = mix.render(A, B, {"A": {"mix": xa, "inst": None}, "B": {"mix": xb, "inst": None}}, p,
                                      stems=False, pre=1.5, post=2.5, viz=False)
        tr = time.time() - t
        fp = _encode(wav, os.path.join(CACHE, "x_" + k))
        meta = {"state": "ready", "key": k, "a": a["id"], "b": b["id"], "tpl": p["tpl"], "label": p["label"],
                "why": p["why"][:3], "fmt": os.path.splitext(fp)[1][1:], "marks": marks,
                "secs": {"analisis": round(ta, 1), "render": round(tr, 1)}}
        meta.update({kk: info[kk] for kk in ("t0", "dur", "speed", "sw", "win", "ts", "te", "ramp", "bIn", "bEnd")})
        _jsave(_meta_path(k), json.loads(json.dumps(meta, default=float)))
        _prune()
        return meta


def prep(a, b):
    """Estado de la transicion a -> b; si no esta ni en marcha, la pone en marcha (en segundo plano)"""
    k = key(a["id"], b["id"])
    mp = _meta_path(k)
    if os.path.isfile(mp):
        try:
            return _jload(mp)
        except Exception:
            pass
    with _jlock:
        j = _jobs.get(k)
        if j and (j["state"] == "working" or time.time() - j["t"] < 120):
            return {"state": j["state"], "key": k, "error": j.get("err")}
        _jobs[k] = {"state": "working", "t": time.time()}

    def run():
        try:
            _build(k, a, b)
            with _jlock:
                _jobs.pop(k, None)
        except Exception as e:
            with _jlock:
                _jobs[k] = {"state": "error", "t": time.time(), "err": str(e)[:200]}
    threading.Thread(target=run, daemon=True).start()
    return {"state": "working", "key": k}


def clip_path(k):
    if not re.fullmatch(r"[0-9a-f]{16}", k or ""):
        return None
    for ext in ("m4a", "wav"):
        fp = os.path.join(CACHE, "x_%s.%s" % (k, ext))
        if os.path.isfile(fp):
            return fp
    return None
