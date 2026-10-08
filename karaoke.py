"""Modo karaoke (app de escritorio).

El PC es el escenario: procesa cada cancion (voz fuera con IA) y la reproduce con la letra palabra a palabra;
los moviles de la misma red se unen escaneando un QR, eligen canciones, ven la cola y el ranking, mandan
reacciones y (si quieren) hacen de micro inalambrico y mando a distancia mientras cantan.

Todo se calcula en la CPU, en un proceso aparte de prioridad baja: la GPU es de la pantalla. Medido en una
RX 550: cualquier trabajo de IA en la GPU (DirectML) congela la pantalla mientras dura (fondo a 1-10 fps), sin
importar la prioridad; en la CPU con prioridad IDLE la pantalla sigue a 60 fps porque Windows siempre atiende
antes a lo que dibuja.

Flujo de cada cancion (para que empiece en segundos):
  1. al anadirla a la cola se baja el audio y la letra en segundo plano (cache de Blyatt)
  2. separacion voz/instrumental rapida con Spleeter (ONNX, ~15x tiempo real en CPU): bloques de ~12 s que se
     pueden reproducir en cuanto salen
  3. de la voz separada sale la melodia de referencia (YIN, 50 fps) con la que se puntua al cantante
  4. si la letra no viene palabra a palabra, se alinea con la voz separada (wav2vec2 CTC forzado, q4)
  5. con la CPU libre (nadie cantando), las canciones se mejoran a MDX-Net HQ5 (mas limpia) para la proxima vez
Mientras alguien canta, se comprueba cada linea que canta (su micro) contra la letra con el mismo wav2vec2:
quien canta la letra puntua entero; quien tararea o calla, menos.
Un solo hilo reparte el trabajo por prioridad: cada cancion es un generador que avanza paso a paso.

Los moviles usan un servidor aparte en la red local que SOLO tiene las rutas del karaoke (nada de la cuenta,
la biblioteca ni el resto de la app). Hay dos puertas: http (entrar sin avisos) y https con un certificado
propio (el navegador del movil solo deja usar el micro en https: avisa una vez por ser un certificado local).
"""
import bisect
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
import tarfile
import threading
import time
import unicodedata
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP = None   # app.py se inyecta aqui (busqueda, letra, descarga de audio, rutas de datos)

SR = 44100
MODEL = {   # mejora en segundo plano: UVR-MDX-NET-Inst_HQ_5 (parametros de model_data.json de UVR)
    "file": "UVR-MDX-NET-Inst_HQ_5.onnx",
    "url": "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR-MDX-NET-Inst_HQ_5.onnx",
    "size": 59074342,
    "sha256": "811cb24095d865763752310848b7ec86aeede0626cb05749ab35350e46897000",
    "n_fft": 5120, "dim_f": 2560, "dim_t": 256, "hop": 1024, "comp": 1.01,
}
SPLEETER = {   # deezer/spleeter 2stems (MIT) convertido a ONNX por k2-fsa/sherpa-onnx (Apache-2.0)
    "file": "spleeter-2stems.tar.bz2", "dir": "spleeter-2stems",
    "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/source-separation-models/sherpa-onnx-spleeter-2stems.tar.bz2",
    "size": 74682545, "sha256": "e26401d9c1801f43c0229731d78d32c2e80085e3aceedeb25f29d2de5fa68ca2",
}
W2V = {   # facebook/wav2vec2-base-960h (Apache-2.0) en ONNX con pesos de 4 bits: el mas rapido en CPU (0.15 s/s)
    "file": "wav2vec2-base-960h_q4.onnx",
    "url": "https://huggingface.co/Xenova/wav2vec2-base-960h/resolve/a19f851b3d42865797e410752b4c570c871e4825/onnx/model_q4.onnx",
    "size": 89834049, "sha256": "337db946188e4b3d0b4a2641dc30727a4792f70aeb1750f65a20fae471f9f217",
}
W2V_CHARS = "|ETAONIHSRDLUMWCFGYPBVK'XJQZ"   # indices 4.. del vocabulario (0 = blanco CTC, 4 = espacio)
ALIGN_BIAS = .08   # medido contra letras palabra a palabra reales: el CTC marca el inicio ~80 ms tarde...
ALIGN_BIAS_SPL = .13   # ...y con la voz de Spleeter (ventana de 93 ms, ataques mas suaves) algo mas
# verificacion de la letra: confianza media de cada letra esperada en su tramo (medido con voces reales: letra
# correcta ~0.36-0.40, tarareo ~0.06-0.08, solo instrumental ~0.04)
VERIFY_LO, VERIFY_HI = .08, .22
_CHUNK = MODEL["hop"] * (MODEL["dim_t"] - 1)   # 261120 muestras por pasada del modelo HQ5
_TRIM = MODEL["n_fft"] // 2                    # bordes de cada pasada que se descartan
_XF = 4096                                     # fundido entre pasadas (sin costuras audibles)
STEP = _CHUNK - 2 * _TRIM - _XF                # 251904 muestras (~5.7 s) = un trozo reproducible
SP_N, SP_H, SP_F, SP_T = 4096, 1024, 1024, 512  # STFT y bloque de Spleeter (512 frames = ~11.9 s)
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


# ---------------------------------------------------------------- motores ONNX (todos en CPU)
ENG = {"status": "idle", "progress": 0, "error": "", "speed": 8, "align": ""}
_CPU = None   # (proceso, conexion)


def _session(path):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.intra_op_num_threads = os.cpu_count() or 4
    so.add_session_config_entry("session.intra_op.allow_spinning", "0")   # sin espera activa: no roba CPU
    return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])


class _Sep:
    """MDX-Net HQ5: STFT/ISTFT identicas a torch alrededor del modelo."""
    def __init__(self, sess):
        import numpy as np
        self.np, self.s = np, sess
        self.inp = sess.get_inputs()[0].name
        n = MODEL["n_fft"]
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)   # hann periodica (torch)
        T, hop = MODEL["dim_t"], MODEL["hop"]
        k = n // hop
        w2 = (self.win ** 2).reshape(k, hop)
        env = np.zeros(n + hop * (T - 1), np.float32)
        for j in range(k):
            env[j * hop: j * hop + T * hop] += np.tile(w2[j], T)
        self.env = env[n // 2: n // 2 + _CHUNK]
        self.idx = np.arange(n)[None, :] + hop * np.arange(T)[:, None]

    def run(self, seg):
        """seg: [2, 261120] float32 (mezcla) -> instrumental [2, 261120]."""
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


class _Spl:
    """Spleeter 2stems: mascara de acompanamiento (Wiener, exponente 2) sobre la STFT; por encima de 11 kHz
    (donde el modelo no llega) se usa la media de las bandas mas altas. Devuelve el solapamiento-suma del
    bloque sin normalizar (la normalizacion global la hace quien junta los bloques: sin costuras)."""
    def __init__(self, d):
        import numpy as np
        self.np = np
        self.v = _session(os.path.join(d, "vocals.onnx"))
        self.a = _session(os.path.join(d, "accompaniment.onnx"))
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(SP_N) / SP_N)).astype(np.float32)

    def run(self, seg, nfr):
        np = self.np
        N, H, F, T = SP_N, SP_H, SP_F, SP_T
        idx = np.arange(N)[None, :] + H * np.arange(nfr)[:, None]
        S = np.fft.rfft(seg[:, idx] * self.win, axis=-1).astype(np.complex64)   # [2, nfr, 2049]
        x = np.zeros((2, 1, T, F), np.float32)
        x[:, 0, :nfr] = np.abs(S[:, :, :F])
        v = self.v.run(None, {"x": x})[0][:, 0, :nfr]
        a = self.a.run(None, {"x": x})[0][:, 0, :nfr]
        m = (a * a + 5e-11) / (v * v + a * a + 1e-10)
        M = np.empty(S.shape, np.float32)
        M[:, :, :F] = m
        M[:, :, F:] = m[:, :, -64:].mean(axis=2, keepdims=True)
        fr = np.fft.irfft(S * M, n=N, axis=-1).astype(np.float32) * self.win
        out = np.zeros((2, (nfr - 1) * H + N), np.float32)
        for q in range(N // H):
            out[:, q * H: q * H + nfr * H] += fr[:, :, q * H:(q + 1) * H].reshape(2, nfr * H)
        return out


def _emis(sess, x):
    """Voz a 16 kHz -> log-probabilidades CTC [frames de 20 ms, 32]."""
    import numpy as np
    x = (x - x.mean()) / (x.std() + 1e-7)
    lo = sess.run(None, {sess.get_inputs()[0].name: x[None, :].astype(np.float32)})[0][0].astype(np.float32)
    lo -= lo.max(axis=1, keepdims=True)
    return lo - np.log(np.exp(lo).sum(axis=1, keepdims=True))


def _child(conn, paths):
    """Proceso aparte para la IA: con prioridad IDLE (mientras alguien canta) Windows siempre atiende antes a la
    pantalla, asi que procesa solo con lo que sobra y la letra y el fondo no se traban."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40)
    m = {}
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
            elif kind == "spl":
                if "spl" not in m:
                    m["spl"] = _Spl(paths["spl"])
                conn.send(m["spl"].run(*arg))
            elif kind == "sep":
                if "sep" not in m:
                    m["sep"] = _Sep(_session(paths["sep"]))
                conn.send(m["sep"].run(arg))
            elif kind == "emis":
                if "w2v" not in m:
                    m["w2v"] = _session(paths["w2v"])
                conn.send(_emis(m["w2v"], arg))
        except Exception as e:
            conn.send(RuntimeError(str(e)[:300]))


_CPU_LOCK = threading.Lock()
_CPU_IDLE = [None]


def _cpu(kind, arg):
    """Tarea en el proceso de IA. Prioridad IDLE si alguien canta; BELOW_NORMAL si no (sigue cediendo a la pantalla)."""
    global _CPU
    idle = not _quiet()
    with _CPU_LOCK:
        if _CPU is None or not _CPU[0].is_alive():
            import multiprocessing as mp
            ctx = mp.get_context("spawn")
            a, b = ctx.Pipe()
            paths = {"spl": os.path.join(_dir("models"), SPLEETER["dir"]), "sep": _model_path(MODEL),
                     "w2v": _model_path(W2V)}
            p = ctx.Process(target=_child, args=(b, paths), daemon=True)
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


def _model_path(spec):
    return os.path.join(_dir("models"), spec["file"])


def _download(spec, key="progress"):
    """Descarga verificada (sha256) a DATA/models; el progreso se ve en el PC."""
    fp = _model_path(spec)
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
            if key:
                ENG[key] = min(99, got * 100 // spec["size"])
                _bump(soft=True)
    if h.hexdigest() != spec["sha256"]:
        os.remove(part)
        raise IOError("el modelo descargado no coincide (sha256)")
    os.replace(part, fp)
    return fp


def _engine():
    """Primera vez: Spleeter (~71 MB) y el alineador (~86 MB). Una pasada de calentamiento carga los modelos en
    el proceso de IA para que la primera cancion no lo pague."""
    if ENG["status"] == "ready":
        return
    import numpy as np
    try:
        d = os.path.join(_dir("models"), SPLEETER["dir"])
        if not os.path.isfile(os.path.join(d, "accompaniment.onnx")):
            ENG.update(status="download", progress=0)
            _bump()
            tb = _download(SPLEETER)
            with tarfile.open(tb, "r:bz2") as t:
                for mem in t.getmembers():
                    name = os.path.basename(mem.name)
                    if mem.isfile() and name in ("vocals.onnx", "accompaniment.onnx"):
                        os.makedirs(d, exist_ok=True)
                        with t.extractfile(mem) as src, open(os.path.join(d, name), "wb") as dst:
                            shutil.copyfileobj(src, dst)
            os.remove(tb)
        ENG.update(status="loading", progress=100)
        _bump()
        threading.Thread(target=_w2v_fetch, daemon=True).start()
        t = time.time()
        _cpu("spl", (np.zeros((2, (SP_T - 1) * SP_H + SP_N), np.float32), SP_T))
        ENG.update(status="ready", error="", speed=round(SP_T * SP_H / SR / max(.05, time.time() - t), 1))
    except Exception as e:
        ENG.update(status="error", error=str(e)[:200])
        _bump()
        raise
    _bump()


_DL = {}   # fichero -> hilo de descarga
_DL_ERR = {}
_DL_AT = {}   # fichero -> cuando se intento por ultima vez (sin red: se reintenta cada minuto, no en bucle)


def _have(spec):
    """El modelo ya esta en disco; si no, se descarga en otro hilo (una vez) y se reintenta luego."""
    fp = _model_path(spec)
    if os.path.isfile(fp) and os.path.getsize(fp) == spec["size"]:
        return True
    th = _DL.get(spec["file"])
    if (not th or not th.is_alive()) and time.time() - _DL_AT.get(spec["file"], 0) > 60:
        _DL_AT[spec["file"]] = time.time()

        def run():
            try:
                _download(spec, None)
                _DL_ERR.pop(spec["file"], None)
            except Exception as e:
                _DL_ERR[spec["file"]] = str(e)[:120]
            _WAKE.set()
        th = _DL[spec["file"]] = threading.Thread(target=run, daemon=True)
        th.start()
    return False


def _w2v_fetch():
    _have(W2V)


def _w2v_ok():
    return _have(W2V)


def _quiet():
    """Nadie cantando: sala, presentacion antes de la cuenta atras o resultados ya asentados."""
    n = S["now"]
    if not n:
        return True
    ph = n.get("phase")
    return ph == "intro" or (ph == "results" and time.time() - n.get("pt", 0) > 2.5)


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


def _melody(np, voc, mix):
    """Melodia de la cancion entera (para la mejora HQ5)."""
    def dec(x):
        x = x[: len(x) // _PD * _PD]
        return x.reshape(-1, _PD).mean(axis=1)
    vd, md = dec(voc), dec(mix)
    out = []
    nfr = -(-len(voc) // PITCH_HOP)
    for a in range(0, nfr, 2000):
        out += _yin(np, vd, md, a, min(nfr, a + 2000))
    return out


# ---------------------------------------------------------------- alineado / verificacion de la letra (CTC)
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


def _lyric_conf(np, E, toks):
    """Cuanto se parece lo cantado a la letra: media, por cada letra esperada, de su mejor probabilidad en el
    tramo donde el alineado la coloca. Tarareo o silencio dan ~0.05; cantar la letra ~0.35."""
    if not toks or len(E) < 2 * len(toks) + 2:
        return None
    path = _viterbi(np, E, toks)
    P = np.exp(E)
    conf = []
    for k in range(len(toks)):
        fr = np.where(path == 2 * k + 1)[0]
        if not len(fr):
            conf.append(0.0)
            continue
        conf.append(float(P[max(0, fr[0] - 2): min(len(E), fr[-1] + 3), toks[k]].max()))
    return float(np.mean(conf))


def _words_of(text):
    return re.findall(r"\S+", text or "")


def _sung_lines(lyr):
    """Lineas con letra (mismo filtro que la pantalla: sin las vacias ni las de solo ♪)."""
    return [l for l in (lyr or {}).get("lines") or [] if re.sub(r"[♪♫♬~\s]", "", l.get("text") or "")]


def _place(np, E, t0, words, pitch, bias=ALIGN_BIAS):
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
    st = [t0 + first[i] * .02 - bias if i in first else None for i in range(n)]
    en = [t0 + (last[i] + 1) * .02 - bias if i in last else None for i in range(n)]
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


def _line_spans(s):
    """(inicio, fin) en segundos de cada linea con letra, para verificar lo que canta cada cual."""
    if (s["lyrics"] or {}).get("type") == "Static":
        return []
    lines = _sung_lines(s["lyrics"])
    out = []
    for i, l in enumerate(lines):
        syl = [x for x in l.get("syllabus") or [] if (x.get("text") or "").strip()]
        if syl:
            a = min(x["time"] for x in syl) / 1000
            b = max(x["time"] + max(60, x.get("duration") or 0) for x in syl) / 1000
        else:
            a = (l.get("time") or 0) / 1000
            nt = lines[i + 1]["time"] / 1000 if i + 1 < len(lines) else a + 6
            b = min(nt, a + 8)
        out.append((a, b, l.get("text") or "".join(x["text"] for x in syl)))
    return out


# ---------------------------------------------------------------- canciones (cache en disco + procesado)
SONGS = {}   # vid -> estado publico de su procesado
_GENS = {}   # vid -> generador en curso
_UPS = {}    # vid -> generador de la mejora a HQ5
_PREF = {}   # vid -> hilo de precarga de audio/letra
_SPL_TAG = "spleeter-2stems"


def _song_dir(vid):
    return os.path.join(_dir("karaoke"), vid)


def _song(vid, info=None):
    s = SONGS.get(vid)
    if s is None:
        s = {"vid": vid, "status": "wait", "progress": 0, "chunks": 0, "nch": 0, "n": 0, "dur": 0, "pitch": [],
             "lyrics": None, "lrev": 0, "align": "", "aligned_upto": 0, "error": "", "title": "", "artist": "",
             "catdur": 0, "model": ""}
        meta = os.path.join(_song_dir(vid), "meta.json")
        try:
            with open(meta, encoding="utf-8") as f:
                m = json.load(f)
            if m.get("step") == STEP and m.get("model") in (MODEL["file"], _SPL_TAG) and all(
                    os.path.isfile(os.path.join(_song_dir(vid), "%03d.pcm" % i)) for i in range(m["nch"])):
                s.update(m, status="ready", progress=100, chunks=m["nch"])
                if s["align"] != "pending":
                    s["aligned_upto"] = s["dur"]
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
            s.update(align="pending", aligned_upto=1e9 if s["lyrics"].get("type") == "Static" else 0)
            _WAKE.set()
        else:
            s["aligned_upto"] = 1e9
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
    meta = {k2: s[k2] for k2 in ("n", "dur", "nch", "pitch", "title", "artist", "catdur", "lyrics", "lrev", "align", "model")}
    meta.update(step=STEP, sr=SR)
    with open(os.path.join(_song_dir(s["vid"]), "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _bias(s):
    return ALIGN_BIAS_SPL if s.get("model") == _SPL_TAG else ALIGN_BIAS


def _align_steps(np, s, v16, upto):
    """Pasos de alineado de lo que ya tiene voz separada hasta `upto` segundos (uno por `yield`)."""
    lyr = s["lyrics"]
    if s["align"] != "pending" or not lyr:
        return
    if not _w2v_ok():
        if W2V["file"] in _DL_ERR and not _DL[W2V["file"]].is_alive():
            s["align"] = "error"
            s["aligned_upto"] = 1e9
            return
        yield "wait"
        return
    lines = [l for l in lyr["lines"] if _words_of(l.get("text"))]
    done = s.setdefault("_aligned", set())
    if lyr.get("type") == "Line":
        for i, l in enumerate(lines):
            a = max(0, l["time"] / 1000 - .35)
            nt = lines[i + 1]["time"] / 1000 if i + 1 < len(lines) else min(s["dur"], a + 12)
            b = min(nt + .25, a + 15, s["dur"])
            if i in done:
                continue
            if b > upto:
                return
            words = _words_of(l["text"])
            if b - a > .5 and _toks(l["text"]):
                E = _cpu("emis", v16[int(a * 16000): int(b * 16000)])
                times = _place(np, E, a, words, s["pitch"], _bias(s))
                if times:
                    l["syllabus"] = _syllabus(words, times)
                    l["time"] = l["syllabus"][0]["time"]
                    s["lrev"] += 1
            done.add(i)
            s["aligned_upto"] = nt   # todo lo anterior a la siguiente linea ya esta alineado
            _bump(soft=True)
            yield
    elif lyr.get("type") == "Static" and upto >= s["dur"] - .1:
        # solo texto: la cancion entera de una vez (ventanas de 30 s para las emisiones)
        Es = s.setdefault("_E", [])
        for a in range(len(Es) * 30, int(s["dur"]) + 1, 30):
            Es.append(_cpu("emis", v16[a * 16000: min(len(v16), (a + 30) * 16000)]))
            yield
        E = np.concatenate([e for e in Es if len(e)])
        s.pop("_E", None)
        words = [w for l in lines for w in _words_of(l["text"])]
        times = _place(np, E, 0, words, s["pitch"], _bias(s))
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
    s.update(align="done", aligned_upto=1e9)
    s["lrev"] += 1
    _bump()


def _job(vid):
    """Separa una cancion (Spleeter) por pasos; cada `yield` es un punto donde el hilo puede cambiar de trabajo."""
    import numpy as np
    s = SONGS[vid]
    d = _song_dir(vid)
    os.makedirs(d, exist_ok=True)
    if s["status"] == "ready":   # ya separada (cache): solo falta alinear la letra con la voz guardada
        _get_lyrics(s)
        v16 = _load_v16(np, s)
        yield
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
    s.update(n=N, dur=round(N / SR, 3), nch=nch, chunks=0, pitch=[], status="sep", model=_SPL_TAG)
    threading.Thread(target=_get_lyrics, args=(s,), daemon=True).start()
    _bump()
    yield
    # STFT global con hop 1024 (center=True): los bloques de 512 frames se suman en un solo buffer y se
    # normalizan con la envolvente global -> exactamente el mismo resultado que de una pieza, sin costuras
    N2, H, T = SP_N // 2, SP_H, SP_T
    xpad = np.pad(mix, ((0, 0), (N2, N2 + T * H)), mode="constant")
    xpad[:, :N2] = mix[:, N2:0:-1]   # reflejo al principio (como center=True)
    nfr = 1 + N // H
    win2 = ((0.5 - 0.5 * np.cos(2 * np.pi * np.arange(SP_N) / SP_N)) ** 2).astype(np.float32)
    env = np.zeros(xpad.shape[1], np.float32)
    for q in range(SP_N // H):
        env[q * H: q * H + nfr * H] += np.tile(win2[q * H:(q + 1) * H], nfr)
    out = np.zeros_like(xpad)
    vd = np.zeros(nch * STEP // _PD + 1, np.float32)   # voz y mezcla mono a 14.7 kHz para la melodia
    md = np.zeros_like(vd)
    v16 = np.zeros(int(N * 16000 / SR) + 2, np.float32)   # voz a 16 kHz para el alineado de la letra
    pf, k, nfr_done = 0, 0, 0
    nfrm = -(-N // PITCH_HOP)
    t_sep = time.time()
    while k < nch:
        if nfr_done < nfr:
            f0, f1 = nfr_done, min(nfr, nfr_done + T)
            seg = xpad[:, f0 * H: (f1 - 1) * H + SP_N]
            t = time.time()
            ola = _cpu("spl", (seg, f1 - f0))
            ENG["speed"] = round(.6 * ENG["speed"] + .4 * (f1 - f0) * H / SR / max(.05, time.time() - t), 1)
            out[:, f0 * H: f0 * H + ola.shape[1]] += ola
            nfr_done = f1
        final = N if nfr_done >= nfr else nfr_done * H - N2   # muestras ya definitivas (coordenadas originales)
        while k < nch and min(N, (k + 1) * STEP) <= final:
            a = k * STEP
            n = min(STEP, N - a)
            ins = out[:, N2 + a: N2 + a + n] / np.maximum(env[N2 + a: N2 + a + n], 1e-6)
            m = mix[:, a: a + n]
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
            upto = nfrm if k == nch - 1 else max(pf, ((a + n) // _PD - _YW - _TMAX) // _PH)
            s["pitch"].extend(_yin(np, vd, md, pf, min(upto, nfrm)))
            pf = min(upto, nfrm)
            k += 1
            s.update(chunks=k, progress=int(k * 100 / nch))
        _bump(soft=True)
        yield
        if s["lyrics"] is not None:
            yield from _align_steps(np, s, v16, min(N, k * STEP) / SR - .2)
    del out, xpad
    s.update(status="ready", progress=100, sep_secs=round(time.time() - t_sep, 1))
    _bump()
    _get_lyrics(s)
    _save_meta(s)
    while s["align"] == "pending":
        yield from _align_steps(np, s, v16, s["dur"])
        if s["align"] == "pending":
            yield "wait"
    _save_meta(s)
    _evict()


def _load_v16(np, s):
    d = _song_dir(s["vid"])
    v16 = []
    for k in range(s["nch"]):
        with open(os.path.join(d, "%03d.pcm" % k), "rb") as f:
            b = np.frombuffer(f.read(), "<i2")
        n = len(b) // 3
        v16.append(_to16(np, b[2 * n:].astype(np.float32) / 32767, k * STEP)[1])
    return np.concatenate(v16)


def _upgrade(vid):
    """Mejora a MDX-Net HQ5 con la CPU libre (nadie cantando): mas limpia que Spleeter. Se escribe aparte y se
    cambia de golpe cuando la cancion no esta sonando; la proxima vez que se cante ya suena mejor."""
    import numpy as np
    s = SONGS[vid]
    d = _song_dir(vid)
    while not _have(MODEL):
        if MODEL["file"] in _DL_ERR and not _DL[MODEL["file"]].is_alive():
            raise IOError(_DL_ERR[MODEL["file"]])
        yield "wait"
    mix = _decode(np, _get_audio(vid))
    N = mix.shape[1]
    if N != s["n"]:
        s["model"] = MODEL["file"]   # el audio cambio: no se mejora
        return
    pad = np.zeros((2, _TRIM + N + _CHUNK), np.float32)
    pad[:, _TRIM:_TRIM + N] = mix
    ramp = np.linspace(0, 1, _XF, dtype=np.float32)
    tail, vocs = None, []
    for k in range(s["nch"]):
        a = k * STEP
        seg = pad[:, a: a + _CHUNK]
        while not _quiet():   # alguien canta: la mejora espera (la CPU es para lo que suena)
            yield "wait"
        if float(np.abs(seg).max()) < 1e-4:
            inst = np.zeros((2, _CHUNK - 2 * _TRIM), np.float32)
        else:
            inst = _cpu("sep", seg)[:, _TRIM: _CHUNK - _TRIM]
        if tail is not None:
            inst[:, :_XF] = tail * (1 - ramp) + inst[:, :_XF] * ramp
        tail = inst[:, STEP: STEP + _XF].copy()
        n = min(STEP, N - a)
        m = mix[:, a: a + n]
        voc = (m - inst[:, :n]).mean(axis=0)
        vocs.append(voc)
        with open(os.path.join(d, "hq_%03d.pcm" % k), "wb") as f:
            f.write((np.clip(np.concatenate([inst[0, :n], inst[1, :n], voc]), -1, 1) * 32767).astype("<i2").tobytes())
        yield
    pitch = _melody(np, np.concatenate(vocs), mix.mean(axis=0))
    while S["now"] and S["now"]["vid"] == vid:   # sonando ahora: el cambio, al terminar
        yield "wait"
    for k in range(s["nch"]):
        os.replace(os.path.join(d, "hq_%03d.pcm" % k), os.path.join(d, "%03d.pcm" % k))
    s.update(model=MODEL["file"], pitch=pitch)
    _save_meta(s)
    _bump(soft=True)


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
        if v in keep or v in _GENS or v in _UPS:
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


def _step(table, vid, make):
    """Un paso del generador de `vid`; devuelve lo que entrega ("wait" = no pudo avanzar)."""
    g = table.get(vid)
    if g is None:
        g = table[vid] = make(vid)
    try:
        return next(g)
    except StopIteration:
        table.pop(vid, None)
        return None


def _worker():
    # prioridad normal a proposito: con el GIL en la mano, un hilo de baja prioridad sin CPU dejaria esperando a
    # los hilos que atienden a la pantalla y a los moviles (lo pesado va en el proceso de IA, de prioridad baja)
    while True:
        if not S["on"]:
            _WAKE.wait(5)
            _WAKE.clear()
            continue
        if ENG["status"] != "ready":
            time.sleep(1.5)   # que la sala se pinte antes de cargar nada
            try:
                _engine()
            except Exception:
                for v in _order():   # sin motor no hay pista sin voz: que no se queden esperando
                    if SONGS.get(v) and SONGS[v]["status"] not in ("ready", "error"):
                        SONGS[v].update(status="error", error="motor de voz no disponible")
                _bump()
                _WAKE.wait(30)
                _WAKE.clear()
                continue
        try:
            if _verify_step():   # lo primero: comprobar lo que canta quien esta cantando
                continue
        except Exception:
            pass
        stepped = False
        for vid in [v for v in _order() if _pending(v)]:
            try:
                r = _step(_GENS, vid, _job)
                if r is None and SONGS[vid]["align"] == "pending" and vid not in _GENS:
                    SONGS[vid]["align"] = "done"   # la letra no se pudo alinear entera: se deja como esta
            except Exception as e:
                _GENS.pop(vid, None)
                s = SONGS[vid]
                if s["status"] == "ready":   # fallo del alineado: la cancion se puede cantar igual
                    s.update(align="error", aligned_upto=1e9)
                else:
                    s.update(status="error", error=str(e)[:160])
                _bump()
                r = None
            if r != "wait":
                stepped = True
                break
        if not stepped and _quiet():   # todo listo y nadie canta: mejorar lo que viene y lo ya cantado (la proxima vez)
            ups, seen = _order(), set(_order())
            for h in reversed(S["history"]):
                if h.get("vid") and h["vid"] not in seen:
                    seen.add(h["vid"])
                    ups.append(h["vid"])
            for vid in ups:
                s = SONGS.get(vid)
                if not s or s["status"] != "ready" or s.get("model") != _SPL_TAG:
                    continue
                try:
                    r = _step(_UPS, vid, _upgrade)
                except Exception:
                    _UPS.pop(vid, None)
                    s["model"] = MODEL["file"] + "?"   # no reintentar en esta sesion
                    r = None
                if r != "wait":
                    stepped = True
                    break
        if not stepped:
            _WAKE.wait(.5)
            _WAKE.clear()


# ---------------------------------------------------------------- verificacion de lo que se canta
_AUD = {}   # quien -> deque de (t0 del PC, muestras int16 a 16 kHz) de su micro (los ultimos ~90 s)
_AUD_LAG = {}   # quien -> lo que tarda en llegar su audio (sube al instante, baja despacio)


def audio_in(who, t0, raw):
    import numpy as np
    if not S["now"] or len(raw) > 200000:
        return {"ok": False}
    q = _AUD.setdefault(who, deque())
    x = np.frombuffer(raw[: len(raw) // 2 * 2], "<i2").copy()
    t0 = float(t0)
    if q:   # el micro graba sin cortes: si encaja con el lote anterior (salvo unos ms de reloj), va pegado a el
        end = q[-1][0] + len(q[-1][1]) / 16000
        if abs(t0 - end) < .04:
            t0 = end
    q.append((t0, x))
    lag = min(5.0, max(0.0, time.time() - float(t0) - len(x) / 16000))
    old = _AUD_LAG.get(who, .1)
    _AUD_LAG[who] = lag if lag > old else old + (lag - old) * .05
    while q and q[0][0] < time.time() - 90:
        q.popleft()
    return {"ok": True}


def _audio_span(who, w0, w1):
    """Audio del micro de `who` entre dos instantes del reloj del PC (huecos en silencio)."""
    import numpy as np
    out = np.zeros(max(0, int((w1 - w0) * 16000)), np.float32)
    got = 0
    for t0, x in list(_AUD.get(who) or []):
        a = int(round((t0 - w0) * 16000))
        if a >= len(out) or a + len(x) <= 0:
            continue
        lo, hi = max(0, a), min(len(out), a + len(x))
        out[lo:hi] = x[lo - a: hi - a] / 32768
        got += hi - lo
    return out, got / max(1, len(out))


_V16 = {}   # (vid, modelo, trozo) -> voz separada a 16 kHz (los ultimos trozos usados)


def _voc16(np, s, t0, t1):
    """Voz separada (la que suena como voz guia) a 16 kHz entre dos instantes de la cancion."""
    out = np.zeros(max(0, int((t1 - t0) * 16000)), np.float32)
    i0 = int(t0 * 16000)
    for k in range(max(0, int(t0 * SR // STEP)), min(s["nch"], int(t1 * SR // STEP) + 1)):
        key = (s["vid"], s.get("model"), k)
        if key not in _V16:
            with open(os.path.join(_song_dir(s["vid"]), "%03d.pcm" % k), "rb") as f:
                b = np.frombuffer(f.read(), "<i2")
            n = len(b) // 3
            _V16[key] = _to16(np, b[2 * n:].astype(np.float32) / 32767, k * STEP)
            while len(_V16) > 6:
                _V16.pop(next(iter(_V16)))
        j0, y = _V16[key]
        lo, hi = max(i0, j0), min(i0 + len(out), j0 + len(y))
        if hi > lo:
            out[lo - i0: hi - i0] = y[lo - j0: hi - j0]
    return out


def _band(np, z):
    """150 Hz - 3.5 kHz: la voz, sin el retumbe de los graves ni lo que el remuestreo dobla de los agudos."""
    L = 1 << int(np.ceil(np.log2(max(2, len(z)))))
    Z = np.fft.rfft(z.astype(np.float64), L)
    f = np.fft.rfftfreq(L, 1 / 16000)
    Z[(f < 150) | (f > 3500)] = 0
    return np.fft.irfft(Z, L)[: len(z)]


def _guide_share(np, x, y, slack, seg=4096, hop=1024):
    """Que parte de lo captado `x` es la voz guia que sale por los altavoces (0..1). `y` es la voz original desde
    `slack` muestras antes hasta otras tantas despues. Se alinea por correlacion (toda la linea y luego segundo a
    segundo: da igual que los relojes se desvien un poco) y se mide la coherencia espectral, ponderada por la
    energia captada en la banda de la voz. Aguanta el eco de la sala (guia sola: 0.74-0.99; guia y musica sin
    cantar: 0.6+), mientras que una persona, aunque cante lo mismo, da < 0.1 (su onda no se parece a la original)
    y cantando con la guia 12 dB por debajo, ~0.27."""
    W, n = 16000, len(x)
    if n < W:
        return 0.0
    if len(y) < n + 2 * slack:   # lo captado dura unas muestras mas (relojes): se completa con silencio
        y = np.pad(y, (0, n + 2 * slack - len(y)))
    xb, yb = _band(np, x), _band(np, y)
    L = 1 << int(np.ceil(np.log2(len(yb) + n)))   # desplazamiento de toda la linea...
    kp = int(np.argmax(np.abs(np.fft.irfft(np.fft.rfft(yb, L) * np.conj(np.fft.rfft(xb, L)), L)[: 2 * slack + 1])))
    win = np.hanning(seg)
    f = np.fft.rfftfreq(seg, 1 / 16000)
    m = (f >= 150) & (f <= 3500)
    sxy, sxx, syy = 0, 0, 0
    ex = float((xb * xb).mean())
    R = 1280   # ...y cada segundo se sigue su deriva (+-80 ms)
    for o in range(0, n - W + 1, W):
        a = xb[o: o + W]
        if float((a * a).mean()) < .1 * ex:   # casi en silencio: no dice nada
            continue
        k0, k1 = max(0, kp - R), min(2 * slack, kp + R)
        bb = yb[o + k0: o + k1 + W]
        L = 1 << int(np.ceil(np.log2(len(bb) + W)))
        c = np.fft.irfft(np.fft.rfft(bb, L) * np.conj(np.fft.rfft(a, L)), L)[: len(bb) - W + 1]
        kp = k0 + int(np.argmax(np.abs(c)))
        xa, ya = x[o: o + W].astype(np.float64), y[o + kp: o + kp + W].astype(np.float64)
        idx = range(0, W - seg + 1, hop)
        X = np.fft.rfft(np.stack([xa[i: i + seg] * win for i in idx]), axis=1)[:, m]
        Y = np.fft.rfft(np.stack([ya[i: i + seg] * win for i in idx]), axis=1)[:, m]
        sxy = sxy + np.abs((X * np.conj(Y)).sum(0))   # en modulo: cada ventana trae su propio desfase
        sxx = sxx + (np.abs(X) ** 2).sum(0)
        syy = syy + (np.abs(Y) ** 2).sum(0)
    if isinstance(sxx, int):
        return 0.0
    C = np.abs(sxy) ** 2 / np.maximum(sxx * syy, 1e-20)
    return float((C * sxx).sum() / max(1e-20, float(sxx.sum())))


def _wall_of(an, p):
    """Instante del PC en que sonaba el momento `p` de la cancion. Con el historial de anclas (una por segundo y
    otra en cada pausa o paron por falta de pista): aguanta los parones y los relojes que se desvian."""
    h = an.get("hist") or [(an["pos"], an["wall"])]
    j = bisect.bisect_left([q[0] for q in h], p)
    if j == 0:
        return h[0][1] + (p - h[0][0])
    if j == len(h):
        return h[-1][1] + (p - h[-1][0])
    (p0, w0), (p1, w1) = h[j - 1], h[j]
    return w1 if p1 - p0 < 1e-3 else w0 + (p - p0) * (w1 - w0) / (p1 - p0)


def _verify_step():
    """Si una linea ya se canto, su audio contra la letra (una por llamada). True si trabajo."""
    import numpy as np
    now = S["now"]
    an = S.get("anchor")
    if not now or now.get("phase") not in ("sing", "results") or not an or an.get("qid") != now["qid"] or not an.get("src"):
        return False
    s = SONGS.get(now["vid"])
    if not s or not s["lyrics"] or not _w2v_ok():
        return False
    pos = an["pos"] + (time.time() - an["wall"] if an.get("run", True) else 0)   # parada: la cancion no avanza
    ver = S["verify"].setdefault(now["qid"], {})
    who = now["by"] if an["src"] == "phone" else "pc"
    # lo ultimo cantado aun viaja (lotes de 250 ms + la wifi): se espera a que llegue
    tail = min(3.0, max(.7, _AUD_LAG.get(who, .3) + .55))
    for i, (a, b, text) in enumerate(_line_spans(s)):
        if b + tail > pos:   # en orden: lo que sigue aun no se ha cantado
            break
        if i in ver:
            continue
        if a < an.get("from", 0) - .5:   # empezo antes de que llegara su micro
            ver[i] = None
            continue
        # instante del PC en que el cantante oia ese momento de la cancion (+ lo que tarda el sonido en salir)
        w0 = _wall_of(an, a - .25) + an.get("lat", .06)
        w1 = _wall_of(an, b + .35) + an.get("lat", .06)
        x, cover = _audio_span(who, w0, w1)
        toks = _toks(text)
        if not toks or cover < .5:   # sin letra verificable u otro alfabeto: no se penaliza
            ver[i] = 1.0 if not toks else None
            continue
        if float(np.sqrt((x * x).mean())) < .004:   # no canto
            ver[i] = 0.0
            continue
        conf = _lyric_conf(np, _cpu("emis", x), toks)
        c = 1.0 if conf is None else min(1.0, max(0.0, (conf - VERIFY_LO) / (VERIFY_HI - VERIFY_LO)))
        if S["guide"] > 0:   # con voz guia: si lo que entra al micro es la guia de los altavoces, no es quien canta
            pb = min(1.0, max(0.0, (_guide_share(np, x, _voc16(np, s, a - .85, b + .95), 9600) - .3) / .3))
            c = -1.0 if pb >= 1 else c * (1 - pb)   # -1: solo se oia la guia (la linea no puntua)
        ver[i] = round(c, 2)
        return True
    return False


# ---------------------------------------------------------------- sesion (estado compartido PC <-> moviles)
S = {"on": False, "code": "", "ip": "", "port": 0, "tport": 0, "v": 0, "players": {}, "queue": [], "now": None,
     "history": [], "live": None, "reacts": [], "qid": 0, "rid": 0, "diff": "normal", "guide": 0, "cmds": [], "cid": 0,
     "anchor": None, "verify": {}}
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
                 players={}, queue=[], now=None, history=[], live=None, reacts=[], cmds=[], tport=0,
                 anchor=None, verify={})
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
    S.update(on=False, now=None, queue=[], live=None, anchor=None)
    _GENS.clear()   # lo a medio procesar se suelta (memoria); al volver se rehace desde el principio
    _UPS.clear()
    _AUD.clear()
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
    S["anchor"] = None
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


def live(d):
    """Cada segundo, mientras se canta: puntos en directo para los moviles + ancla del reloj de la cancion (para
    encontrar en el audio del micro lo cantado en cada linea). Responde con las lineas ya verificadas."""
    now = S["now"]
    if not now:
        return {"ok": False}
    S["live"] = {"qid": now["qid"], "score": int(d.get("score") or 0), "rating": str(d.get("rating") or "")[:24]}
    try:
        src = d.get("src") if d.get("src") in ("pc", "phone") else None
        old = S.get("anchor") or {}
        same = old.get("qid") == now["qid"] and old.get("src") == src
        pos, wall = float(d["pos"]), float(d["wall"])
        hist = old["hist"] if same and old.get("hist") is not None else deque(maxlen=1200)
        if not hist or (pos >= hist[-1][0] and wall > hist[-1][1]):
            hist.append((pos, wall))
        S["anchor"] = {"qid": now["qid"], "pos": pos, "wall": wall, "lat": float(d.get("lat") or .06), "src": src,
                       "from": old.get("from") if same else pos, "hist": hist, "run": d.get("run") is not False}
    except (KeyError, TypeError, ValueError):
        pass
    _WAKE.set()
    _bump()
    return {"ok": True, "vok": _w2v_ok(), "verify": {str(k): v for k, v in (S["verify"].get(now["qid"]) or {}).items()}}


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
    S["history"].append({"qid": now["qid"], "vid": now["vid"], "title": now["title"], "artist": now["artist"],
                         "cover": now["cover"], "by": now["by"], "score": sc, "diff": S["diff"], "t": int(time.time())})
    S["live"] = None
    S["anchor"] = None
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
    out.update(sr=SR, step=STEP, hop=PITCH_HOP, pitch=s["pitch"][frm:], speed=ENG["speed"],
               aligned_upto=min(s["aligned_upto"], 1e6), **{"from": frm})
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
        if n > 220000:
            self.close_connection = True
            return self._out({"error": "demasiado grande"}, status=413)
        raw = self.rfile.read(n)
        if not self._ok_room(qs):
            return self._out({"error": "room"}, status=403)
        if u.path == "/k/audio":   # audio crudo del micro (16 kHz, int16): para verificar la letra
            p = _player(self.headers.get("X-Key"))
            if not p or not S["now"] or S["now"]["by"] != p["id"]:
                return self._out({"ok": False})
            try:
                return self._out(audio_in(p["id"], float(self.headers.get("X-T") or 0), raw))
            except ValueError:
                return self._out({"ok": False})
        if n > 16384:
            return self._out({"error": "demasiado grande"}, status=413)
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
def host_api(method, path, qs, d, raw=None, headers=None):
    g = lambda k, dv="": (qs.get(k) or [dv])[0]
    if path == "/kara/audio":   # audio del micro del PC (binario, 16 kHz int16)
        try:
            return audio_in("pc", float((headers or {}).get("X-T") or 0), raw or b"")
        except ValueError:
            return {"ok": False}
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
        return live(d)
    if path == "/kara/result":
        return result(d.get("score"))
    if path == "/kara/remove":
        return remove(int(d.get("qid") or 0))
    if path == "/kara/move":
        return move(int(d.get("qid") or 0), d.get("to") or 0)
    if path == "/kara/add":   # el PC tambien puede anadir (sin cantante: "Invitado")
        return add("", d.get("song") or {})
    return {"error": "?"}
