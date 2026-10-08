"""Modo karaoke (app de escritorio).

El PC es el escenario: procesa cada cancion (voz fuera con IA) y la reproduce con la letra palabra a palabra;
los moviles de la misma red se unen escaneando un QR, eligen canciones, ven la cola y el ranking, mandan
reacciones y (si quieren) hacen de micro inalambrico y mando a distancia mientras cantan.

Flujo de cada cancion (pensado para que nadie espere y nada se trabe):
  1. al anadirla a la cola se baja el audio y la letra en segundo plano (cache de Blyatt)
  2. separacion voz/instrumental con MDX-Net (UVR-MDX-NET-Inst_HQ_5, ONNX) en trozos de ~5.7 s que se pueden
     reproducir en cuanto salen. La GPU (DirectML) va a ~5x tiempo real pero bloquea la pantalla mientras
     trabaja (medido: la letra y el fondo caen a ~10 fps), asi que SOLO se usa cuando nadie canta; mientras
     alguien canta se sigue en la CPU en un proceso aparte con prioridad IDLE (medido: 60 fps intactos)
  3. de la voz separada sale la melodia de referencia (YIN, 50 fps) con la que se puntua al cantante
  4. si la letra no viene palabra a palabra, se alinea aqui con la voz separada (wav2vec2 CTC, forzado):
     por linea si la letra trae tiempos por linea, o la cancion entera si es solo texto
Un solo hilo trabaja por prioridad (la que suena > la siguiente > ...): cada cancion es un generador que
avanza paso a paso, asi un cambio en la cola aparca lo que se estaba haciendo y se retoma luego.

Los moviles usan un servidor aparte en la red local que SOLO tiene las rutas del karaoke (nada de la cuenta,
la biblioteca ni el resto de la app). Hay dos puertas: http (entrar sin avisos) y https con un certificado
propio (el navegador del movil solo deja usar el micro en https: avisa una vez por ser un certificado local).
"""
import hashlib
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import time
import unicodedata
import urllib.request
from collections import deque
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
_HF = "https://huggingface.co/Xenova/wav2vec2-base-960h/resolve/a19f851b3d42865797e410752b4c570c871e4825/onnx/"
W2V = {   # facebook/wav2vec2-base-960h (Apache-2.0) en ONNX: fp16 para la GPU, int8 para la CPU
    "gpu": {"file": "wav2vec2-base-960h_fp16.onnx", "url": _HF + "model_fp16.onnx", "size": 189192204,
            "sha256": "378348ee38b739cc77e77e3fb8502f0f40ed53da0dbf7401b24c25c8fafe03de"},
    "cpu": {"file": "wav2vec2-base-960h_int8.onnx", "url": _HF + "model_int8.onnx", "size": 95286006,
            "sha256": "a4249bb7b7bcbe391be19980922b0523c0907e976a9e5d5aabfadb40fcea5058"},
}
W2V_CHARS = "|ETAONIHSRDLUMWCFGYPBVK'XJQZ"   # indices 4.. del vocabulario (0 = blanco CTC, 4 = espacio)
ALIGN_BIAS = .08   # medido contra letras palabra a palabra reales: el CTC marca el inicio ~80 ms tarde
_CHUNK = MODEL["hop"] * (MODEL["dim_t"] - 1)   # 261120 muestras por pasada del modelo
_TRIM = MODEL["n_fft"] // 2                    # bordes de cada pasada que se descartan
_XF = 4096                                     # fundido entre pasadas (sin costuras audibles)
STEP = _CHUNK - 2 * _TRIM - _XF                # 251904 muestras (~5.7 s) = un trozo reproducible
PITCH_HOP = 882                                # 20 ms a 44.1 kHz: un valor de melodia por trozo de 20 ms
MAX_SECS = 12 * 60
CACHE_MB = 2048
EMOJI = ("👏", "🔥", "❤️", "😂", "🎉", "😮")
COLORS = ("#ff375f", "#ff9f0a", "#ffd60a", "#30d158", "#64d2ff", "#0a84ff", "#5e5ce6", "#bf5af2", "#ff6482", "#66d4cf")
DIFFS = ("easy", "normal", "hard")


def _dir(*p):
    d = os.path.join(APP.DATA, *p)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- motores (separacion + alineado) en ONNX
ENG = {"status": "idle", "progress": 0, "gpu": False, "error": "", "speed": 0, "cpu_speed": .5, "align": ""}
_GPU = None   # separador en la GPU de este proceso (None: no hay DirectML usable)
_CPU = None   # (proceso, conexion): separador/alineador en CPU con prioridad IDLE
_ALG = None   # alineador en la GPU


def _session(path, gpu):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if gpu:
        so.enable_mem_pattern = False   # requisito de DirectML
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return ort.InferenceSession(path, so, providers=["DmlExecutionProvider"])
    so.intra_op_num_threads = os.cpu_count() or 4
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")   # sin espera activa: no roba CPU
    return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])


class _Sep:
    def __init__(self, sess):
        import numpy as np
        self.np, self.s = np, sess
        self.inp = sess.get_inputs()[0].name
        n = MODEL["n_fft"]
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)   # hann periodica (torch)
        T, hop = MODEL["dim_t"], MODEL["hop"]
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


def _emis(sess, x):
    """Voz a 16 kHz -> log-probabilidades CTC [frames de 20 ms, 32]."""
    import numpy as np
    x = (x - x.mean()) / (x.std() + 1e-7)
    lo = sess.run(None, {sess.get_inputs()[0].name: x[None, :].astype(np.float32)})[0][0].astype(np.float32)
    lo -= lo.max(axis=1, keepdims=True)
    return lo - np.log(np.exp(lo).sum(axis=1, keepdims=True))


def _child(conn, sep_path):
    """Proceso aparte para la CPU: con prioridad IDLE (mientras alguien canta) Windows siempre atiende antes
    a la pantalla, asi que procesa solo con lo que sobra y la letra y el fondo no se traban."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40)
    sep, alg = None, {}
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        kind, arg = msg
        try:
            if kind == "prio":
                k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40 if arg else 0x4000)   # IDLE / BELOW_NORMAL
                conn.send(True)
            elif kind == "sep":
                sep = sep or _Sep(_session(sep_path, False))
                conn.send(sep.run(arg))
            elif kind == "emis":
                path, x = arg
                if path not in alg:
                    alg[path] = _session(path, False)
                conn.send(_emis(alg[path], x))
        except Exception as e:
            conn.send(RuntimeError(str(e)[:300]))


_CPU_LOCK = threading.Lock()
_CPU_IDLE = [None]


def _cpu(kind, arg, idle=True):
    global _CPU
    with _CPU_LOCK:
        if _CPU is None or not _CPU[0].is_alive():
            import multiprocessing as mp
            ctx = mp.get_context("spawn")
            a, b = ctx.Pipe()
            p = ctx.Process(target=_child, args=(b, _model_path()), daemon=True)
            p.start()
            _CPU = (p, a)
            _CPU_IDLE[0] = True
        conn = _CPU[1]
        if _CPU_IDLE[0] != idle:
            conn.send(("prio", idle))
            conn.recv()
            _CPU_IDLE[0] = idle
        conn.send((kind, arg))
        r = conn.recv()
    if isinstance(r, Exception):
        raise r
    return r


def _model_path():
    return os.path.join(_dir("models"), MODEL["file"])


def _download(spec, key):
    """Descarga verificada (sha256) a DATA/models; el progreso se ve en el PC (ENG[key])."""
    fp = os.path.join(_dir("models"), spec["file"])
    if os.path.isfile(fp) and os.path.getsize(fp) == spec["size"]:
        return fp
    part = fp + ".part"
    h = hashlib.sha256()
    req = urllib.request.Request(spec["url"], headers={"User-Agent": "Blyatt"})
    with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
        got = 0
        while True:
            b = r.read(1 << 18)
            if not b:
                break
            f.write(b)
            h.update(b)
            got += len(b)
            pr = min(99, got * 100 // spec["size"])
            if key == "align":
                ENG["align"] = "download:%d" % pr
            else:
                ENG["progress"] = pr
            _bump(soft=True)
    if h.hexdigest() != spec["sha256"]:
        os.remove(part)
        raise IOError("el modelo descargado no coincide (sha256)")
    os.replace(part, fp)
    return fp


def _engine():
    """Descarga (una vez, ~59 MB) y carga el separador; una pasada de calentamiento compila los kernels de la
    GPU para que la primera cancion no lo pague. Sin GPU usable, todo va por el proceso de CPU."""
    global _GPU
    if ENG["status"] == "ready":
        return
    import numpy as np
    try:
        ENG.update(status="download", progress=0)
        _bump()
        _download(MODEL, "progress")
        ENG.update(status="loading", progress=100)
        _bump()
        try:
            import onnxruntime as ort
            if "DmlExecutionProvider" in ort.get_available_providers():
                g = _Sep(_session(_model_path(), True))
                t = time.time()
                g.run(np.zeros((2, _CHUNK), np.float32))
                g.run(np.zeros((2, _CHUNK), np.float32))
                ENG["speed"] = round(STEP / SR / max(.05, (time.time() - t) / 2), 1)
                _GPU = g
        except Exception:
            _GPU = None   # GPU sin DirectX 12 / driver roto
        if not _GPU:
            t = time.time()
            _cpu("sep", np.zeros((2, _CHUNK), np.float32), idle=False)
            ENG["speed"] = ENG["cpu_speed"] = round(STEP / SR / max(.05, time.time() - t), 2)
        _speeds()
        ENG.update(status="ready", gpu=bool(_GPU), error="")
    except Exception as e:
        ENG.update(status="error", error=str(e)[:200])
        _bump()
        raise
    _bump()


def _speeds(save=False):
    fp = os.path.join(_dir("models"), "speeds.json")
    try:
        if save:
            with open(fp, "w") as f:
                json.dump({"speed": ENG["speed"], "cpu_speed": ENG["cpu_speed"]}, f)
        else:
            with open(fp) as f:
                d = json.load(f)
            ENG["cpu_speed"] = float(d.get("cpu_speed") or ENG["cpu_speed"])
    except (OSError, ValueError, TypeError):
        pass


def _quiet():
    """Nadie cantando: sala, presentacion antes de la cuenta atras o resultados ya asentados. Solo entonces
    se usa la GPU (cada pasada la ocupa ~0.9 s sin que el sistema pueda intercalar la pantalla)."""
    n = S["now"]
    if not n:
        return True
    ph = n.get("phase")
    return ph == "intro" or (ph == "results" and time.time() - n.get("pt", 0) > 2.5)


def _separate(seg):
    if _quiet() and _GPU:
        t = time.time()
        r = _GPU.run(seg)
        ENG["speed"] = round(.7 * ENG["speed"] + .3 * STEP / SR / max(.05, time.time() - t), 2)
        return r
    idle = not _quiet()
    t = time.time()
    r = _cpu("sep", seg, idle=idle)
    if idle:   # lo que importa para decidir cuando empezar: la velocidad con alguien cantando
        ENG["cpu_speed"] = round(.6 * ENG["cpu_speed"] + .4 * STEP / SR / max(.05, time.time() - t), 2)
        _speeds(save=True)
    return r


_W2V_DL = {}


def _w2v(kind):
    """Ruta del alineador (`gpu`: fp16, `cpu`: int8) o None mientras se descarga (en otro hilo, una sola vez)."""
    spec = W2V[kind]
    fp = os.path.join(_dir("models"), spec["file"])
    if os.path.isfile(fp) and os.path.getsize(fp) == spec["size"]:
        return fp
    th = _W2V_DL.get(kind)
    if not th or not th.is_alive():
        def run():
            try:
                _download(spec, "align")
                ENG["align"] = "ready"
            except Exception as e:
                ENG["align"] = "error:" + str(e)[:80]
            _WAKE.set()
            _bump()
        th = _W2V_DL[kind] = threading.Thread(target=run, daemon=True)
        th.start()
    return None


def _emissions(x16):
    """Log-probs del alineador: GPU si nadie canta (y hay), si no el proceso de CPU. None = modelo aun bajando."""
    global _ALG
    if _quiet() and _GPU and _ALG is not False:
        if _ALG is None:
            fp = _w2v("gpu")
            if not fp:
                return None
            try:
                _ALG = _session(fp, True)
            except Exception:
                _ALG = False   # sin memoria de video para los dos modelos: el alineado va por la CPU
        if _ALG:
            try:
                return _emis(_ALG, x16)
            except Exception:
                _ALG = False
    fp = _w2v("cpu")
    return _cpu("emis", (fp, x16), idle=not _quiet()) if fp else None


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


# ---------------------------------------------------------------- alineado de la letra (CTC forzado)
def _toks(word):
    """Palabra -> indices del vocabulario (sin tildes: canciones en espanol tambien alinean bien)."""
    w = "".join(c for c in unicodedata.normalize("NFKD", word.upper()) if not unicodedata.combining(c))
    return [4 + W2V_CHARS.index(c) for c in w.replace("’", "'") if c in W2V_CHARS[1:]]


def _viterbi(np, E, toks):
    """Camino CTC mas probable de `toks` por las emisiones E [T, C] -> estado por frame (impar = token)."""
    T, S = len(E), len(toks)
    L = 2 * S + 1
    lab = np.zeros(L, np.int64)
    lab[1::2] = toks
    tk = np.array(toks)
    skip = np.zeros(L, bool)
    skip[3::2] = tk[1:] != tk[:-1]
    NEG = -1e9
    dp = np.full(L, NEG, np.float32)
    dp[0], dp[1] = E[0, 0], E[0, lab[1]]
    bp = np.zeros((T, L), np.int8)
    st = np.empty((3, L), np.float32)
    cols = np.arange(L)
    for t in range(1, T):
        st[0] = dp
        st[1, 0] = NEG
        st[1, 1:] = dp[:-1]
        st[2, :2] = NEG
        st[2, 2:] = dp[:-2]
        st[2, ~skip] = NEG
        k = st.argmax(0)
        bp[t] = k
        dp = st[k, cols] + E[t, lab]
    s = L - 1 if dp[L - 1] >= dp[L - 2] else L - 2
    path = np.zeros(T, np.int64)
    for t in range(T - 1, -1, -1):
        path[t] = s
        s -= bp[t, s]
    return path


def _words_of(text):
    return re.findall(r"\S+", text or "")


def _place(np, E, t0, words, pitch):
    """Alinea las palabras de un tramo cuyas emisiones (desde t0 s) son E -> [(inicio, fin)] en segundos.
    Las palabras sin letras alineables (numeros, otros alfabetos) se reparten entre sus vecinas."""
    toks, owner = [], []
    for i, w in enumerate(words):
        tk = _toks(w)
        toks += tk
        owner += [i] * len(tk)
    if not toks or len(E) < 2 * len(toks) + 2:
        return None
    path = _viterbi(np, E, toks)
    first, last = {}, {}
    for f, st in enumerate(path.tolist()):
        if st % 2:
            w = owner[st // 2]
            first.setdefault(w, f)
            last[w] = f
    n = len(words)
    st = [t0 + first[i] * .02 - ALIGN_BIAS if i in first else None for i in range(n)]
    en = [t0 + (last[i] + 1) * .02 - ALIGN_BIAS if i in last else None for i in range(n)]
    known = [i for i in range(n) if st[i] is not None]
    if not known:
        return None
    for i in range(n):   # sin tokens: interpolar entre las conocidas
        if st[i] is None:
            a = max([k for k in known if k < i], default=None)
            b = min([k for k in known if k > i], default=None)
            lo = en[a] if a is not None else st[b] - .3
            hi = st[b] if b is not None else en[a] + .3
            st[i], en[i] = lo, max(lo + .1, hi)
    out = []
    for i in range(n):
        s0 = max(st[i], out[-1][1] if out else st[i])
        nxt = st[i + 1] if i + 1 < n else s0 + 6
        e0 = max(en[i], s0 + .12)
        # notas sostenidas: el CTC marca solo el principio de la vocal; la voz (melodia) dice hasta donde dura
        f = int(e0 / .02)
        while f < len(pitch) and pitch[f] and (f + 1) * .02 < nxt - .05 and (f + 1) * .02 - s0 < 6:
            f += 1
        e0 = max(e0, min(f * .02, nxt - .03))
        out.append((s0, max(s0 + .1, e0)))
    return out


def _syllabus(words, times):
    return [{"time": int(a * 1000), "duration": int(max(80, (b - a) * 1000)),
             "text": w + (" " if i < len(words) - 1 else "")} for i, (w, (a, b)) in enumerate(zip(words, times))]


def _need_align(lyr):
    if not lyr or lyr.get("type") not in ("Line", "Static") or lyr.get("aligned_all"):
        return False
    lines = [l for l in lyr.get("lines") or [] if _words_of(l.get("text"))]
    ok = sum(1 for l in lines if _toks(l["text"]))
    return bool(lines) and ok >= .6 * len(lines)


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
             "pitch": [], "lyrics": None, "lrev": 0, "align": "", "error": "", "title": "", "artist": "", "catdur": 0}
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
            d = APP.lyrics(s["title"], s["artist"], s["catdur"] or s["dur"], s["vid"])
        except Exception:
            d = {"lines": []}
        s["lyrics"] = json.loads(json.dumps(d))   # copia propia: el alineado la modifica
        s["lrev"] += 1
        if _need_align(s["lyrics"]):
            s["align"] = "pending"
            _WAKE.set()
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
                       capture_output=True, timeout=120, creationflags=0x4000 | 0x08000000)   # BELOW_NORMAL, sin ventana
    if r.returncode != 0 or not r.stdout:
        raise IOError("no se pudo decodificar el audio")
    return np.frombuffer(r.stdout, np.float32).reshape(-1, 2).T.copy()


def _to16(np, x, a):
    """Trozo mono a 44.1 kHz que empieza en la muestra `a` -> 16 kHz en la rejilla global (sin costuras)."""
    i0 = int(np.ceil(a * 16000 / SR))
    i1 = int(np.ceil((a + len(x)) * 16000 / SR))
    if i1 <= i0:
        return i0, np.zeros(0, np.float32)
    t = np.arange(i0, i1) * SR / 16000 - a
    return i0, np.interp(t, np.arange(len(x)), x).astype(np.float32)


def _save_meta(s):
    meta = {k2: s[k2] for k2 in ("n", "dur", "nch", "pitch", "title", "artist", "catdur", "lyrics", "lrev", "align")}
    meta.update(step=STEP, sr=SR, model=MODEL["file"])
    with open(os.path.join(_song_dir(s["vid"]), "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _align_steps(np, s, v16, upto):
    """Pasos de alineado de lo que ya tiene voz separada hasta `upto` segundos (uno por `yield`)."""
    lyr = s["lyrics"]
    if s["align"] != "pending" or not lyr:
        return
    if str(ENG["align"]).startswith("error") and not any(t.is_alive() for t in _W2V_DL.values()):
        s["align"] = "error"
        return
    lines = [l for l in lyr["lines"] if _words_of(l.get("text"))]
    done = s.setdefault("_aligned", set())
    if lyr.get("type") == "Line":
        for i, l in enumerate(lines):
            if i in done:
                continue
            a = max(0, l["time"] / 1000 - .35)
            nt = lines[i + 1]["time"] / 1000 if i + 1 < len(lines) else min(s["dur"], a + 12)
            b = min(nt + .25, a + 15, s["dur"])
            if b > upto:
                return
            words = _words_of(l["text"])
            if b - a > .5 and _toks(l["text"]):
                E = _emissions(v16[int(a * 16000): int(b * 16000)])
                if E is None:
                    yield "wait"
                    return
                times = _place(np, E, a, words, s["pitch"])
                if times:
                    l["syllabus"] = _syllabus(words, times)
                    l["time"] = l["syllabus"][0]["time"]
                    s["lrev"] += 1
                    _bump(soft=True)
            done.add(i)
            yield
    elif lyr.get("type") == "Static" and upto >= s["dur"] - .1:
        # solo texto: la cancion entera de una vez (ventanas de 30 s para las emisiones)
        Es = s.setdefault("_E", [])
        for a in range(len(Es) * 30, int(s["dur"]) + 1, 30):
            e = _emissions(v16[a * 16000: min(len(v16), (a + 30) * 16000)])
            if e is None:
                yield "wait"
                return
            Es.append(e)
            yield
        E = np.concatenate([e for e in Es if len(e)])
        s.pop("_E", None)
        words = [w for l in lines for w in _words_of(l["text"])]
        times = _place(np, E, 0, words, s["pitch"])
        if times:
            k = 0
            for l in lines:
                ws = _words_of(l["text"])
                l["syllabus"] = _syllabus(ws, times[k: k + len(ws)])
                l["time"] = l["syllabus"][0]["time"]
                k += len(ws)
            lyr["type"] = "Line"
    else:
        return
    lyr["aligned_all"] = True
    s["align"] = "done"
    s["lrev"] += 1
    _bump()


def _job(vid):
    """Procesa una cancion por pasos; cada `yield` es un punto donde el hilo puede cambiar de trabajo."""
    import numpy as np
    s = SONGS[vid]
    d = _song_dir(vid)
    os.makedirs(d, exist_ok=True)
    if s["status"] == "ready":   # ya separada (cache): solo falta alinear la letra con la voz guardada
        _get_lyrics(s)
        v16 = []
        for k in range(s["nch"]):
            with open(os.path.join(d, "%03d.pcm" % k), "rb") as f:
                b = np.frombuffer(f.read(), "<i2")
            n = len(b) // 3
            v16.append(_to16(np, b[2 * n:].astype(np.float32) / 32767, k * STEP)[1])
            if k % 8 == 7:
                yield
        v16 = np.concatenate(v16)
        while s["align"] == "pending":
            yield from _align_steps(np, s, v16, s["dur"])
            if s["align"] == "pending":
                yield "wait"
        _save_meta(s)
        return
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
    pad = np.zeros((2, _TRIM + N + _CHUNK), np.float32)
    pad[:, _TRIM:_TRIM + N] = mix
    vd = np.zeros(nch * STEP // _PD + 1, np.float32)   # voz y mezcla mono a 14.7 kHz para la melodia
    md = np.zeros_like(vd)
    v16 = np.zeros(int(N * 16000 / SR) + 2, np.float32)   # voz a 16 kHz para el alineado de la letra
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
            inst = _separate(seg)[:, _TRIM: _CHUNK - _TRIM]
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

        def dec(x):
            x = x[: len(x) // _PD * _PD]
            return x.reshape(-1, _PD).mean(axis=1)
        vd[a // _PD: a // _PD + n // _PD] = dec(voc)
        md[a // _PD: a // _PD + n // _PD] = dec(m.mean(axis=0))
        i0, y = _to16(np, voc, a)
        v16[i0: i0 + len(y)] = y[: max(0, len(v16) - i0)]
        upto = nfr if k == nch - 1 else max(pf, ((a + n) // _PD - _YW - _TMAX) // _PH)
        s["pitch"].extend(_yin(np, vd, md, pf, min(upto, nfr)))
        pf = min(upto, nfr)
        s.update(chunks=k + 1, progress=int((k + 1) * 100 / nch))
        _bump(soft=True)
        yield
        if s["lyrics"] is not None:
            yield from _align_steps(np, s, v16, (a + n) / SR - .2)
    s.update(status="ready", progress=100)
    _bump()
    _get_lyrics(s)
    _save_meta(s)
    while s["align"] == "pending":
        yield from _align_steps(np, s, v16, s["dur"])
        if s["align"] == "pending":
            yield "wait"
    _save_meta(s)
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


def _pending(v):
    s = SONGS.get(v)
    return s and s["status"] != "error" and (s["status"] != "ready" or s["align"] == "pending")


def _worker():
    # prioridad normal a proposito: con el GIL en la mano, un hilo de baja prioridad sin CPU dejaria esperando a
    # los hilos que atienden a la pantalla y a los moviles (lo pesado ya va en la GPU o en el proceso IDLE)
    while True:
        if not S["on"]:
            _WAKE.wait(5)
            _WAKE.clear()
            continue
        if ENG["status"] != "ready":
            time.sleep(1.5)   # la carga del modelo retiene el GIL unos segundos: primero que la sala se pinte
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
        todo = [v for v in _order() if _pending(v)]
        if not todo:
            _WAKE.wait(5)
            _WAKE.clear()
            continue
        stepped = False
        for vid in todo:
            g = _GENS.get(vid)
            if g is None:
                g = _GENS[vid] = _job(vid)
            try:
                r = next(g)
            except StopIteration:
                _GENS.pop(vid, None)
                if SONGS[vid]["align"] == "pending":   # la letra no se pudo alinear entera: se deja como esta
                    SONGS[vid]["align"] = "done"
                r = None
            except Exception as e:
                _GENS.pop(vid, None)
                s = SONGS[vid]
                if s["status"] == "ready":   # fallo del alineado: la cancion se puede cantar igual
                    s["align"] = "error"
                else:
                    s.update(status="error", error=str(e)[:160])
                _bump()
                r = None
            if r != "wait":
                stepped = True
                break
        if not stepped:   # todo espera a una descarga
            time.sleep(.5)


# ---------------------------------------------------------------- sesion (estado compartido PC <-> moviles)
S = {"on": False, "code": "", "ip": "", "port": 0, "tport": 0, "v": 0, "players": {}, "queue": [], "now": None,
     "history": [], "live": None, "reacts": [], "qid": 0, "rid": 0, "diff": "normal", "guide": 0, "cmds": [], "cid": 0}
_COND = threading.Condition()
_LAST_SOFT = [0.0]
_SRV = []
_MIC = {"seq": 0, "buf": deque(maxlen=600)}   # (seq, jugador, ts del servidor, midi*10) del micro de los moviles
_MICC = threading.Condition()


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


def mic_url():
    return "https://%s:%d" % (S["ip"], S["tport"]) if S["tport"] else ""


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
    now_t = time.time()
    pl = []
    for p in S["players"].values():
        x = {k: p[k] for k in ("id", "name", "color", "total", "best", "songs")}
        x["avg"] = round(p["total"] / p["songs"]) if p["songs"] else 0
        x["mic"] = now_t - p.get("mic_at", 0) < 4
        pl.append(x)
    # ranking justo: el promedio por cancion (no la suma), desempate por la mejor
    pl.sort(key=lambda p: (-(p["songs"] > 0), -p["avg"], -p["best"], p["name"].lower()))
    now = None
    if S["now"]:
        now = song(S["now"])
        s = SONGS.get(now["vid"]) or {}
        now["dur"] = s.get("dur") or now.get("dur")
    return {"v": S["v"], "on": S["on"], "code": S["code"], "url": url() if S["on"] else "", "mic_url": mic_url(),
            "engine": dict(ENG), "players": pl, "queue": [song(q) for q in S["queue"]], "now": now,
            "live": S["live"], "history": S["history"][-30:], "reacts": S["reacts"][-12:], "diff": S["diff"],
            "guide": S["guide"], "cmds": S["cmds"][-10:], "t": now_t}


def wait_state(v, timeout=25.0):
    with _COND:
        _COND.wait_for(lambda: S["v"] != v, timeout)
        return _pub()


# ---------------------------------------------------------------- servidores de la red local (moviles)
class _LanServer(ThreadingHTTPServer):
    allow_reuse_address = False   # en Windows permitiria a dos procesos escuchar en el mismo puerto
    daemon_threads = True

    def server_bind(self):   # sin socket.getfqdn("0.0.0.0"): una busqueda DNS inversa que tarda segundos
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "blyatt-karaoke", self.server_address[1]

    def handle_error(self, request, client_address):   # certificado rechazado, pestana cerrada...: sin ruido
        pass


class _TlsServer(_LanServer):
    ctx = None

    def get_request(self):   # el saludo TLS se hace en el hilo de cada conexion, no aqui
        sock, addr = self.socket.accept()
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr


def _cert():
    """Certificado propio (EC P-256, 2 anos) para la IP local: el movil avisa una vez y luego deja usar el micro."""
    d = _dir("karaoke_tls")
    cp, kp, ip_f = (os.path.join(d, x) for x in ("cert.pem", "key.pem", "ip.txt"))
    try:
        with open(ip_f) as f:
            if f.read() == S["ip"] and os.path.isfile(cp) and os.path.isfile(kp):
                return cp, kp
    except OSError:
        pass
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Blyatt Karaoke")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=730))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(S["ip"])),
                                                        x509.DNSName("blyatt.local")]), critical=False)
            .sign(key, hashes.SHA256()))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(ip_f, "w") as f:
        f.write(S["ip"])
    return cp, kp


def _listen(cls, ports):
    for port in list(ports) + [0]:
        try:
            return cls(("0.0.0.0", port), _Guest)
        except OSError:
            continue


def open_session(diff=None, guide=None):
    global _WORKER
    if not S["on"]:
        S.update(on=True, code="".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4)),
                 players={}, queue=[], now=None, history=[], live=None, reacts=[], cmds=[], tport=0)
        S["ip"] = _lan_ip()
        srv = _listen(_LanServer, range(8765, 8776))
        S["port"] = srv.server_address[1]
        _SRV.append(srv)
        try:   # puerta https (micro del movil); sin cryptography el karaoke funciona igual sin ella
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*_cert())
            _TlsServer.ctx = ctx
            tsrv = _listen(_TlsServer, range(8865, 8876))
            S["tport"] = tsrv.server_address[1]
            _SRV.append(tsrv)
        except Exception:
            S["tport"] = 0
        for x in _SRV:
            threading.Thread(target=x.serve_forever, daemon=True).start()
    if diff in DIFFS:
        S["diff"] = diff
    if guide is not None:
        settings({"guide": guide})
    if not _WORKER:
        _WORKER = threading.Thread(target=_worker, daemon=True)
        _WORKER.start()
    _WAKE.set()
    _bump()
    return dict(_pub(), qr=qr_svg())


def close_session():
    S.update(on=False, now=None, queue=[], live=None)
    _GENS.clear()   # lo a medio procesar se suelta (memoria); al volver se rehace desde el principio
    for s in SONGS.values():
        if s["status"] not in ("ready", "error"):
            s["status"] = "wait"
        if s["align"] == "pending":
            s.pop("_aligned", None)
    while _SRV:
        srv = _SRV.pop()
        threading.Thread(target=lambda x=srv: (x.shutdown(), x.server_close()), daemon=True).start()
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


def command(p, c, v=None):
    """Mando a distancia: solo quien canta controla su cancion (pausa, terminar, voz guia)."""
    now = S["now"]
    if not now or now["by"] != p["id"] or c not in ("pause", "resume", "end", "guide"):
        return {"error": "Solo quien canta puede controlar la canción"}
    if c == "guide":
        try:
            S["guide"] = max(0, min(100, int(v)))
        except (TypeError, ValueError):
            return {"error": "?"}
    S["cid"] += 1
    S["cmds"] = (S["cmds"] + [{"id": S["cid"], "c": c, "v": S["guide"] if c == "guide" else None}])[-20:]
    _bump()
    return {"ok": True}


def mic_in(p, samples):
    """Altura de la voz que manda el movil de quien canta: [[ts del servidor, midi*10], ...]."""
    now = S["now"]
    if not now or now["by"] != p["id"] or not isinstance(samples, list):
        return {"ok": False}
    p["mic_at"] = time.time()
    with _MICC:
        for x in samples[:60]:
            try:
                ts, m = float(x[0]), int(x[1])
            except (TypeError, ValueError, IndexError):
                continue
            _MIC["seq"] += 1
            _MIC["buf"].append((_MIC["seq"], p["id"], ts, max(0, min(1500, m))))
        _MICC.notify_all()
    return {"ok": True}


def mic_out(pid, since, timeout=1.0):
    with _MICC:
        _MICC.wait_for(lambda: _MIC["seq"] > since, timeout)
        return {"seq": _MIC["seq"], "s": [[q, ts, m] for q, who, ts, m in _MIC["buf"] if q > since and who == pid]}


def next_song():
    S["now"] = None
    S["live"] = None
    if S["queue"]:
        q = S["queue"].pop(0)
        S["now"] = dict(q, phase="intro", score=None, pt=time.time())
    _WAKE.set()
    _bump()
    return _pub()


def set_phase(phase):
    if S["now"] and phase in ("intro", "count", "sing", "paused", "results"):
        S["now"].update(phase=phase, pt=time.time())
        _WAKE.set()
        _bump()
    return {"ok": True}


def settings(d):
    if d.get("diff") in DIFFS:
        S["diff"] = d["diff"]
    if "guide" in d:
        try:
            S["guide"] = max(0, min(100, int(d["guide"])))
        except (TypeError, ValueError):
            pass
    _bump()
    return {"ok": True, "diff": S["diff"], "guide": S["guide"]}


def live(score, rating):
    if S["now"]:
        S["live"] = {"qid": S["now"]["qid"], "score": int(score or 0), "rating": str(rating or "")[:24]}
        _bump()
    return {"ok": True}


def result(score):
    now = S["now"]
    if not now:
        return {"error": "Nada sonando"}
    now.update(phase="results", pt=time.time())
    sc = None if score is None else max(0, min(10000, int(score)))
    now["score"] = sc
    p = S["players"].get(now["by"])
    if p and sc is not None:
        p["total"] += sc
        p["best"] = max(p["best"], sc)
        p["songs"] += 1
    S["history"].append({"qid": now["qid"], "title": now["title"], "artist": now["artist"], "cover": now["cover"],
                         "by": now["by"], "score": sc, "diff": S["diff"], "t": int(time.time())})
    S["live"] = None
    _WAKE.set()
    _bump()
    return _pub()


def song_info(vid, frm=0, lrev=-1):
    s = SONGS.get(vid)
    if not s:
        return {"error": "desconocida"}
    frm = max(0, int(frm or 0))
    if s["lyrics"] is None and s["title"]:
        _prefetch(vid)
    out = {k: s[k] for k in ("status", "progress", "chunks", "nch", "n", "dur", "error", "lrev", "align")}
    out.update(sr=SR, step=STEP, hop=PITCH_HOP, pitch=s["pitch"][frm:], gpu=ENG["gpu"],
               speed=ENG["speed"], cpu_speed=ENG["cpu_speed"], **{"from": frm})
    if int(lrev) != s["lrev"]:   # la letra solo viaja cuando cambia (el alineado la va completando)
        out["lyrics"] = s["lyrics"]
    return out


def pcm(vid, n):
    s = SONGS.get(vid)
    if not s or n >= s["chunks"]:
        return None
    with open(os.path.join(_song_dir(vid), "%03d.pcm" % n), "rb") as f:
        return f.read()


class _Guest(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # conexiones que se reutilizan (el micro manda 10 veces por segundo)
    timeout = 75

    def setup(self):
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(10)
            self.request.do_handshake()
        super().setup()

    def handle(self):
        try:
            super().handle()
        except (ssl.SSLError, ConnectionError, socket.timeout, OSError):
            pass   # el movil rechazo el certificado la primera vez, cerro la pestana, etc.

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
        if u.path == "/k/time":   # sincronizar el reloj del movil con el del PC (para el micro)
            return self._out({"t": time.time()})
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
        n = int(self.headers.get("Content-Length") or 0)
        if n > 16384:
            self.close_connection = True
            return self._out({"error": "demasiado grande"}, status=413)
        raw = self.rfile.read(n)
        if not self._ok_room(qs):
            return self._out({"error": "room"}, status=403)
        try:
            d = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        if u.path == "/k/join":
            return self._out(join(d.get("name"), d.get("key")))
        p = _player(d.get("key"))
        if not p:
            return self._out({"error": "join"}, status=401)
        if u.path == "/k/mic":
            return self._out(mic_in(p, d.get("s")))
        if u.path == "/k/add":
            return self._out(add(p["id"], d.get("song") or {}))
        if u.path == "/k/remove":
            return self._out(remove(int(d.get("qid") or 0), p["id"]))
        if u.path == "/k/react":
            return self._out(react(p["id"], d.get("e")))
        if u.path == "/k/cmd":
            return self._out(command(p, d.get("c"), d.get("v")))
        return self._out({"error": "?"}, status=404)

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------- rutas del PC (servidor local de la app)
def host_api(method, path, qs, d):
    g = lambda k, dv="": (qs.get(k) or [dv])[0]
    if path == "/kara/open":
        return open_session(d.get("diff"), d.get("guide"))
    if path == "/kara/close":
        return close_session()
    if path == "/kara/state":
        try:
            v = int(g("v", "-1"))
        except ValueError:
            v = -1
        return wait_state(v, 20) if v >= 0 else _pub()
    if path == "/kara/song":
        try:
            return song_info(g("id"), g("from", "0"), g("lrev", "-1"))
        except ValueError:
            return {"error": "?"}
    if path == "/kara/mic":
        try:
            return mic_out(g("pid"), int(g("since", "0")))
        except ValueError:
            return {"error": "?"}
    if path == "/kara/next":
        return next_song()
    if path == "/kara/phase":
        return set_phase(d.get("phase"))
    if path == "/kara/set":
        return settings(d)
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
