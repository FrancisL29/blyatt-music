"""Planificador y motor de transiciones del laboratorio (numpy + scipy: portable al servidor).

Reloj maestro del clip = tiempo del archivo de la cancion que sale (A), que suena a su velocidad. La entrante (B)
se estira SIN cambiar el tono (WSOLA) para casar el tempo y despues vuelve a su tempo poco a poco.

Plantillas (pocas y bien hechas, como Apple/Spotify):
  eq      mezcla larga por frases con cambio de graves (house/techno, intros/outros instrumentales)
  voz     voz primero: la voz (separada) de B canta sobre la instrumental de A; luego entra el resto de B
  inst    instrumental primero: la instrumental de B sustituye a la de A bajo la voz de A; al acabar A su ultima
          linea, canta B
  corte   corte en el "uno" tras una frase cantada (en un silencio de la voz), con eco del ultimo golpe
  fundido fundido por bandas (agudos, medios y graves a destiempo) cuando nada casa
"""
import io
import wave

import numpy as np
from scipy import signal

from analysis import mid_flux

SR = 44100
LABELS = {"eq": "Mezcla EQ por frases", "voz": "Voz primero", "inst": "Instrumental primero",
          "corte": "Corte en frase con eco", "fundido": "Fundido por bandas"}


# ------------------------------------------------------------------ utilidades de tiempo
def _down_grid(I):
    return I["bar0"], 4 * I["per"]


def next_down(I, t, eps=0.0):
    b0, bar = _down_grid(I)
    return b0 + np.ceil((t - b0 - eps) / bar) * bar


def prev_down(I, t):
    b0, bar = _down_grid(I)
    return b0 + np.floor((t - b0) / bar) * bar


def vspans(I):
    """donde canta: de la voz separada si se calculo; si no, de la letra"""
    if I.get("vocals"):
        return I["vocals"]
    return [[l["t"], l["e"]] for l in I.get("lyrics") or []]


def sings(sp, t0, t1):
    return sum(max(0, min(b, t1) - max(a, t0)) for a, b in sp)


def tempo_rel(A, B):
    best = None
    for m in (.5, 1, 2):
        s = A["bpm"] / (B["bpm"] * m)
        if best is None or abs(s - 1) < abs(best[0] - 1):
            best = (s, m)
    return best


def camelot_dist(a, b):
    d = min((a["cam"] - b["cam"]) % 12, (b["cam"] - a["cam"]) % 12)
    return d if a["letter"] == b["letter"] else (1 if d == 0 else d + 1)


# ------------------------------------------------------------------ alineacion fina de los bombos
def beat_profile(mono, I, t_from, nbeats=32):
    """Perfil medio de los ataques del bombo dentro de un golpe (1 ms por punto; el centro = golpe de la rejilla) en
    nbeats golpes desde el golpe de rejilla >= t_from. Ataque = flujo espectral en 60-3000 Hz (el golpe del bombo,
    sin los charles a contratiempo ni el rumble). None si no hay audio suficiente"""
    per = I["per"]
    b0 = I.get("barStart", I["bar0"])
    g0 = b0 + np.ceil((t_from - b0) / per - 1e-6) * per
    a, b = int((g0 - per) * SR), int((g0 + (nbeats + 1) * per) * SR)
    if a < 0 or b > len(mono) or nbeats < 4:
        return None
    fl, tt = mid_flux(mono[a:b])
    o = np.interp(np.arange(0, (b - a) / SR, .001), tt, fl)
    P = int(round(per * 1000))
    prof = np.zeros(P)
    for k in range(nbeats):
        c = int(round((per + k * per) * 1000)) - P // 2
        if c < 0 or c + P > len(o):
            return None
        prof += o[c:c + P]
    return prof / nbeats


def kick_lag(monoA, A, tA, monoB, B, tB, s=1.0, nbeats=32):
    """Cuanto hay que retrasar B (segundos del clip; negativo = adelantar) para que sus bombos caigan encima de los
    de A. tA, tB: donde empiezan los compases con bombo que se comparan (tiempo de cada archivo). Con tempos casados
    se correlan los dos perfiles; si no, se igualan sus picos. Devuelve (retraso, nitidez del golpe)"""
    pa = beat_profile(monoA, A, tA, nbeats)
    pb = beat_profile(monoB, B, tB, nbeats)
    if pa is None or pb is None:
        return 0.0, 0.0
    sharp = min(pa.max() / (pa.mean() or 1), pb.max() / (pb.mean() or 1))
    if sharp < 1.3:          # sin golpe claro en alguna de las dos: no se toca
        return 0.0, float(sharp)
    P = len(pa)
    pb2 = np.interp(np.linspace(0, len(pb) - 1, P), np.arange(len(pb)), pb)   # al golpe de A
    sh = lambda p: np.maximum(0, p - np.median(p)) ** 2
    if abs(s - 1) < .12:
        xc = np.fft.irfft(np.fft.rfft(sh(pa)) * np.conj(np.fft.rfft(sh(pb2))), P)   # xc[l] = sum pa[i] pb2[i-l]
        l = int(xc.argmax())
    else:
        l = int(pa.argmax()) - int(pb2.argmax())
    l = (l + P // 2) % P - P // 2          # -medio golpe .. +medio golpe
    return l / 1000.0, float(sharp)


def _kf(I, t, nbars=8):
    """fraccion de compases con bombo en los nbars desde t"""
    k = I.get("kick") or []
    bar = 4 * I["per"]
    i = int(np.floor((t - I.get("barStart", I["bar0"])) / bar + 1e-6))
    seg = k[max(0, i): max(0, i + nbars)]
    return float(np.mean([x >= .45 for x in seg])) if seg else 0.0


# ------------------------------------------------------------------ motor
def _env(points, t):
    pts = sorted(points)
    return np.interp(t, [p[0] for p in pts], [p[1] for p in pts]).astype(np.float32)


def _fade(t0, t1, up, n=24):
    xs = np.linspace(0, 1, n)
    v = np.sin(xs * np.pi / 2) if up else np.cos(xs * np.pi / 2)
    return [(t0 + (t1 - t0) * x, float(y)) for x, y in zip(xs, v)]


def _bands(x):
    lo = signal.sosfiltfilt(signal.butter(2, 200, "low", fs=SR, output="sos"), x, axis=-1)
    hi = signal.sosfiltfilt(signal.butter(2, 2500, "high", fs=SR, output="sos"), x, axis=-1)
    return lo, x - lo - hi, hi


def wsola(x, start, speed, n_out, N=2048, Hs=512, tol=512, dec=8):
    """Estira x (2, n) desde la muestra `start` con velocidad speed(t_out_seg) (contenido por segundo de salida)
    sin cambiar el tono. Devuelve el camino (inicio de cada trozo) para aplicarlo igual a las pistas separadas"""
    xm = x.mean(0)
    pad = N + 2 * tol + 4 * SR
    xp = np.concatenate([np.zeros(pad, np.float32), xm, np.zeros(pad, np.float32)])
    xd = xp[: len(xp) // dec * dec].reshape(-1, dec).mean(1)
    ND, TD = N // dec, tol // dec
    nfr = n_out // Hs + 2
    path = np.zeros(nfr, np.int64)
    ip = float(start)
    prev = None
    for k in range(nfr):
        ideal = int(round(ip))
        if prev is None:
            ch = ideal
        else:
            nat = prev + Hs
            a = (nat + pad) // dec
            ref = xd[a: a + ND]
            c0 = (ideal - tol + pad) // dec
            seg = xd[c0: c0 + ND + 2 * TD]
            if len(ref) == ND and len(seg) == ND + 2 * TD and np.any(ref):
                W = np.lib.stride_tricks.sliding_window_view(seg, ND)
                sc = (W @ ref) / (np.sqrt((W * W).sum(1)) + 1e-9)
                ch = c0 * dec - pad + int(sc.argmax()) * dec
            else:
                ch = ideal
        path[k] = ch
        prev = ch
        ip += Hs * speed((k * Hs + N / 2) / SR)
    return path


def wsola_apply(x, path, n_out, N=2048, Hs=512):
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N) / N)).astype(np.float32)
    out = np.zeros((x.shape[0], n_out + N), np.float32)
    n = x.shape[1]
    for k, p in enumerate(path):
        o = k * Hs
        if o >= n_out:
            break
        a, b = max(0, p), min(n, p + N)
        if b <= a:
            continue
        out[:, o + (a - p): o + (b - p)] += x[:, a:b] * win[a - p: b - p]
    return out[:, :n_out] / 2.0      # Hann con salto N/4: suma 2


def render(A, B, aud, p, stems=True, pre=8.0, post=6.0):
    """aud = {"A": {"mix": (2,n), "inst": (2,n) o None}, "B": {...}}. Devuelve (wav bytes, marcas)"""
    tpl = p["tpl"]
    beatA, barA = A["per"], 4 * A["per"]
    barB = 4 * B["per"]
    s0 = float(p.get("s", 1.0))
    Ts = float(p["ts"])
    Te = float(p["te"])
    ramp = 8 * barA if abs(s0 - 1) > 1e-4 else 0.0
    t0 = max(0.0, min(Ts, p.get("tg", Ts)) - pre)
    t1 = Te + ramp + post
    n = int((t1 - t0) * SR)
    T = t0 + np.arange(n) / SR                         # reloj del clip en tiempo de A
    useS = stems and aud["A"].get("inst") is not None and aud["B"].get("inst") is not None and tpl in ("voz", "inst")
    # ---- A: a su velocidad (y en bucle si se le acaba la musica en "voz primero")
    idxA = np.rint(T * SR).astype(np.int64)
    if p.get("loop"):
        L0, L1 = p["loop"]
        lp = T >= L1
        idxA[lp] = np.rint((L0 + np.mod(T[lp] - L1, L1 - L0)) * SR).astype(np.int64)
    mixA = aud["A"]["mix"]
    okA = (idxA >= 0) & (idxA < mixA.shape[1])
    take = lambda arr, idx, ok: np.where(ok, arr[:, np.clip(idx, 0, arr.shape[1] - 1)], 0).astype(np.float32)
    aMix = take(mixA, idxA, okA)
    aInst = take(aud["A"]["inst"], idxA, okA) if useS else aMix
    aVoc = aMix - aInst if useS else np.zeros_like(aMix)
    # ---- B: estirada (WSOLA) desde Ts y de vuelta a su tempo
    def speed(tt):   # tt: segundos de salida desde Ts
        t = Ts + tt
        if t <= Te or ramp == 0:
            return s0
        return s0 + (1 - s0) * min(1, (t - Te) / ramp)
    nB = int((t1 - Ts) * SR)
    mixB = aud["B"]["mix"]
    # ---- bombos encima de bombos: se mide donde pegan de verdad los de A y los de B (no basta con las rejillas: en
    #      hard techno cada tema tiene su bombo, su click y su rumble) y se corre B lo que haga falta
    b_in = float(p["b_in"])
    lag, ksharp = 0.0, 0.0
    tAs, tBs = [], []
    if tpl in ("eq", "bucle"):
        tA0 = p["loop"][0] if tpl == "bucle" else p["tsw"] - 8 * barA
        tAs, tBs = [tA0, tA0 - 8 * barA], [b_in, b_in + (p["tsw"] - Ts) * s0]
    elif tpl == "corte":
        tAs, tBs = [Ts - 8 * barA, Ts - 16 * barA], [b_in, b_in + 8 * barB]
    elif tpl == "silencio":
        tAs, tBs = [p["tg"] - 8 * barA, p["tg"] - 16 * barA], [b_in + (Te - Ts), b_in + (Te - Ts) + 8 * barB]
    meas = []
    mA, mB = mixA.mean(0), mixB.mean(0)
    for ta in tAs:
        for tb in tBs:
            if _kf(A, ta) >= .5 and _kf(B, tb) >= .5:
                l, sh = kick_lag(mA, A, ta, mB, B, tb, s0)
                if sh >= 1.3:
                    meas.append((l, sh))
    if meas:
        lag = float(np.median([m[0] for m in meas]))
        ksharp = max(m[1] for m in meas)
        spread = max(abs(m[0] - lag) for m in meas)
        # con la rejilla ya en el bombo el ajuste suele ser pequeno; uno grande solo si el golpe es nitido y todas las
        # medidas coinciden
        if abs(lag) > .2 * beatA and (ksharp < 2.0 or spread > .03):
            lag = 0.0
        lag = float(np.clip(lag, -.45 * beatA, .45 * beatA))
        b_in -= lag * s0
    bMix = np.zeros((2, n), np.float32)
    oB = int((Ts - t0) * SR)
    direct = ramp == 0 and abs(s0 - 1) < 1e-6      # a su tempo: se copia tal cual (sin WSOLA)

    def stretchB(x):
        if direct:
            idx = int(round(b_in * SR)) + np.arange(nB)
            ok = (idx >= 0) & (idx < x.shape[1])
            return np.where(ok, x[:, np.clip(idx, 0, x.shape[1] - 1)], 0).astype(np.float32)
        return wsola_apply(x, path, nB)
    path = None if direct else wsola(mixB, int(b_in * SR), speed, nB)
    bMix[:, oB:oB + nB] = stretchB(mixB)[:, : n - oB]
    if useS:
        bInst = np.zeros_like(bMix)
        bInst[:, oB:oB + nB] = stretchB(aud["B"]["inst"])[:, : n - oB]
        bVoc = bMix - bInst
    else:
        bInst, bVoc = bMix, np.zeros_like(bMix)
    gB = float(np.clip(10 ** ((A["lufs"] - B["lufs"]) / 20), .5, 2.0))   # mismo volumen percibido
    # ---- envolventes (1 = sin cambio; .05 = -26 dB: "matar" la banda)
    K = .05
    E = {k: [(t0, 1.0)] for k in ("aF", "aL", "aM", "aH", "aV", "bL", "bM", "bH", "bV")}
    E["bF"] = [(t0, 0.0), (Ts, 0.0)]
    send = [(t0, 0.0)]
    marks = []
    mk = lambda t, txt: marks.append({"t": round(t - t0, 3), "txt": txt})
    if tpl in ("eq", "bucle"):
        Tsw = p["tsw"]
        E["bF"] += _fade(Ts, Ts + min(4 * barA, Tsw - Ts), True)
        E["bL"] = [(t0, K), (Tsw, K), (Tsw + beatA, 1)]
        E["bH"] = [(t0, .45), (Ts, .45), (Tsw + 2 * beatA, 1)]
        E["aL"] = [(t0, 1), (Tsw, 1), (Tsw + beatA, K)]
        E["aH"] = [(t0, 1), (Tsw, 1), (Tsw + 2 * beatA, .6)]
        if sings(vspans(A), Ts, Te) > 1 and sings(vspans(B), p["b_in"], p["b_in"] + (Te - Ts) * s0) > 1:
            E["bM"] = [(t0, .3), (Tsw, .3), (Tsw + beatA, 1)]
            E["aM"] = [(t0, 1), (Tsw, 1), (Tsw + beatA, .3)]
        E["aF"] = [(t0, 1)] + _fade(max(Tsw, Te - 2 * barA), Te, False)
        if tpl == "bucle":
            mk(p["loop"][0], "frase que se repite"); mk(p["loop"][1], "A en bucle")
        mk(Ts, "entra B (sin graves)"); mk(Tsw, "cambio de graves"); mk(Te, "sale A")
    elif tpl == "voz":
        Bs, Tsw, Tf = p["bs"], p["tsw"], p["te"]
        E["bF"] += [(Ts, 0), (Ts + beatA, 1)]
        for b in ("bM", "bH"):
            E[b] = [(t0, 0), (Tsw - barA, 0), (Tsw, 1)]
        E["bL"] = [(t0, 0), (Tsw - .25 * beatA, 0), (Tsw + .5 * beatA, 1)]
        E["bV"] = [(t0, 1.15)]
        E["aV"] = [(t0, 1), (Bs - .5 * beatA, 1), (Bs, 0)]
        E["aM"] = [(t0, 1), (Bs - beatA, 1), (Bs, .63)]
        E["aL"] = [(t0, 1), (Tsw, 1), (Tsw + beatA, K)]
        E["aH"] = [(t0, 1), (Tsw, 1), (Tf, .3)]
        E["aF"] = [(t0, 1)] + _fade(Tsw, Tf, False)
        if not useS:   # sin voz separada: los medios de B como "voz"
            E["bM"] = [(t0, 1)]
            E["bH"] = [(t0, .25), (Tsw - barA, .25), (Tsw, 1)]
        mk(Ts, "B: un compás antes"); mk(Bs, "canta B (sola)"); mk(Tsw, "entra todo B"); mk(Tf, "fin de la base de A")
        if p.get("loop") and p["loop"][1] < Tf:
            mk(p["loop"][1], "base de A en bucle")
    elif tpl == "inst":
        Tm, Tv = p["tm"], p["tv"]
        E["bF"] += _fade(Ts, Ts + min(4 * barA, Tm - Ts), True)
        E["bL"] = [(t0, K), (Tm, K), (Tm + beatA, 1)]
        E["bH"] = [(t0, .45), (Ts, .45), (Tm + 2 * beatA, 1)]
        for b in ("aM", "aH"):
            E[b] = [(t0, 1), (Tm, 1), (Tm + barA, 0)]
        E["aL"] = [(t0, 1), (Tm, 1), (Tm + beatA, 0)]
        E["aV"] = [(t0, 1.1), (Tv - .25 * beatA, 1.1), (Tv + .75 * beatA, 0)]
        send = [(t0, 0), (Tv - beatA, 0), (Tv - .5 * beatA, .5), (Tv + beatA, 0)]
        if not useS:
            E["aM"] = [(t0, 1)]
            E["aH"] = [(t0, 1), (Tm, 1), (Tm + barA, .3)]
        mk(Ts, "entra la instrumental de B"); mk(Tm, "cambio de instrumentales"); mk(Tv, "canta B")
    elif tpl == "silencio":
        Tg, Tk = p["tg"], Te
        aEnd = max(Ts + beatA, Tg) if Ts < Tg else Ts + .5 * beatA
        E["aL"] = [(t0, 1), (Ts, 1), (Ts + .25 * beatA, K)]
        E["aF"] = [(t0, 1)] + _fade(Ts, aEnd, False)
        E["bF"] = [(t0, 0), (Ts, 0), (Ts + .005, 1)]
        mk(Tg, "parón de A"); mk(Ts, "arranca B"); mk(Tk, "bombos de B")
    elif tpl == "corte":
        Tc = Ts
        E["aL"] = [(t0, 1), (Tc - 2 * barA, 1), (Tc, .35)]
        E["aF"] = [(t0, 1), (Tc - .005, 1), (Tc + .005, 0)]
        send = [(t0, 0), (Tc - beatA, 0), (Tc - beatA + .01, .75), (Tc - .005, .75), (Tc + .005, 0)]
        E["bF"] = [(t0, 0), (Tc, 0), (Tc + .006, 1)]
        mk(Tc - 2 * barA, "A pierde graves"); mk(Tc, "corte + eco, entra B")
    else:   # fundido
        Wd = Te - Ts
        E["aH"] = [(t0, 1), (Ts, 1), (Te, 0)]
        E["aM"] = [(t0, 1), (Ts, 1), (Ts + .8 * Wd, 0)]
        E["aL"] = [(t0, 1), (Ts + .45 * Wd, 1), (Ts + .55 * Wd, 0)]
        E["bF"] = [(t0, 0), (Ts, 0), (Ts + .02, 1)]
        E["bH"] = [(t0, 0), (Ts, 0), (Ts + .4 * Wd, 1)]
        E["bM"] = [(t0, 0), (Ts + .2 * Wd, 0), (Ts + .7 * Wd, 1)]
        E["bL"] = [(t0, 0), (Ts + .45 * Wd, 0), (Ts + .55 * Wd, 1)]
        mk(Ts, "empieza el fundido"); mk(Ts + .5 * Wd, "cambio de graves"); mk(Te, "sale A")
    if ramp:
        mk(Te + ramp, "B ya a su tempo")
    gBe = _env([(t0, gB), (Te, gB), (Te + max(ramp, 4.0), 1.0)], T)
    # ---- mezcla
    aL, aM, aH = _bands(aInst)
    bL, bM, bH = _bands(bInst)
    ev = lambda k: _env(E[k], T)
    A_out = ev("aF") * (ev("aL") * aL + ev("aM") * aM + ev("aH") * aH + ev("aV") * aVoc)
    B_out = ev("bF") * gBe * (ev("bL") * bL + ev("bM") * bM + ev("bH") * bH + ev("bV") * bVoc)
    out = A_out + B_out
    se = _env(send, T)
    if se.max() > 0:   # eco al ritmo (3/4 de golpe), sin graves
        src = aMix if tpl == "corte" else A_out      # corte: el ultimo golpe de A; inst: su ultima palabra
        sig = signal.sosfilt(signal.butter(2, 300, "high", fs=SR, output="sos"), src * se, axis=-1)
        D = int(.75 * beatA * SR)
        for k in range(1, 7):
            g = .45 ** k
            out[:, k * D:] += g * sig[:, : n - k * D]
    pk = np.abs(out).max()
    if pk > .99:
        out *= .99 / pk
    # para dibujar el editor: formas de onda de las dos y sus curvas (volumen, graves, voz)
    NP = 1600

    def peaks(x):
        mm = np.abs(x).max(0)
        k = max(1, len(mm) // NP)
        return np.round(mm[: k * (len(mm) // k)].reshape(-1, k).max(1), 3).tolist()

    def curve(e):
        k = max(1, len(e) // NP)
        return np.round(np.clip(e[: k * (len(e) // k)].reshape(-1, k).mean(1), 0, 1.2), 3).tolist()
    aFv, bFv = ev("aF"), ev("bF")
    lo150 = signal.butter(4, 150, "low", fs=SR, output="sos")

    def lowenv(x):   # graves (<150 Hz) en RMS por punto: se ven los golpes del bombo
        m = signal.sosfiltfilt(lo150, x.mean(0)).astype(np.float32)
        k = max(1, len(m) // NP)
        r = np.sqrt((m[: k * (len(m) // k)].reshape(-1, k) ** 2).mean(1)) * 1.6
        return np.round(np.clip(r, 0, 1), 3).tolist()

    # golpes de cada rejilla en el reloj del clip (los de B, tras estirarla y correrla)
    def grid(I, t_a, t_b):
        per, g0 = I["per"], I.get("barStart", I["bar0"])
        k0 = int(np.ceil((t_a - g0) / per))
        out = []
        while g0 + k0 * per < t_b:
            out.append((g0 + k0 * per, k0 % 4 == 0))
            k0 += 1
        return out
    beatsA = [[round((t - t0) / (t1 - t0), 5), int(d)] for t, d in grid(A, t0, t1)]
    uu = np.arange(0, nB / SR, .005)
    cont = b_in + np.concatenate([[0], np.cumsum([speed(u) * .005 for u in uu[:-1]])])   # contenido de B en cada instante
    beatsB = []
    for t, d in grid(B, cont[0], cont[-1]):
        u = float(np.interp(t, cont, uu))
        beatsB.append([round((Ts + u - t0) / (t1 - t0), 5), int(d)])
    viz = {"A": peaks(aMix), "B": peaks(bMix), "Alow": lowenv(aMix), "Blow": lowenv(bMix), "beatsA": beatsA, "beatsB": beatsB,
           "aVol": curve(aFv), "bVol": curve(bFv), "aBass": curve(aFv * ev("aL")), "bBass": curve(bFv * ev("bL")),
           "aVoc": curve(aFv * ev("aV")) if useS else None, "bVoc": curve(bFv * ev("bV")) if useS else None,
           "bStart": (Ts - t0) / (t1 - t0)}
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((out.T * 32767).astype("<i2").tobytes())
    info = {"t0": float(t0), "dur": n / SR, "stems": bool(useS), "speed": s0, "gainB": float(round(20 * np.log10(gB), 1)), "viz": viz,
            "kickLagMs": round(lag * 1000), "kickSharp": round(ksharp, 2)}
    return buf.getvalue(), marks, info
