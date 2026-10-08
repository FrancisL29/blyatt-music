"""Modo karaoke (app de escritorio).

El PC es el escenario: procesa cada cancion (voz fuera con IA) y la reproduce con la letra palabra a palabra;
los moviles de la misma red se unen escaneando un QR, eligen canciones y ven la cola y el ranking.

Flujo de cada cancion (pensado para que nadie espere):
  1. al anadirla a la cola se baja el audio y la letra en segundo plano (cache de Blyatt)
  2. separacion voz/instrumental con MDX-Net (UVR-MDX-NET-Inst_HQ_5, ONNX) en la GPU via DirectML (CPU si no
     hay): ~7x tiempo real en una RX 550. Se procesa en trozos de ~5.7 s EN ORDEN y cada trozo se puede
     reproducir en cuanto sale -> la cancion empieza a sonar a los pocos segundos aunque no haya terminado
  3. de la voz separada sale la melodia de referencia (YIN, 50 fps) con la que se puntua al cantante
Un solo hilo trabaja por prioridad (la que suena > la siguiente > ...): si cambia el orden de la cola, el
trabajo en curso se aparca y se sigue luego (cada cancion es un generador que avanza trozo a trozo).

El servidor para los moviles es aparte (escucha en la red local) y SOLO tiene las rutas del karaoke:
nada de la cuenta, la biblioteca ni el resto de la app queda expuesto en la red.
"""
import hashlib
import json
import math
import os
import random
import re
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP = None   # app.py se inyecta aqui (busqueda, letra, descarga de audio, rutas de datos)

SR = 44100
MODEL = {   # UVR-MDX-NET-Inst_HQ_5 (parametros de model_data.json de UVR para este hash)
    "file": "UVR-MDX-NET-Inst_HQ_5.onnx",
    "url": "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR-MDX-NET-Inst_HQ_5.onnx",
    "size": 59074342,
    "sha256": "811cb24095d865763752310848b7ec86aeede0626cb05749ab35350e46897000",
    "n_fft": 5120, "dim_f": 2560, "dim_t": 256, "hop": 1024, "comp": 1.01,
}
_CHUNK = MODEL["hop"] * (MODEL["dim_t"] - 1)   # 261120 muestras por pasada del modelo
_TRIM = MODEL["n_fft"] // 2                    # bordes de cada pasada que se descartan
_XF = 4096                                     # fundido entre pasadas (sin costuras audibles)
STEP = _CHUNK - 2 * _TRIM - _XF                # 251904 muestras (~5.7 s) = un trozo reproducible
PITCH_HOP = 882                                # 20 ms a 44.1 kHz: un valor de melodia por trozo de 20 ms
MAX_SECS = 12 * 60
CACHE_MB = 2048
EMOJI = ("👏", "🔥", "❤️", "😂", "🎉", "😮")
COLORS = ("#ff375f", "#ff9f0a", "#ffd60a", "#30d158", "#64d2ff", "#0a84ff", "#5e5ce6", "#bf5af2", "#ff6482", "#66d4cf")


def _dir(*p):
    d = os.path.join(APP.DATA, *p)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- motor de separacion (MDX-Net en ONNX)
ENG = {"status": "idle", "progress": 0, "gpu": False, "error": "", "speed": 0}
_SEP = None


class _Sep:
    def __init__(self, path):
        import numpy as np
        import onnxruntime as ort
        self.np = np
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.enable_mem_pattern = False   # requisito de DirectML
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        provs = ["CPUExecutionProvider"]
        if "DmlExecutionProvider" in ort.get_available_providers():
            provs.insert(0, "DmlExecutionProvider")
        try:
            self.s = ort.InferenceSession(path, so, providers=provs)
        except Exception:   # GPU sin DirectX 12 / driver roto: CPU
            self.s = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
        self.gpu = self.s.get_providers()[0] == "DmlExecutionProvider"
        self.inp = self.s.get_inputs()[0].name
        n = MODEL["n_fft"]
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)   # hann periodica (torch)
        T = MODEL["dim_t"]
        hop = MODEL["hop"]
        k = n // hop   # 5 bloques de salto por ventana: el solapamiento-suma se hace con 5 sumas desplazadas
        w2 = (self.win ** 2).reshape(k, hop)
        env = np.zeros(n + hop * (T - 1), np.float32)
        for j in range(k):
            env[j * hop: j * hop + T * hop] += np.tile(w2[j], T)
        self.env = env[n // 2: n // 2 + _CHUNK]
        self.idx = np.arange(n)[None, :] + hop * np.arange(T)[:, None]

    def run(self, seg):
        """seg: [2, 261120] float32 (mezcla) -> instrumental [2, 261120] (STFT/ISTFT identicas a torch)."""
        np = self.np
        n, hop, F, T = MODEL["n_fft"], MODEL["hop"], MODEL["dim_f"], MODEL["dim_t"]
        p = n // 2
        xp = np.pad(seg, ((0, 0), (p, p)), mode="reflect")
        S = np.fft.rfft(xp[:, self.idx] * self.win, axis=-1)[:, :, :F]   # [2, T, F]
        x = np.empty((1, 4, F, T), np.float32)
        x[0, 0], x[0, 1] = S[0].real.T, S[0].imag.T
        x[0, 2], x[0, 3] = S[1].real.T, S[1].imag.T
        x[:, :, :3, :] = 0   # UVR anula las 3 bandas mas graves a la entrada
        y = self.s.run(None, {self.inp: x})[0][0]
        C = np.zeros((2, T, n // 2 + 1), np.complex64)
        C[0, :, :F] = (y[0] + 1j * y[1]).T
        C[1, :, :F] = (y[2] + 1j * y[3]).T
        fr = (np.fft.irfft(C, n=n, axis=-1) * self.win).astype(np.float32).reshape(2, T, n // hop, hop)
        out = np.zeros((2, n + hop * (T - 1)), np.float32)
        for j in range(n // hop):
            out[:, j * hop: j * hop + T * hop] += fr[:, :, j, :].reshape(2, T * hop)
        return out[:, p: p + _CHUNK] / self.env * MODEL["comp"]


def _model_path():
    return os.path.join(_dir("models"), MODEL["file"])


def _download_model(fp):
    part = fp + ".part"
    h = hashlib.sha256()
    req = urllib.request.Request(MODEL["url"], headers={"User-Agent": "Blyatt"})
    with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
        got = 0
        while True:
            b = r.read(1 << 18)
            if not b:
                break
            f.write(b)
            h.update(b)
            got += len(b)
            ENG["progress"] = min(99, int(got * 100 / MODEL["size"]))
            _bump(soft=True)
    if h.hexdigest() != MODEL["sha256"]:
        os.remove(part)
        raise IOError("el modelo descargado no coincide (sha256)")
    os.replace(part, fp)


def _engine():
    """Descarga (una vez, ~59 MB) y carga el modelo; una pasada de calentamiento compila los kernels de la GPU
    para que la primera cancion no lo pague."""
    global _SEP
    if _SEP:
        return _SEP
    import numpy as np
    try:
        fp = _model_path()
        if not os.path.isfile(fp) or os.path.getsize(fp) != MODEL["size"]:
            ENG.update(status="download", progress=0)
            _bump()
            _download_model(fp)
        ENG.update(status="loading", progress=100)
        _bump()
        sep = _Sep(fp)
        t = time.time()
        sep.run(np.zeros((2, _CHUNK), np.float32))
        sep.run(np.zeros((2, _CHUNK), np.float32))
        ENG.update(status="ready", gpu=sep.gpu, speed=round(STEP / SR / max(.05, (time.time() - t) / 2), 1))
        _SEP = sep
    except Exception as e:
        ENG.update(status="error", error=str(e)[:200])
        _bump()
        raise
    _bump()
    return _SEP


# ---------------------------------------------------------------- melodia de referencia (YIN vectorizado)
_PD = 3                      # la voz se analiza a 14.7 kHz (44.1 / 3): sobra para fundamentales < 1.1 kHz
_PH = PITCH_HOP // _PD       # 294
_YW, _TMIN, _TMAX = 512, 13, 210   # ventana 35 ms; 70..1130 Hz


def _yin(np, vd, md, f0, f1):
    """Frames [f0, f1) -> MIDI*10 (0 = sin voz). vd/md: voz y mezcla mono a 14.7 kHz (song completa)."""
    if f1 <= f0:
        return []
    c = np.arange(f0, f1) * _PH
    idx = np.clip(c[:, None] - _YW // 2 + np.arange(_YW + _TMAX)[None, :], 0, len(vd) - 1)
    X = vd[idx].astype(np.float64)
    x0 = X[:, :_YW]
    r = np.fft.irfft(np.fft.rfft(X, 1024) * np.conj(np.fft.rfft(x0, 1024)), 1024)[:, :_TMAX + 1]
    ec = np.concatenate([np.zeros((len(X), 1)), np.cumsum(X * X, axis=1)], axis=1)
    tau = np.arange(_TMAX + 1)
    d = ec[:, _YW][:, None] + (ec[:, tau + _YW] - ec[:, tau]) - 2 * r
    d[:, 0] = 0
    cs = np.cumsum(d[:, 1:], axis=1)
    cm = np.ones_like(d)
    cm[:, 1:] = d[:, 1:] * tau[1:] / np.maximum(cs, 1e-12)
    cm[:, :_TMIN] = 9
    below = cm[:, :_TMAX] < .15
    has = below.any(axis=1)
    t = np.where(has, below.argmax(axis=1), cm[:, :_TMAX].argmin(axis=1))
    rows = np.arange(len(X))
    for _ in range(12):   # bajar hasta el minimo local
        nxt = (t + 1 < _TMAX) & (cm[rows, np.minimum(t + 1, _TMAX)] < cm[rows, t])
        t = t + nxt
    best = cm[rows, t]
    a, b, cc = cm[rows, np.maximum(t - 1, 1)], best, cm[rows, np.minimum(t + 1, _TMAX)]
    den = a - 2 * b + cc
    delta = np.clip(np.where(np.abs(den) > 1e-9, (a - cc) / (2 * np.where(den == 0, 1, den)), 0), -1, 1)
    f = (SR / _PD) / np.maximum(t + delta, 1)
    rv = np.sqrt((x0 * x0).mean(axis=1))
    M = md[idx[:, :_YW]]
    rm = np.sqrt((M * M).mean(axis=1))
    ok = (best < .3) & (rv > .012) & (rv > .18 * rm) & (f > 70) & (f < 1100)
    midi = 69 + 12 * np.log2(np.maximum(f, 1) / 440)
    return [int(round(m * 10)) if v else 0 for m, v in zip(midi.tolist(), ok.tolist())]


# ---------------------------------------------------------------- canciones (cache en disco + procesado)
SONGS = {}   # vid -> estado publico de su procesado
_GENS = {}   # vid -> generador en curso
_PREF = {}   # vid -> hilo de precarga de audio/letra


def _song_dir(vid):
    return os.path.join(_dir("karaoke"), vid)


def _song(vid, info=None):
    s = SONGS.get(vid)
    if s is None:
        s = {"vid": vid, "status": "wait", "progress": 0, "chunks": 0, "nch": 0, "n": 0, "dur": 0,
             "pitch": [], "lyrics": None, "error": "", "title": "", "artist": "", "catdur": 0}
        meta = os.path.join(_song_dir(vid), "meta.json")
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
            if m.get("step") == STEP and m.get("model") == MODEL["file"] and all(
                    os.path.isfile(os.path.join(_song_dir(vid), "%03d.pcm" % i)) for i in range(m["nch"])):
                s.update(m, status="ready", progress=100, chunks=m["nch"])
                os.utime(meta)
        except (OSError, ValueError, KeyError):
            pass
        SONGS[vid] = s
    if info:
        s["title"] = s["title"] or (info.get("title") or "")[:200]
        s["artist"] = s["artist"] or (info.get("artist") or "")[:200]
        s["catdur"] = s["catdur"] or _secs(info.get("duration"))
    return s


def _secs(v):
    try:
        if isinstance(v, (int, float)):
            return float(v)
        return float(sum(int(x) * 60 ** i for i, x in enumerate(reversed(str(v).split(":")))))
    except (ValueError, TypeError):
        return 0.0


def _get_audio(vid):
    fp = APP._audio_path(vid)
    with APP._id_lock("dl", vid):
        if os.path.isfile(fp) and not APP._mp4_ok(fp):
            os.remove(fp)
        if os.path.isfile(fp):
            os.utime(fp)
        else:
            APP.audio_fetch(vid)
    return fp


def _get_lyrics(s):
    if s["lyrics"] is None:
        try:
            s["lyrics"] = APP.lyrics(s["title"], s["artist"], s["catdur"] or s["dur"], s["vid"])
        except Exception:
            s["lyrics"] = {"lines": []}
        _bump(soft=True)
    return s["lyrics"]


def _prefetch(vid):
    """Al anadirla: audio + letra en segundo plano (la separacion la hace el hilo de trabajo por turno)."""
    s = SONGS[vid]
    if vid in _PREF and _PREF[vid].is_alive():
        return

    def run():
        if s["status"] != "ready":   # ya procesada (cache): solo falta la letra
            try:
                _get_audio(vid)
            except Exception:
                pass
        _get_lyrics(s)
    th = threading.Thread(target=run, daemon=True)
    _PREF[vid] = th
    th.start()


def _decode(np, fp):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", fp, "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                       capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise IOError("no se pudo decodificar el audio")
    return np.frombuffer(r.stdout, np.float32).reshape(-1, 2).T.copy()


def _job(vid):
    """Procesa una cancion trozo a trozo; cada `yield` es un punto donde el hilo puede cambiar de trabajo."""
    import numpy as np
    s = SONGS[vid]
    d = _song_dir(vid)
    os.makedirs(d, exist_ok=True)
    s.update(status="download", progress=0, error="")
    _bump(soft=True)
    yield
    fp = _get_audio(vid)
    mix = _decode(np, fp)
    N = mix.shape[1]
    if N < SR * 5 or N > SR * MAX_SECS:
        raise IOError("duración no válida para karaoke")
    nch = -(-N // STEP)
    s.update(n=N, dur=round(N / SR, 3), nch=nch, chunks=0, pitch=[], status="sep")
    threading.Thread(target=_get_lyrics, args=(s,), daemon=True).start()
    _bump()
    yield
    sep = _engine()
    pad = np.zeros((2, _TRIM + N + _CHUNK), np.float32)
    pad[:, _TRIM:_TRIM + N] = mix
    vd = np.zeros(nch * STEP // _PD + 1, np.float32)   # voz y mezcla mono a 14.7 kHz para la melodia
    md = np.zeros_like(vd)
    ramp = np.linspace(0, 1, _XF, dtype=np.float32)
    tail = None
    pf = 0   # frames de melodia ya calculados
    nfr = -(-N // PITCH_HOP)
    for k in range(nch):
        a = k * STEP
        seg = pad[:, a: a + _CHUNK]
        if float(np.abs(seg).max()) < 1e-4:   # silencio: no hace falta el modelo
            inst = np.zeros((2, _CHUNK - 2 * _TRIM), np.float32)
        else:
            inst = sep.run(seg)[:, _TRIM: _CHUNK - _TRIM]
        if tail is not None:   # fundido con el final de la pasada anterior
            inst[:, :_XF] = tail * (1 - ramp) + inst[:, :_XF] * ramp
        tail = inst[:, STEP: STEP + _XF].copy()
        n = min(STEP, N - a)
        m = mix[:, a: a + n]
        ins = inst[:, :n]
        voc = (m - ins).mean(axis=0)
        planes = np.concatenate([ins[0], ins[1], voc])
        with open(os.path.join(d, "%03d.pcm" % k), "wb") as f:
            f.write((np.clip(planes, -1, 1) * 32767).astype("<i2").tobytes())
        # melodia: los frames cuya ventana ya esta completa
        def dec(x):
            x = x[: len(x) // _PD * _PD]
            return x.reshape(-1, _PD).mean(axis=1)
        vd[a // _PD: a // _PD + n // _PD] = dec(voc)
        md[a // _PD: a // _PD + n // _PD] = dec(m.mean(axis=0))
        upto = nfr if k == nch - 1 else max(pf, ((a + n) // _PD - _YW - _TMAX) // _PH)
        s["pitch"].extend(_yin(np, vd, md, pf, min(upto, nfr)))
        pf = min(upto, nfr)
        s.update(chunks=k + 1, progress=int((k + 1) * 100 / nch))
        _bump(soft=True)
        yield
    meta = {k2: s[k2] for k2 in ("n", "dur", "nch", "pitch", "title", "artist", "catdur")}
    meta.update(step=STEP, sr=SR, model=MODEL["file"])
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    s.update(status="ready", progress=100)
    _get_lyrics(s)
    _bump()
    _evict()


def _evict():
    root = _dir("karaoke")
    keep = {q["vid"] for q in S["queue"]} | ({S["now"]["vid"]} if S["now"] else set())
    dirs = []
    for v in os.listdir(root):
        p = os.path.join(root, v)
        try:
            sz = sum(os.path.getsize(os.path.join(p, f)) for f in os.listdir(p))
            dirs.append((os.path.getmtime(os.path.join(p, "meta.json")) if os.path.isfile(os.path.join(p, "meta.json")) else 0, sz, v, p))
        except OSError:
            pass
    total = sum(x[1] for x in dirs)
    for _, sz, v, p in sorted(dirs):
        if total <= CACHE_MB * 1024 * 1024:
            break
        if v in keep or v in _GENS:
            continue
        shutil.rmtree(p, ignore_errors=True)
        SONGS.pop(v, None)
        total -= sz


def _order():
    """Prioridad: la que suena, luego la cola en orden."""
    out = []
    if S["now"]:
        out.append(S["now"]["vid"])
    out += [q["vid"] for q in S["queue"]]
    seen = set()
    return [v for v in out if not (v in seen or seen.add(v))]


_WAKE = threading.Event()
_WORKER = None


def _worker():
    while True:
        if not S["on"]:
            _WAKE.wait(5)
            _WAKE.clear()
            continue
        if not _SEP:   # la carga del modelo retiene el GIL unos segundos: primero que la sala se pinte
            time.sleep(1.5)
        try:
            _engine()   # preparar el motor en cuanto se abre el karaoke (antes de que nadie elija cancion)
        except Exception:
            for v in _order():   # sin motor no hay pista sin voz: que no se queden esperando
                if SONGS.get(v) and SONGS[v]["status"] not in ("ready", "error"):
                    SONGS[v].update(status="error", error="motor de voz no disponible")
            _bump()
            _WAKE.wait(30)
            _WAKE.clear()
            continue
        todo = [v for v in _order() if SONGS.get(v) and SONGS[v]["status"] not in ("ready", "error")]
        if not todo:
            _WAKE.wait(5)
            _WAKE.clear()
            continue
        vid = todo[0]
        g = _GENS.get(vid)
        if g is None:
            g = _GENS[vid] = _job(vid)
        try:
            next(g)
        except StopIteration:
            _GENS.pop(vid, None)
        except Exception as e:
            _GENS.pop(vid, None)
            SONGS[vid].update(status="error", error=str(e)[:160])
            _bump()
        # mientras alguien canta, lo que va detras se procesa con pausas: la GPU/CPU tambien pinta la pantalla
        now = S["now"]
        if now and now.get("phase") == "sing" and now["vid"] != vid:
            time.sleep(.25)


# ---------------------------------------------------------------- sesion (estado compartido PC <-> moviles)
S = {"on": False, "code": "", "ip": "", "port": 0, "v": 0, "players": {}, "queue": [], "now": None,
     "history": [], "live": None, "reacts": [], "qid": 0, "rid": 0}
_COND = threading.Condition()
_LAST_SOFT = [0.0]
_SRV = None


def _bump(soft=False):
    """Nueva version del estado: despierta a quien espera (long-poll). Los avances de procesado (soft) se
    agrupan para no despertar a todos los moviles por cada trozo."""
    with _COND:
        now = time.time()
        if soft and now - _LAST_SOFT[0] < .5:
            return
        _LAST_SOFT[0] = now
        S["v"] += 1
        _COND.notify_all()


def _lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(("10.255.255.255", 1))   # no envia nada: solo elige la interfaz de salida
            ip = so.getsockname()[0]
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if re.match(r"^(192\.168|10\.|172\.(1[6-9]|2\d|3[01]))", ip):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def url():
    return "http://%s:%d/?r=%s" % (S["ip"], S["port"], S["code"])


def qr_svg():
    try:
        import segno
        return segno.make(url(), error="m").svg_inline(omitsize=True, border=0, dark="#000", light=None)
    except Exception:
        return ""


def _pub():
    def song(q):
        s = SONGS.get(q["vid"]) or {}
        return dict(q, status=s.get("status", "wait"), progress=s.get("progress", 0), error=s.get("error", ""))
    pl = []
    for p in S["players"].values():
        pl.append({k: p[k] for k in ("id", "name", "color", "total", "best", "songs")})
    pl.sort(key=lambda p: (-p["total"], -p["best"], p["name"].lower()))
    now = None
    if S["now"]:
        now = song(S["now"])
        s = SONGS.get(now["vid"]) or {}
        now["dur"] = s.get("dur") or now.get("dur")
    return {"v": S["v"], "on": S["on"], "code": S["code"], "url": url() if S["on"] else "",
            "engine": dict(ENG), "players": pl, "queue": [song(q) for q in S["queue"]], "now": now,
            "live": S["live"], "history": S["history"][-30:], "reacts": S["reacts"][-12:]}


def wait_state(v, timeout=25.0):
    with _COND:
        _COND.wait_for(lambda: S["v"] != v, timeout)
        return _pub()


def open_session():
    global _SRV, _WORKER
    if not S["on"]:
        S.update(on=True, code="".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4)),
                 players={}, queue=[], now=None, history=[], live=None, reacts=[])
        S["ip"] = _lan_ip()
        for port in list(range(8765, 8776)) + [0]:
            try:
                _SRV = _LanServer(("0.0.0.0", port), _Guest)
                break
            except OSError:
                continue
        S["port"] = _SRV.server_address[1]
        threading.Thread(target=_SRV.serve_forever, daemon=True).start()
    if not _WORKER:
        _WORKER = threading.Thread(target=_worker, daemon=True)
        _WORKER.start()
    _WAKE.set()
    _bump()
    return dict(_pub(), qr=qr_svg())


def close_session():
    global _SRV
    S.update(on=False, now=None, queue=[], live=None)
    _GENS.clear()   # lo a medio procesar se suelta (memoria); al volver se rehace desde el principio
    for s in SONGS.values():
        if s["status"] not in ("ready", "error"):
            s["status"] = "wait"
    if _SRV:
        srv, _SRV = _SRV, None
        threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
    _bump()
    return {"ok": True}


def _player(key):
    return next((p for p in S["players"].values() if p["key"] == key), None) if key else None


def join(name, key):
    name = re.sub(r"\s+", " ", (name or "").strip())[:24]
    if not name:
        return {"error": "Escribe tu nombre"}
    p = _player(key)
    if not p:
        if len(S["players"]) >= 40:
            return {"error": "La sala está llena"}
        used = {x["color"] for x in S["players"].values()}
        pid = os.urandom(4).hex()
        p = {"id": pid, "key": os.urandom(12).hex(), "name": name, "total": 0, "best": 0, "songs": 0,
             "color": next((c for c in COLORS if c not in used), random.choice(COLORS))}
        S["players"][pid] = p
    p["name"] = name
    _bump()
    return {"id": p["id"], "key": p["key"], "name": p["name"], "color": p["color"]}


def add(by, song):
    vid = str(song.get("id") or "")
    if not re.fullmatch(r"[\w-]{6,20}", vid):
        return {"error": "Canción no válida"}
    if len(S["queue"]) >= 60:
        return {"error": "La cola está llena"}
    if by and sum(1 for q in S["queue"] if q["by"] == by) >= 6:
        return {"error": "Ya tienes 6 canciones en la cola"}
    S["qid"] += 1
    q = {"qid": S["qid"], "vid": vid, "title": str(song.get("title") or "")[:200],
         "artist": str(song.get("artist") or "")[:200], "cover": str(song.get("cover") or "")[:500],
         "dur": _secs(song.get("duration")), "by": by}
    if q["cover"] and not q["cover"].startswith("https://"):
        q["cover"] = ""
    S["queue"].append(q)
    _song(vid, song)
    _prefetch(vid)
    _WAKE.set()
    _bump()
    return {"ok": True, "qid": q["qid"], "pos": len(S["queue"])}


def remove(qid, by=None):
    n = len(S["queue"])
    S["queue"] = [q for q in S["queue"] if not (q["qid"] == qid and (by is None or q["by"] == by))]
    if len(S["queue"]) == n:
        return {"error": "No se pudo quitar"}
    _bump()
    return {"ok": True}


def move(qid, to):
    q = next((x for x in S["queue"] if x["qid"] == qid), None)
    if not q:
        return {"error": "No está en la cola"}
    S["queue"].remove(q)
    S["queue"].insert(max(0, min(int(to), len(S["queue"]))), q)
    _WAKE.set()
    _bump()
    return {"ok": True}


def react(by, e):
    if e not in EMOJI:
        return {"error": "?"}
    S["rid"] += 1
    S["reacts"] = (S["reacts"] + [{"id": S["rid"], "e": e, "by": by}])[-20:]
    _bump()
    return {"ok": True}


def next_song():
    S["now"] = None
    S["live"] = None
    if S["queue"]:
        q = S["queue"].pop(0)
        S["now"] = dict(q, phase="intro", score=None)
    _WAKE.set()
    _bump()
    return _pub()


def set_phase(phase):
    if S["now"] and phase in ("intro", "sing", "paused", "results"):
        S["now"]["phase"] = phase
        _bump()
    return {"ok": True}


def live(score, rating):
    if S["now"]:
        S["live"] = {"qid": S["now"]["qid"], "score": int(score or 0), "rating": str(rating or "")[:24]}
        _bump()
    return {"ok": True}


def result(score):
    now = S["now"]
    if not now:
        return {"error": "Nada sonando"}
    now["phase"] = "results"
    sc = None if score is None else max(0, min(10000, int(score)))
    now["score"] = sc
    p = S["players"].get(now["by"])
    if p and sc is not None:
        p["total"] += sc
        p["best"] = max(p["best"], sc)
        p["songs"] += 1
    S["history"].append({"qid": now["qid"], "title": now["title"], "artist": now["artist"], "cover": now["cover"],
                         "by": now["by"], "score": sc, "t": int(time.time())})
    S["live"] = None
    _bump()
    return _pub()


def song_info(vid, frm=0):
    s = SONGS.get(vid)
    if not s:
        return {"error": "desconocida"}
    frm = max(0, int(frm or 0))
    if s["lyrics"] is None and s["title"]:
        _prefetch(vid)
    return {k: s[k] for k in ("status", "progress", "chunks", "nch", "n", "dur", "error")} | {
        "sr": SR, "step": STEP, "hop": PITCH_HOP, "from": frm, "pitch": s["pitch"][frm:],
        "lyrics": s["lyrics"], "gpu": ENG["gpu"]}


def pcm(vid, n):
    s = SONGS.get(vid)
    if not s or n >= s["chunks"]:
        return None
    with open(os.path.join(_song_dir(vid), "%03d.pcm" % n), "rb") as f:
        return f.read()


# ---------------------------------------------------------------- servidor de la red local (moviles)
class _LanServer(ThreadingHTTPServer):
    allow_reuse_address = False   # en Windows permitiria a dos procesos escuchar en el mismo puerto
    daemon_threads = True

    def server_bind(self):   # sin socket.getfqdn("0.0.0.0"): una busqueda DNS inversa que tarda segundos
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "blyatt-karaoke", self.server_address[1]


class _Guest(BaseHTTPRequestHandler):
    def _out(self, body, ctype="application/json", status=200):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok_room(self, qs):
        return S["on"] and (qs.get("r") or [""])[0].upper() == S["code"]

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(APP.BASE, "kara.html"), "rb") as f:
                return self._out(f.read(), "text/html; charset=utf-8")
        if u.path in ("/favicon.ico", "/icon.png"):
            with open(os.path.join(APP.BASE, "assets", "icon-192.png"), "rb") as f:
                return self._out(f.read(), "image/png")
        if not self._ok_room(qs):
            return self._out({"error": "room"}, status=403)
        if u.path == "/k/state":
            try:
                v = int((qs.get("v") or ["-1"])[0])
            except ValueError:
                v = -1
            return self._out(wait_state(v, 20))
        if u.path == "/k/search":
            q = (qs.get("q") or [""])[0].strip()[:120]
            if not q:
                return self._out([])
            try:
                res = APP.search(q, "songs")
            except Exception:
                return self._out({"error": "No se pudo buscar"}, status=502)
            return self._out([{k: x.get(k) for k in ("id", "title", "artist", "cover", "duration", "explicit")}
                              for x in (res if isinstance(res, list) else [])][:25])
        return self._out({"error": "?"}, status=404)

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._ok_room(qs):
            return self._out({"error": "room"}, status=403)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 16384:
            return self._out({"error": "demasiado grande"}, status=413)
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        if u.path == "/k/join":
            return self._out(join(d.get("name"), d.get("key")))
        p = _player(d.get("key"))
        if not p:
            return self._out({"error": "join"}, status=401)
        if u.path == "/k/add":
            return self._out(add(p["id"], d.get("song") or {}))
        if u.path == "/k/remove":
            return self._out(remove(int(d.get("qid") or 0), p["id"]))
        if u.path == "/k/react":
            return self._out(react(p["id"], d.get("e")))
        return self._out({"error": "?"}, status=404)

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------- rutas del PC (servidor local de la app)
def host_api(method, path, qs, d):
    g = lambda k, dv="": (qs.get(k) or [dv])[0]
    if path == "/kara/open":
        return open_session()
    if path == "/kara/close":
        return close_session()
    if path == "/kara/state":
        try:
            v = int(g("v", "-1"))
        except ValueError:
            v = -1
        return wait_state(v, 20) if v >= 0 else _pub()
    if path == "/kara/song":
        return song_info(g("id"), g("from", "0"))
    if path == "/kara/next":
        return next_song()
    if path == "/kara/phase":
        return set_phase(d.get("phase"))
    if path == "/kara/live":
        return live(d.get("score"), d.get("rating"))
    if path == "/kara/result":
        return result(d.get("score"))
    if path == "/kara/remove":
        return remove(int(d.get("qid") or 0))
    if path == "/kara/move":
        return move(int(d.get("qid") or 0), d.get("to") or 0)
    if path == "/kara/add":   # el PC tambien puede anadir (sin cantante: "Invitado")
        return add("", d.get("song") or {})
    return {"error": "?"}
