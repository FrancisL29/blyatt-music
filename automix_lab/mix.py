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


# ------------------------------------------------------------------ planificador
def plan(A, B, stems=True):
    s, m = tempo_rel(A, B)
    match = abs(s - 1) <= .10 and A["conf"] > 1.1 and B["conf"] > 1.1
    kd = camelot_dist(A["key"], B["key"]) if A["key"]["conf"] > .02 and B["key"]["conf"] > .02 else 1
    de = B["energy"] - A["energy"]
    barA, beatA = 4 * A["per"], A["per"]
    barB = 4 * B["per"]
    VA, VB = vspans(A), vspans(B)
    Aend = prev_down(A, A["musicEnd"])
    b0 = next_down(B, B["musicStart"] - .05)
    lastVA = max([b for a, b in VA if a < Aend - .5], default=None)
    firstVB = min([a for a, b in VB if a > 1.0], default=None)
    common = []
    if not match:
        common.append("tempos que no casan (%.0f→%.0f BPM, %+.0f%%)" % (A["bpm"], B["bpm"], (1 / s - 1) * 100))
    else:
        common.append("tempo: B se estira %+.1f%% sin cambiar el tono" % ((s - 1) * 100) + (" (medio/doble tempo)" if m != 1 else ""))
    common.append("tonalidad %s→%s (distancia Camelot %d)" % (A["key"]["camelot"], B["key"]["camelot"], kd))
    common.append("energía %d→%d" % (A["energy"], B["energy"]))
    out = []

    def add(tpl, score, why, **p):
        p.update(tpl=tpl, label=LABELS[tpl], score=round(score, 2), why=why + common, s=s if p.get("stretch", True) else 1.0)
        p.pop("stretch", None)
        out.append(p)
    epen = -.4 * max(0, abs(de) - 1)
    # 1) mezcla EQ
    if match:
        best = None
        for L in (16, 8, 4):
            Ts = Aend - L * barA
            if Ts < A["dur"] * .4:
                continue
            Te = Aend
            # cuanto cantan las dos a la vez (B en su intro: contenido = b0 + (t - Ts) * s)
            both = 0.0
            for t in np.arange(Ts, Te, .25):
                bt = b0 + (t - Ts) * s
                if sings(VA, t, t + .25) > 0 and sings(VB, bt, bt + .25) > 0:
                    both += .25
            clash = both / (Te - Ts)
            sc = 2.2 + .08 * L - 3 * clash - (0 if kd <= 1 else .5 * (kd - 1) * L / 8) + epen
            if A["family"] == "club" and B["family"] == "club":
                sc += .8
            if best is None or sc > best[0]:
                best = (sc, L, Ts, Te, clash)
        if best:
            sc, L, Ts, Te, clash = best
            why = ["%d compases encima, cambio de graves a mitad" % L]
            if clash > .05:
                why.append("se pisan las voces un %d%% del tiempo" % (clash * 100))
            add("eq", sc, why, ts=Ts, b_in=b0, bars=L, tsw=Ts + (L // 2) * barA, te=Te)
    # 2) voz primero
    if match and stems and kd <= 2 and lastVA and firstVB:
        Bs = next_down(A, lastVA + .1)
        bV = B["bar0"] + np.floor((firstVB + .25 * B["per"] - B["bar0"]) / barB) * barB
        bin_ = bV - barB if bV - barB >= 0 else bV
        Ts = Bs - (bV - bin_) / s
        V = 8 if B["family"] != "club" else 8
        Tsw = Bs + V * barA
        cover = sings(VB, bV, bV + V * barB) / (V * barB)
        if Ts > A["dur"] * .35 and cover > .3:
            sc = 3.0 - .6 * max(0, kd - 1) + epen + (.5 if Aend - Bs >= barA else 0) + .5 * cover
            Ae = Aend
            LL = 2 if Ae - Bs >= 2 * barA else 1
            add("voz", sc, ["la voz de B entra al acabar la última línea de A, %d compases sola" % V,
                            "la base de A sigue (en bucle si se le acaba) y se desvanece 8 compases"],
                ts=Ts, b_in=bin_, bs=Bs, tsw=Tsw, te=Tsw + 8 * barA, loop=[Ae - LL * barA, Ae])
    # 3) instrumental primero
    if match and stems and kd <= 2 and lastVA and firstVB and Aend - lastVA < 4 * barA:
        Tv = next_down(A, lastVA - .1)
        bV = B["bar0"] + np.floor((firstVB + .25 * B["per"] - B["bar0"]) / barB) * barB
        Lb = min(8, int((bV - b0) / barB + 1e-6))
        if Lb >= 4:
            bin_ = bV - Lb * barB
            Ts = Tv - (bV - bin_) / s
            Tm = Ts + (Lb // 2) * barA
            sc = 2.8 - .6 * max(0, kd - 1) + epen
            add("inst", sc, ["la instrumental de B (%d compases) sustituye a la de A bajo su voz" % Lb,
                             "B canta justo cuando A acaba su última línea"],
                ts=Ts, b_in=bin_, tm=Tm, tv=Tv, te=Tv + beatA)
    # 4) corte en frase (en un silencio de la voz de A)
    cands = []
    for t in np.arange(next_down(A, A["dur"] * .6), Aend + 1e-3, barA):
        if sings(VA, t - .5 * beatA, t + .1) > 0:
            continue
        ended = any(t - 2 * barA <= b <= t for a, b in VA)
        cands.append(((1 if ended else 0) + .3 * (t / Aend), t))
    if cands:
        _, Tc = max(cands)
        bin_ = b0
        if de < -1:
            pass
        elif A["energy"] - B["energy"] > 1 or B.get("sections"):
            ch = [x["t0"] for x in B.get("sections") or [] if x["label"] == "estribillo" and x["t0"] > 4]
            if ch and A["energy"] >= B["energy"]:
                bin_ = next_down(B, ch[0] - .05)
        sc = 1.2 + (1.2 if not match else 0) + (.4 if sings(VA, Tc - 2 * barA, Tc) > 0 else 0)
        add("corte", sc, ["A corta en el \"uno\" justo tras una frase cantada, con eco",
                          "B entra en %s" % ("su estribillo" if bin_ != b0 else "su primer compás")],
            ts=Tc, b_in=bin_, te=Tc, stretch=match)
    # 5) fundido por bandas
    W = 4 * barA if match else 6.0
    add("fundido", .6 + (.6 if not match else 0), ["agudos, medios y graves cambian a destiempo (%.1f s)" % W],
        ts=Aend - W, b_in=b0, te=Aend, stretch=match)
    out.sort(key=lambda x: -x["score"])
    for i, c in enumerate(out):
        c["id"] = i
    return out


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
    t0 = max(0.0, Ts - pre)
    t1 = Te + ramp + post
    n = int((t1 - t0) * SR)
    T = t0 + np.arange(n) / SR                         # reloj del clip en tiempo de A
    useS = stems and aud["A"].get("inst") is not None and aud["B"].get("inst") is not None and tpl in ("voz", "inst")
    # ---- A: a su velocidad (y en bucle si se le acaba la musica en "voz primero")
    idxA = np.rint(T * SR).astype(np.int64)
    if p.get("loop") and tpl == "voz":
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
    path = wsola(mixB, int(p["b_in"] * SR), speed, nB)
    bMix = np.zeros((2, n), np.float32)
    oB = int((Ts - t0) * SR)
    bMix[:, oB:oB + nB] = wsola_apply(mixB, path, nB)[:, : n - oB]
    if useS:
        bInst = np.zeros_like(bMix)
        bInst[:, oB:oB + nB] = wsola_apply(aud["B"]["inst"], path, nB)[:, : n - oB]
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
    if tpl == "eq":
        Tsw = p["tsw"]
        E["bF"] += _fade(Ts, Ts + min(4 * barA, Tsw - Ts), True)
        E["bL"] = [(t0, K), (Tsw, K), (Tsw + beatA, 1)]
        E["bH"] = [(t0, .45), (Ts, .45), (Tsw + 2 * beatA, 1)]
        E["aL"] = [(t0, 1), (Tsw, 1), (Tsw + beatA, K)]
        E["aH"] = [(t0, 1), (Tsw, 1), (Tsw + 2 * beatA, .6)]
        if sings(vspans(A), Ts, Te) > 1 and sings(vspans(B), p["b_in"], p["b_in"] + (Te - Ts) * s0) > 1:
            E["bM"] = [(t0, .3), (Tsw, .3), (Tsw + beatA, 1)]
            E["aM"] = [(t0, 1), (Tsw, 1), (Tsw + beatA, .3)]
        E["aF"] = [(t0, 1)] + _fade(Te - 2 * barA, Te, False)
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
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((out.T * 32767).astype("<i2").tobytes())
    info = {"t0": t0, "dur": n / SR, "stems": useS, "speed": s0, "gainB": round(20 * np.log10(gB), 1)}
    return buf.getvalue(), marks, info
