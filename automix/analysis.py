"""Analisis de una cancion para el Automix (laboratorio).

Solo numpy + scipy + onnxruntime: lo mismo que hay en el servidor (Termux en Android), para que lo que funcione
aqui se pueda llevar tal cual. Todo en tiempo del archivo (segundos desde el principio del audio).

Saca:
  - tempo, golpes y "unos" (primer golpe de cada compas)
  - tonalidad (rueda Camelot) con su seguridad
  - volumen percibido (LUFS, BS.1770) y energia 1-10 (estilo Mixed In Key: volumen, densidad de ataques, graves)
  - energia por compas y secciones (novedad sobre la autosimilitud por compas, encajadas en frases de 4 compases)
  - voz: donde canta (de la voz separada con Spleeter, si se pide) y la letra con tiempos
"""
import os
import subprocess

import numpy as np
from scipy import signal

SR = 44100
FPS = 100
HOP = SR // FPS          # 441 muestras = 10 ms
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_KK = {"maj": np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]),
       "min": np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])}


def decode(fp):
    flags = 0x08000000 if os.name == "nt" else 0   # sin ventana
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", fp, "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                       capture_output=True, timeout=240, creationflags=flags)
    if r.returncode != 0 or not r.stdout:
        raise IOError("no se pudo descodificar")
    return np.frombuffer(r.stdout, np.float32).reshape(-1, 2).T.copy()


def _fold(b):
    while b < 85:
        b *= 2
    while b > 170:
        b /= 2
    return b


# ------------------------------------------------------------------ ataques del bombo
FLUX_HOP = 128     # 2,9 ms
_LP4 = signal.butter(10, 4400, "low", fs=SR, output="sos")
_dec_last = [None, None]


def dec4(mono):
    """mono a 11025 Hz (paso bajo IIR + 1 de cada 4). resample_poly tardaba ~4 s en el telefono; esto ~0,5. Se
    guarda la ultima: el detector de golpes y la tonalidad usan la misma"""
    if _dec_last[0] is mono:
        return _dec_last[1]
    y = signal.sosfilt(_LP4, mono.astype(np.float32))[::4].astype(np.float32)
    _dec_last[0], _dec_last[1] = mono, y
    return y


def mid_flux(mono, lo=60, hi=3000):
    """Flujo espectral (subidas del espectro en log) solo entre 60 y 3000 Hz: el golpe del bombo y la caja. Sin los
    agudos (charles a contratiempo) ni el sub (en hard techno el rumble llena el hueco entre bombos y su vuelta se
    confundia con el golpe). Devuelve (flujo, segundos de cada valor). Por trozos: poca memoria (servidor)"""
    # a 11025 Hz (hasta 3 kHz sobra): misma ventana (23 ms), mismo paso (2,9 ms) y mismas bandas que a 44,1 kHz,
    # con FFT 4 veces mas pequenas (el telefono tardaba 9 s por cancion)
    D = 4
    sr = SR / D
    N, Hh = 1024 // D, FLUX_HOP // D
    xd = dec4(mono)
    x = np.concatenate([np.zeros(Hh, np.float32), xd])   # ventana "anterior" a la primera
    out, CH = [], Hh * 6000             # ~17 s por trozo; las ventanas de un trozo empiezan en a, a+Hh, ...
    for a in range(0, len(x) - N, CH):
        seg = x[a: a + CH + N]
        f, t, Z = signal.stft(seg, fs=sr, nperseg=N, noverlap=N - Hh, boundary=None, padded=False)
        sel = (f >= lo) & (f < hi)
        L = np.log1p(1000 * np.abs(Z[sel])).astype(np.float32)
        out.append(np.maximum(0, L[:, 1:] - L[:, :-1]).sum(0))   # la 1a ventana solo sirve de "anterior"
    fl = np.concatenate(out)
    tt = (np.arange(len(fl)) * Hh + N / 2) / sr    # centro de cada ventana (en tiempo del audio original)
    return fl, tt


def beat_offset(fl, tt, per, phase, t0=None, t1=None, lim=None):
    """Donde caen los ataques (60-3000 Hz) dentro del golpe, de media entre t0 y t1, respecto a la rejilla
    (phase + k*per). Devuelve (desfase en s, -medio golpe..+medio golpe; nitidez del pico)"""
    t0 = tt[0] if t0 is None else t0
    t1 = tt[-1] if t1 is None else t1
    sel = (tt >= t0) & (tt < t1)
    P = 96
    pos = ((((tt[sel] - phase) / per) + .5) % 1 * P).astype(int) % P
    prof = np.bincount(pos, weights=fl[sel], minlength=P)
    prof = np.convolve(np.r_[prof[-2:], prof, prof[:2]], [.1, .2, .4, .2, .1], "valid")
    if lim is None:
        k = int(prof.argmax())
    else:   # solo cerca de la rejilla actual (afinar, sin saltar a otra posicion del golpe)
        near = np.array([abs((i + .5) / P - .5) <= lim for i in range(P)])
        k = int(np.where(near, prof, -1).argmax())
    y0, y1, y2 = prof[k - 1], prof[k], prof[(k + 1) % P]
    den = y0 - 2 * y1 + y2
    kf = k + (.5 * (y0 - y2) / den if den else 0)
    return ((kf + .5) / P - .5) * per, float(prof.max() / (prof.mean() or 1))


# ------------------------------------------------------------------ tempo, golpes, compases
def rhythm(mono, hint=None):
    n = len(mono) // HOP
    fr = mono[: n * HOP].reshape(n, HOP)
    e = (fr ** 2).sum(1) + 1e-9
    lo = signal.sosfilt(signal.butter(2, 150, "low", fs=SR, output="sos"), mono)[: n * HOP].reshape(n, HOP)
    el = (lo ** 2).sum(1) + 1e-9
    o = np.maximum(0, np.diff(np.log(e), prepend=np.log(e[0])))
    ol = np.maximum(0, np.diff(np.log(el), prepend=np.log(el[0])))
    nf = n
    # 1) primera estimacion: autocorrelacion del minuto central con preferencia por ~120 BPM y refuerzo del compas
    a0 = max(1, nf // 2 - 3000)
    a1 = min(nf - 200, a0 + 6000)
    vals = {L: float(np.dot(o[a0:a1], o[a0 + L: a1 + L])) for L in range(25, 173)}
    best, bl = -1, 60
    for L in range(33, 87):
        prior = np.exp(-.5 * (np.log2((6000 / L) / 120) / .9) ** 2)
        sc = (vals[L] + .5 * vals.get(2 * L, 0)) * prior
        if sc > best:
            best, bl = sc, L
    y0, y1, y2 = vals[bl - 1], vals[bl], vals[bl + 1]
    den = y0 - 2 * y1 + y2
    per0 = bl + (.5 * (y0 - y2) / den if den else 0)
    bpm = _fold(6000 / per0)
    conf = vals[bl] / (np.mean([vals[L] for L in range(33, 87)]) or 1)
    # 2) barrido de la octava 85-170 con un peine sobre los ataques (todos + graves), por ventanas de 20 s;
    #    corrige los errores tipicos de 3/4; los de 2/3 (riffs en tresillo) necesitan mucha mas evidencia
    sm = np.maximum.reduce([np.roll(o, 1), o, np.roll(o, -1)])
    sml = np.maximum.reduce([np.roll(ol, 1), ol, np.roll(ol, -1)])
    cs = sm / (sm.mean() or 1) + sml / (sml.mean() or 1)
    w0, w1, WL = int(nf * .1), int(nf * .9), 2000

    def comb(b):
        pf = 6000 / b
        tot, nw = 0.0, 0
        ks = np.arange(int(WL / pf))
        ph = np.arange(int(pf))
        for a in range(w0, w1 - WL, WL):
            idx = np.rint(a + ph[:, None] + ks[None, :] * pf).astype(int)
            tot += cs[idx].mean(1).max()
            nw += 1
        return tot / nw if nw else 0
    s0 = comb(bpm)
    bb, bs = bpm, 0
    for c in np.arange(85, 170.01, .5):
        sc = comb(c) * (1.04 if abs(c / bpm - 1) < .02 else 1)
        if sc > bs:
            bs, bb = sc, c
    rr = bb / bpm
    tri = abs(rr / (2 / 3) - 1) < .04 or abs(rr / (4 / 3) - 1) < .04
    if abs(rr - 1) >= .02 and bs > s0 * (1.3 if tri else 1.04):
        bpm = bb
    # 2b) comprobacion fina (2 ms) de los errores 3/4 y 2/3: autocorrelacion de los ataques (graves + todo) en el
    #     periodo del golpe; un 4/4 da un pico muy claro en su golpe y casi nada a 4/3 de el (hard techno a 166 que
    #     salia a 124,5)
    H2 = 88
    n2 = len(mono) // H2
    lo2 = signal.sosfilt(signal.butter(2, 150, "low", fs=SR, output="sos"), mono)

    def lod(x):
        e2 = np.log(np.sqrt((x[: n2 * H2].reshape(n2, H2) ** 2).mean(1)) + 1e-5)
        d2 = np.maximum(0, np.diff(e2, prepend=e2[0]))
        d2 = np.convolve(d2, np.ones(3) / 3, "same")
        return d2 / (d2.mean() or 1)
    o2 = lod(lo2) + lod(mono)
    o2 = o2[int(n2 * .1): int(n2 * .9)]
    o2 = o2 - o2.mean()
    fps2 = SR / H2

    def ac2(b):
        L = 60 * fps2 / b
        N = len(o2) - int(L * 1.02) - 2
        if N < fps2 * 20:
            return 0.0
        return max(float(np.dot(o2[:N], o2[k: N + k])) for k in range(int(L * .99), int(L * 1.01) + 2))
    a_cur = ac2(bpm)
    for alt in (bpm * 4 / 3, bpm * 3 / 4, bpm * 3 / 2, bpm * 2 / 3):
        if 85 <= alt <= 180 and ac2(alt) > 1.35 * a_cur > 0:
            bpm, a_cur = alt, ac2(alt)
    if hint and hint > 40:   # BPM del catalogo (Deezer): manda si es el nuestro o uno de sus errores tipicos
        h, rr = _fold(hint), _fold(hint) / bpm
        if any(abs(rr / x - 1) < .03 for x in (1, 3 / 4, 4 / 3, 2 / 3, 3 / 2)):
            bpm = h
    # 3) afinado del tempo y la fase contra toda la cancion (+-2 % en pasos de 0,05 %)
    f0, f1 = int(nf * .05), int(nf * .95)
    base = 6000 / bpm
    bestS, perF, ph = -1, base, 0
    for k in range(-40, 41):
        pf = base * (1 + k * .0005)
        ks = np.arange(int((f1 - f0) / pf))
        phs = np.arange(int(pf))
        idx = np.rint(f0 + phs[:, None] + ks[None, :] * pf).astype(int)
        idx = np.minimum(idx, nf - 1)
        s = o[idx].sum(1)
        j = int(s.argmax())
        if s[j] > bestS:
            bestS, perF, ph = s[j], pf, j
    bpm = 6000 / perF
    per = perF / FPS
    phase = ((f0 + ph) / FPS) % per
    # el pulso pudo caer en los contratiempos (charles): si los graves caen mas en el medio golpe, ahi esta el golpe
    lon = ol

    def at(t):
        f = int(round(t * FPS))
        return lon[max(0, f - 1): f + 2].max() if 0 < f < nf - 1 else 0
    dur = nf / FPS

    def count(phs):
        on = off = 0
        bars = np.zeros(4)
        k = 0
        t = phs
        while t < dur - per:
            if dur * .1 < t < dur * .9:
                v = at(t)
                on += v
                off += at(t + per / 2)
                bars[k % 4] += v
            t += per
            k += 1
        return on, off, bars
    # FASE por el bombo: subida de energia en 40-120 Hz frente a los 60 ms previos, acumulada por posicion dentro
    # del golpe en toda la cancion; el golpe esta donde pega el bombo (con los ataques de todo, la rejilla podia caer
    # medio golpe desplazada: Billie Jean). Precision de 1/48 de golpe (~10 ms)
    kb = signal.sosfilt(signal.butter(4, [40, 120], "band", fs=SR, output="sos"), mono)[: nf * HOP].reshape(nf, HOP)
    ek = (kb ** 2).sum(1) + 1e-12
    pre = np.convolve(ek, np.ones(6) / 6, "full")[:nf]
    rise = np.maximum(0, np.log(ek) - np.log(np.r_[ek[:1], pre[:-1]]))
    tt = np.arange(nf) / FPS
    pos = (((tt - phase) % per) / per * 48).astype(int) % 48
    prof = np.bincount(pos, weights=rise, minlength=48)
    pk = int(prof.argmax())
    if prof[pk] > 1.25 * prof.mean():
        phase = (phase + pk / 48 * per) % per
    # AFINADO con el golpe del bombo (ataques en 60-3000 Hz). En hard techno el sub es rumble que vuelve ~130 ms
    # despues del bombo y la rejilla caia ahi (Sukuna: bombos 145 ms antes que la rejilla). Si el desfase se acerca a
    # medio golpe (bajo a contratiempo: Billie Jean) no se toca
    fl, ftt = mid_flux(mono)
    dur0 = nf / FPS
    off_s, sharp = beat_offset(fl, ftt, per, phase, dur0 * .1, dur0 * .9)
    if sharp < 1.25:
        off_s = 0.0
    elif abs(off_s) > .4 * per:   # casi medio golpe: no se salta alli; solo se afina cerca (todos con el mismo criterio)
        off_s, _ = beat_offset(fl, ftt, per, phase, dur0 * .1, dur0 * .9, lim=.2)
    on, off, bars = count(phase)          # el "uno" se elige con la fase de los graves (probado) y luego se afina
    b1 = int(bars.argmax())
    # ...pero con bombo constante los 4 golpes tienen casi los mismos graves y el "uno" podia caer un golpe
    # desplazado (Pasilda: sus bombos entraban en el "cuatro"). La pista mas fiable: donde ENTRA una seccion de
    # bombos tras un hueco sin graves, ese golpe es un "uno". Si esas entradas coinciden, mandan
    nbt = int((dur - phase) / per)
    lv = np.zeros(nbt)
    for k in range(nbt):
        f = int(round((phase + k * per) * FPS))
        lv[k] = ek[max(0, f - 3): f + 15].max() if f < nf else 0
    refv = np.percentile(lv, 75) if nbt else 1
    absent, present = lv < .1 * refv, lv > .4 * refv
    votes = np.zeros(4)
    for k in range(8, nbt - 8):
        # entra la seccion de bombos tras un paron largo (2 compases o mas sin graves)
        if present[k] and absent[k - 1] and absent[k - 8: k].sum() >= 7 and present[k: k + 8].sum() >= 7:
            votes[k % 4] += 1
    if votes.sum() >= 1:
        bv = int(votes.argmax())
        second = np.sort(votes)[-2]
        if votes[bv] >= 2 and votes[bv] >= 3 * second:
            b1 = bv
    bar0 = phase + off_s + b1 * per     # "uno" del compas: el golpe con mas graves (se reduce al primer compas abajo)
    phase = (phase + off_s) % per
    even = float(bars.min() / (bars.max() or 1))
    four = float(on / off) if off else 1.0
    beats = np.arange(phase, dur, per)
    downs = np.arange(bar0 % (4 * per), dur, 4 * per)
    # bombo por compas: en cuantos de sus 4 golpes pega un bombo (subida clara en 40-120 Hz con nivel suficiente);
    # 0..1. Independiente del volumen del resto: una intro solo de bateria cuenta igual que el tema lleno
    # (se mide en la rejilla de los graves, antes del afinado: asi se valido)
    medk = np.median(ek)
    kick, kpk = [], []
    for d0 in downs:
        c = 0
        for j in range(4):
            f = int(round((d0 - off_s + j * per) * FPS))
            if 2 <= f < nf - 6:
                post = ek[f: f + 6].max()
                prv = ek[max(0, f - 8): f - 2].mean() + 1e-12
                c += post / prv > 1.8 and post / medk > .15
        lowb = ek[int(d0 * FPS): int((d0 + 4 * per) * FPS)].mean()
        kick.append((c / 4, lowb))
        a0, a1 = int(d0 * FPS), int((d0 + 4 * per) * FPS)
        kpk.append(float(np.abs(lo[a0:a1]).max()) if a1 > a0 else 0.0)
    lows = np.array([x[1] for x in kick]) if kick else np.zeros(0)
    refl = np.median(lows[lows > np.percentile(lows, 30)]) if len(lows) else 1
    # con bajo encima el bombo contrasta menos: un compas con graves de verdad (>= 75 % de lo normal) tambien empuja
    kick = np.array([max(c, .6 * min(1.0, lb / (refl or 1))) for c, lb in kick])
    # FUERZA del bombo por compas: pico de graves (<150 Hz) frente a los compases fuertes del tema (1 = como el tramo
    # principal). Distingue un ritmo suave a medio gas (intro de Fantasy, ~0.7) de los bombos de verdad (~1), y la
    # cola que se apaga al final (Sukuna, compas 114: 0.6)
    kpk = np.array(kpk)
    ref = np.percentile(kpk[kick >= .45], 85) if (kick >= .45).sum() >= 4 else (np.percentile(kpk, 85) if len(kpk) else 1)
    kpow = np.round(kpk / (ref or 1), 3)
    return {"kick": kick, "kpow": kpow, "bpm": float(bpm), "per": float(per), "phase": float(phase), "bar0": float(bar0 % (4 * per)),
            "conf": float(conf), "four": four, "even": even, "beats": beats, "downbeats": downs,
            "env": e, "envLow": el, "onsets": o}


# ------------------------------------------------------------------ tonalidad
def key(mono):
    """Cromagrama (magnitud, 100-2500 Hz, cada instante normalizado: los acordes cuentan igual en todo el tema)
    contra los perfiles de Krumhansl. Probado contra 12 temas de tonalidad conocida: 9 exactos y el resto a una
    casilla de la rueda Camelot (relativa o quinta: siguen siendo compatibles para mezclar)"""
    x = dec4(mono)                                # 11025 Hz
    sr = SR / 4
    f, t, Z = signal.stft(x.astype(np.float32), fs=sr, nperseg=8192, noverlap=6144, boundary=None)
    M = np.abs(Z)
    ok = (f >= 100) & (f <= 2500)
    pc = ((np.rint(12 * np.log2(f[ok] / 440.0)) + 9) % 12).astype(int)
    W = M[ok]
    C = np.zeros((12, W.shape[1]))
    for p in range(12):
        C[p] = W[pc == p].sum(0)
    C = C / (C.sum(0, keepdims=True) + 1e-9)
    e = M.sum(0)
    chroma = C[:, e > np.percentile(e, 30)].mean(1)
    scores = []
    for m in ("maj", "min"):
        prof = _KK[m]
        for r in range(12):
            scores.append((np.corrcoef(np.roll(chroma, -r), prof)[0, 1], r, m))
    scores.sort(reverse=True)
    sc, r, m = scores[0]
    majpc = r if m == "maj" else (r + 3) % 12
    cam = ((majpc * 7) % 12 + 7) % 12 + 1
    return {"name": NAMES[r] + ("" if m == "maj" else "m"), "pc": r, "mode": m,
            "camelot": "%d%s" % (cam, "B" if m == "maj" else "A"), "cam": cam, "letter": "B" if m == "maj" else "A",
            "conf": float(sc - scores[1][0]), "chromaFrames": (M, f, t, sr)}


def camelot_dist(a, b):
    d = min((a["cam"] - b["cam"]) % 12, (b["cam"] - a["cam"]) % 12)
    return d if a["letter"] == b["letter"] else (1 if d == 0 else d + 1)


# ------------------------------------------------------------------ volumen percibido (BS.1770) y energia
def _biquad(kind, f0, q, gain_db, fs):
    A = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / fs
    al = np.sin(w) / (2 * q)
    c = np.cos(w)
    if kind == "hs":
        b = [A * ((A + 1) + (A - 1) * c + 2 * np.sqrt(A) * al), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - 2 * np.sqrt(A) * al)]
        a = [(A + 1) - (A - 1) * c + 2 * np.sqrt(A) * al, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - 2 * np.sqrt(A) * al]
    else:   # paso alto
        b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]
        a = [1 + al, -2 * c, 1 - al]
    return np.array(b) / a[0], np.array(a) / a[0]


def lufs(x):
    b1, a1 = _biquad("hs", 1681.97, .7072, 4.0, SR)
    b2, a2 = _biquad("hp", 38.135, .5003, 0, SR)
    sos = np.vstack([signal.tf2sos(b1, a1), signal.tf2sos(b2, a2)])
    y = signal.sosfilt(sos, x.astype(np.float32), axis=1)
    blk, step = int(.4 * SR), int(.1 * SR)
    cs = np.concatenate([[0.0], np.cumsum((y * y).sum(0), dtype=np.float64)])   # bloques de 400 ms cada 100 ms
    st = np.arange(0, y.shape[1] - blk, step)
    ms = (cs[st + blk] - cs[st]) / blk
    if not len(ms):
        return -70.0
    L = -0.691 + 10 * np.log10(ms + 1e-12)
    g = ms[L > -70]
    if not len(g):
        return -70.0
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g2 = ms[(L > -70) & (L > rel)]
    return float(-0.691 + 10 * np.log10(g2.mean() + 1e-12))


def energy_level(loud, onsets, envLow, env, bpm):
    """1-10, al estilo de Mixed In Key (que no publica la formula): volumen, densidad de ataques, graves y tempo"""
    pk, _ = signal.find_peaks(onsets, height=np.percentile(onsets, 90), distance=8)
    dens = len(pk) / (len(onsets) / FPS)
    lowr = float(envLow.sum() / (env.sum() or 1))
    nz = lambda v, a, b: min(1, max(0, (v - a) / (b - a)))
    s = .45 * nz(loud, -18, -6) + .30 * nz(dens, 1, 5) + .15 * nz(bpm, 80, 150) + .10 * nz(lowr, .15, .6)
    return int(round(1 + 9 * s)), {"density": round(dens, 2), "lowRatio": round(lowr, 3)}


# ------------------------------------------------------------------ secciones
def sections(r, kinfo, dur, vocal_mask=None, chorus_times=()):
    """Rasgos por compas (cromagrama + timbre en bandas + energia), autosimilitud, novedad con un nucleo de
    tablero de ajedrez y limites encajados en frases de 4 compases desde el "uno". Etiquetas por energia,
    repeticion y letra"""
    bar = 4 * r["per"]
    starts = np.arange(r["bar0"], dur - bar, bar)
    if len(starts) < 12:
        return [], np.zeros(0), {"start": 0, "rep4": [], "rep8": []}
    M, f, t, sr = kinfo["chromaFrames"]
    ok = (f >= 65) & (f <= 2000)
    pc = ((np.rint(12 * np.log2(np.maximum(f, 1) / 440.0)) + 9) % 12).astype(int)
    edges = np.geomspace(60, 5000, 25)
    band = np.digitize(f, edges) - 1
    feats, en = [], []
    env = r["env"]
    for s0 in starts:
        sel = (t >= s0) & (t < s0 + bar)
        X = M[:, sel] ** 2 if sel.any() else np.zeros((len(f), 1))
        ch = np.array([X[ok & (pc == p)].sum() for p in range(12)])
        ch = ch / (np.linalg.norm(ch) or 1)
        tb = np.array([X[band == b].sum() for b in range(24)])
        tb = np.log(tb + 1e-9)
        feats.append(np.concatenate([ch, tb]))
        en.append(np.log10(env[int(s0 * FPS): int((s0 + bar) * FPS)].mean() + 1e-12))
    F = np.array(feats)
    F[:, 12:] = (F[:, 12:] - F[:, 12:].mean(0)) / (F[:, 12:].std(0) + 1e-9) * .35
    Fn = F / (np.linalg.norm(F, axis=1, keepdims=True) + 1e-9)
    S = Fn @ Fn.T
    n = len(S)
    K = 4
    g = np.outer(np.r_[-np.ones(K), np.ones(K)], np.r_[-np.ones(K), np.ones(K)])   # + dentro de cada bloque, - cruzado
    nov = np.zeros(n)
    for i in range(K, n - K):
        nov[i] = (S[i - K:i + K, i - K:i + K] * g).sum()
    nov = np.maximum(0, nov)
    pk, _ = signal.find_peaks(nov, distance=4, height=np.percentile(nov, 70) if n > 8 else None)
    # a frases de 4 compases si queda a 1 compas
    bnd = sorted({int(4 * round(p / 4)) if abs(p - 4 * round(p / 4)) <= 1 else int(p) for p in pk})
    bnd = [b for b in bnd if 2 <= b <= n - 2]
    cuts = [0] + bnd + [n]
    en = np.array(en)
    med = np.median(en)
    segs = []
    for i in range(len(cuts) - 1):
        a, b = cuts[i], cuts[i + 1]
        if b - a < 2 and segs:
            segs[-1]["b"] = b
            continue
        segs.append({"a": a, "b": b})
    # parecido entre secciones (para encontrar el estribillo que se repite)
    for s in segs:
        s["vec"] = Fn[s["a"]:s["b"]].mean(0)
        s["e"] = float(en[s["a"]:s["b"]].mean())
    for i, s in enumerate(segs):
        sims = [float(np.dot(s["vec"], o["vec"]) / (np.linalg.norm(s["vec"]) * np.linalg.norm(o["vec"]) + 1e-9))
                for j, o in enumerate(segs) if j != i]
        s["rep"] = sum(1 for x in sims if x > .93)
    top = np.percentile([s["e"] for s in segs], 60)
    out = []
    for i, s in enumerate(segs):
        t0, t1 = float(starts[s["a"]]), float(starts[s["b"] - 1] + bar)
        has_ch = any(t0 - 1 <= c < t1 - 2 for c in chorus_times)
        voc = None
        if vocal_mask is not None:
            a, b = int(t0 * FPS), int(t1 * FPS)
            voc = float(vocal_mask[a:b].mean()) if b > a else 0
        if i == 0 and s["e"] < med - .15:
            lab = "intro"
        elif i == len(segs) - 1 and s["e"] < med - .1:
            lab = "outro"
        elif has_ch or (s["e"] >= top and s["rep"] >= 1):
            lab = "estribillo"
        elif s["e"] < med - .25:
            lab = "break"
        else:
            lab = "estrofa"
        out.append({"t0": round(t0, 3), "t1": round(t1, 3), "bars": s["b"] - s["a"], "label": lab,
                    "energy": round(float(s["e"] - med), 3), "voice": None if voc is None else round(voc, 3)})
    # repeticion de frases: cuanto se parece cada frase de 4 (y de 8) compases a la siguiente (bucles naturales)
    rep4, rep8 = np.zeros(n), np.zeros(n)
    for i in range(n - 8):
        rep4[i] = float(np.mean([S[i + j, i + 4 + j] for j in range(4)]))
    for i in range(n - 16):
        rep8[i] = float(np.mean([S[i + j, i + 8 + j] for j in range(8)]))
    return out, en - med, {"start": float(starts[0]), "rep4": np.round(rep4, 3).tolist(), "rep8": np.round(rep8, 3).tolist()}


# ------------------------------------------------------------------ voz separada (Spleeter ONNX, 2 pistas)
class Spleeter:
    """deezer/spleeter 2stems en ONNX (el del karaoke). Devuelve la instrumental; voz = mezcla - instrumental"""
    N, H, F, T = 4096, 1024, 1024, 512

    def __init__(self, d):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.intra_op_num_threads = os.cpu_count() or 4
        self.v = ort.InferenceSession(os.path.join(d, "vocals.onnx"), so, providers=["CPUExecutionProvider"])
        self.a = ort.InferenceSession(os.path.join(d, "accompaniment.onnx"), so, providers=["CPUExecutionProvider"])
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(self.N) / self.N)).astype(np.float32)

    def _block(self, seg, nfr):
        N, H, F, T = self.N, self.H, self.F, self.T
        idx = np.arange(N)[None, :] + H * np.arange(nfr)[:, None]
        S = np.fft.rfft(seg[:, idx] * self.win, axis=-1).astype(np.complex64)
        x = np.zeros((2, 1, T, F), np.float32)
        x[:, 0, :nfr] = np.abs(S[:, :, :F])
        v = self.v.run(None, {"x": x})[0][:, 0, :nfr]
        a = self.a.run(None, {"x": x})[0][:, 0, :nfr]
        m = (a * a + 5e-11) / (v * v + a * a + 1e-10)
        Mk = np.empty(S.shape, np.float32)
        Mk[:, :, :F] = m
        Mk[:, :, F:] = m[:, :, -64:].mean(axis=2, keepdims=True)
        fr = np.fft.irfft(S * Mk, n=N, axis=-1).astype(np.float32) * self.win
        out = np.zeros((2, (nfr - 1) * H + N), np.float32)
        for q in range(N // H):
            out[:, q * H: q * H + nfr * H] += fr[:, :, q * H:(q + 1) * H].reshape(2, nfr * H)
        return out

    def instrumental(self, mix):
        N, H, T = self.N, self.H, self.T
        n = mix.shape[1]
        N2 = N // 2
        xp = np.pad(mix, ((0, 0), (N2, N2 + T * H)), mode="constant")
        xp[:, :N2] = mix[:, N2:0:-1]
        nfr = 1 + n // H
        win2 = self.win ** 2
        env = np.zeros(xp.shape[1], np.float32)
        for q in range(N // H):
            env[q * H: q * H + nfr * H] += np.tile(win2[q * H:(q + 1) * H], nfr)
        out = np.zeros_like(xp)
        f0 = 0
        while f0 < nfr:
            f1 = min(nfr, f0 + T)
            ola = self._block(xp[:, f0 * H:(f1 - 1) * H + N], f1 - f0)
            out[:, f0 * H: f0 * H + ola.shape[1]] += ola
            f0 = f1
        return (out[:, N2:N2 + n] / np.maximum(env[N2:N2 + n], 1e-6)).astype(np.float32)


def vocal_activity(mix, inst):
    """Por cada 10 ms: 1 si canta (la voz separada supera un umbral relativo a la mezcla y absoluto)"""
    voc = (mix - inst).mean(0)
    m = mix.mean(0)
    n = len(m) // HOP
    ev = np.sqrt((voc[: n * HOP].reshape(n, HOP) ** 2).mean(1) + 1e-12)
    em = np.sqrt((m[: n * HOP].reshape(n, HOP) ** 2).mean(1) + 1e-12)
    ev = signal.medfilt(ev, 25)
    em = signal.medfilt(em, 25)
    act = ((ev > .2 * em) & (20 * np.log10(ev) > -42)).astype(np.float32)
    # huecos y destellos cortos fuera (< 0,35 s)
    act = signal.medfilt(act, 35)
    return act, ev


def spans(mask, min_len=.35):
    """tramos [t0, t1] donde mask = 1"""
    d = np.diff(np.r_[0, (mask > .5).astype(int), 0])
    a, b = np.where(d == 1)[0], np.where(d == -1)[0]
    return [[round(x / FPS, 2), round(y / FPS, 2)] for x, y in zip(a, b) if (y - x) / FPS >= min_len]


def peaks(x, n=1200):
    m = np.abs(x).max(0)
    k = max(1, len(m) // n)
    return np.round(m[: k * (len(m) // k)].reshape(-1, k).max(1), 3).tolist()
