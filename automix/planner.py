"""Planificador de transiciones (laboratorio). Una sola opcion: "Auto" = la mejor puntuada; las demas quedan
como alternativas para comparar.

Reglas nuevas:
  - CONTINUIDAD DEL BOMBO (house, techno, hard techno, todo lo que empuja): si la saliente va con bombo, nunca se
    cae en la intro suave de la entrante. La entrante entra antes de sus bombos, por debajo, mientras la saliente
    sigue con los suyos, y el cambio de graves cae justo en el primer bombo de la entrante. Si la saliente acaba con
    un outro sin bombo, la mezcla termina antes de el.
  - BUCLE DE FRASE: si la saliente tiene una frase que se repite (el mismo trozo de 4 u 8 compases varias veces,
    con letra o instrumental), se queda en bucle en esa frase y la entrante entra encima.
  - SECCIONES DE GENIUS cruzadas con la letra sincronizada para saber donde empieza cada estribillo.
  - INTRO SIN BOMBO PERO FUERTE (rumble, sintes): encima de los bombos de A embarra, asi que B entra directamente en
    sus bombos y se mezcla bombo sobre bombo (hard techno al mismo tempo: lo mas seguro).
  - SILENCIO Y ENTRADA: si A hace un paron sin bombo (o acaba) y B arranca sus bombos tras una voz sola ("this is
    chaos"), B empieza en ese silencio y sus bombos caen en el "uno" donde volverian los de A. Gana sobre todo con
    tempos distintos (B entra a su tempo, sin estirar).
  - Los bombos se alinean al renderizar midiendo donde pegan de verdad en las dos (mix.kick_lag).
  - FUERZA DEL BOMBO por compas (kpow): con mucho empuje (hard techno: energia 9+, 130+ BPM) se entra en los bombos
    PRINCIPALES (no en un ritmo suave previo) y A deja de contar cuando sus bombos se apagan. Estas reglas, el silencio
    y la alineacion fina solo actuan con mucho empuje; pop, house, etc. se mezclan como antes.
"""
import numpy as np

from .mix import camelot_dist, next_down, prev_down, sings, tempo_rel, vspans

LABELS = {"eq": "Mezcla EQ por frases", "bucle": "Bucle de frase", "voz": "Voz primero",
          "inst": "Instrumental primero", "corte": "Corte en frase con eco", "silencio": "Silencio y entrada",
          "fundido": "Fundido por bandas"}
KICK = .45


def _bar(I):
    return 4 * I["per"]


def bar_i(I, t):
    return int(np.floor((t - I.get("barStart", I["bar0"])) / _bar(I) + 1e-6))


def bar_t(I, i):
    return I.get("barStart", I["bar0"]) + i * _bar(I)


def kick(I, i):
    k = I.get("kick") or []
    return k[i] if 0 <= i < len(k) else 0.0


def driving(I):
    """musica que empuja con el bombo (club, o mucha energia con bombo marcado)"""
    k = I.get("kick") or []
    steady = np.mean([x >= KICK for x in k]) if k else 0
    return I["family"] == "club" or (I["energy"] >= 7 and steady > .6)


def sustained(I, i, n=4):
    """el compas i arranca un tramo con bombo seguido (el y al menos 3 de los 4 primeros)"""
    k = I.get("kick") or []
    if i < 0 or i + n > len(k) or k[i] < KICK:
        return False
    return sum(x >= KICK for x in k[i: i + n]) >= n - 1


def first_kick(I, t_from):
    """primer compas desde t_from con bombo (y el siguiente tambien): donde arranca de verdad"""
    k = I.get("kick") or []
    for i in range(max(0, bar_i(I, t_from)), len(k) - 1):
        if k[i] >= KICK and k[i + 1] >= KICK:
            return bar_t(I, i)
    return None


# ---- musica de mucho empuje (hard techno, techno duro): ahi el bombo manda y hay reglas propias. El resto (pop,
#      house, disco...) se mezcla como antes
def hard(I):
    return driving(I) and I["energy"] >= 9 and I["bpm"] >= 130


def kpow(I, i):
    p = I.get("kpow")
    if not p:
        return 1.0
    return p[i] if 0 <= i < len(p) else 0.0


def drive(I, i, th=.9):
    """compas con bombo a plena fuerza (no un ritmo suave a medio gas ni la cola que se apaga)"""
    return kick(I, i) >= KICK and kpow(I, i) >= th


def main_start(I, t_from):
    """primer compas desde t_from donde entran los bombos principales: 4 compases con bombo a plena fuerza"""
    k = I.get("kick") or []
    for i in range(max(0, bar_i(I, t_from)), len(k) - 3):
        if drive(I, i) and np.median([kpow(I, i + j) for j in range(4)]) >= .92                 and sum(kick(I, i + j) >= KICK for j in range(4)) >= 3:
            return bar_t(I, i)
    return None


def drive_end(I, t_to):
    """fin del ultimo compas con bombo a plena fuerza (con el anterior tambien) antes de t_to"""
    for i in range(min(len(I.get("kick") or []), bar_i(I, t_to)) - 1, 0, -1):
        if drive(I, i) and drive(I, i - 1):
            return bar_t(I, i + 1)
    return None


def kick_run(I, t, n_max=64):
    """cuantos compases seguidos (tolerando uno suelto) lleva bombo desde t"""
    k = I.get("kick") or []
    i, n, miss = bar_i(I, t), 0, 0
    while 0 <= i < len(k) and n < n_max:
        if k[i] >= KICK:
            miss = 0
        else:
            miss += 1
            if miss > 1:
                return max(0, n - 1)
        n += 1
        i += 1
    return n


def kfrac(I, t0, t1):
    """fraccion de compases con bombo entre t0 y t1"""
    k = I.get("kick") or []
    a, b = max(0, bar_i(I, t0)), min(len(k), bar_i(I, t1 - .01) + 1)
    return float(np.mean([x >= KICK for x in k[a:b]])) if b > a else 0.0


def ebar(I, i0, i1):
    e = I.get("energyBars") or []
    s = e[max(0, i0): max(0, i1)]
    return float(np.mean(s)) if s else 0.0


def last_kick_end(I, t_to):
    """fin del ultimo compas con bombo antes de t_to"""
    k = I.get("kick") or []
    for i in range(min(len(k), bar_i(I, t_to)) - 1, 0, -1):
        if k[i] >= KICK and k[i - 1] >= KICK:
            return bar_t(I, i + 1)
    return None


def phrase_floor(I, t, n=4):
    """compas de inicio de frase (cada n compases desde el principio de la rejilla) <= t"""
    i = bar_i(I, t)
    return bar_t(I, i - (i % n))


def plan(A, B, stems=True, bars=None):
    s, m = tempo_rel(A, B)
    match = abs(s - 1) <= .10 and A["conf"] > 1.1 and B["conf"] > 1.1
    kd = camelot_dist(A["key"], B["key"]) if A["key"]["conf"] > .02 and B["key"]["conf"] > .02 else 1
    de = B["energy"] - A["energy"]
    barA, beatA, barB = _bar(A), A["per"], _bar(B)
    VA, VB = vspans(A), vspans(B)
    Aend = prev_down(A, A["musicEnd"])
    b0 = next_down(B, B["musicStart"] - .1)
    lastVA = max([b for a, b in VA if a < Aend - .5], default=None)
    firstVB = min([a for a, b in VB if a > 1.0], default=None)
    dA, dB = driving(A), driving(B)
    hA, hB = hard(A), hard(B)
    hp = hA and hB
    kA_end = last_kick_end(A, Aend) if dA else None
    kB_start = first_kick(B, b0)
    soft_intro = kB_start is not None and kB_start - b0 >= 2 * barB
    jump = False
    if hp:
        # con mucho empuje lo que cuenta son los bombos PRINCIPALES (a plena fuerza), no el primer golpe: Fantasy
        # empieza con un ritmo suave a medio gas (0:29) y sus bombos de verdad entran en 0:58
        km = main_start(B, b0)
        if km is not None and km - b0 >= 2 * barB:
            kB_start, soft_intro = km, True
            i0, i1 = bar_i(B, b0), bar_i(B, km)
            # lo de antes de los bombos suena fuerte o ya lleva ritmo: encima de los bombos de A embarra -> B entra
            # directamente en sus bombos principales (bombo sobre bombo). Si es una intro tranquila, entra por debajo
            jump = kfrac(B, b0, km) >= .4 or ebar(B, i0, i1) >= ebar(B, i1, i1 + 16) - .6
    # hasta donde se puede usar A: si acaba con un outro sin bombo (o, con mucho empuje, con bombos que se apagan),
    # la mezcla termina antes
    A_use = Aend
    if hA:
        de_ = drive_end(A, Aend)
        if de_ and Aend - de_ >= barA:
            A_use = de_
    elif dA and kA_end and Aend - kA_end > 4 * barA:
        A_use = kA_end
    common = []
    if not match:
        common.append("tempos que no casan (%.0f→%.0f BPM)" % (A["bpm"], B["bpm"]))
    else:
        common.append("B se estira %+.1f%% sin cambiar el tono" % ((s - 1) * 100) + (" (medio/doble tempo)" if m != 1 else ""))
    common.append("tono %s→%s (Camelot %d)" % (A["key"]["camelot"], B["key"]["camelot"], kd))
    common.append("energía %d→%d" % (A["energy"], B["energy"]))
    if dA:
        common.append("A va con bombo" + (" (su outro sin bombo se evita)" if A_use < Aend else ""))
    epen = -.4 * max(0, abs(de) - 1)
    out = []

    def add(tpl, score, why, **p):
        p.update(tpl=tpl, label=LABELS[tpl], score=round(float(score), 2), why=why + common, align=bool(hp),
                 s=float(s) if p.pop("stretch", True) else 1.0)
        for k in list(p):
            if isinstance(p[k], (np.floating, np.integer)):
                p[k] = float(p[k])
        if p.get("loop"):
            p["loop"] = [float(x) for x in p["loop"]]
        out.append(p)

    def blend_b(pre_bars):
        """donde entra B y que contenido suyo cae en el cambio de graves: su primer bombo si tiene intro suave y la
        musica empuja (continuidad); si no, pre_bars compases despues de su principio"""
        if jump:   # B entra ya en sus bombos; el cambio de graves pre_bars despues, dejando bombo seguido detras
            pre = max(2, min(pre_bars, kick_run(B, kB_start) - 4))
            return kB_start, kB_start + pre * barB, pre
        if soft_intro and (dA or dB):
            Bk = kB_start
            pre = min(pre_bars, int((Bk - 0) / barB + 1e-6))
        else:
            Bk = b0 + pre_bars * barB
            pre = pre_bars
        pre = max(1, pre)
        return Bk - pre * barB, Bk, pre

    def clash(Ts, Te, b_in):
        both = 0.0
        for t in np.arange(Ts, Te, .25):
            bt = b_in + (t - Ts) * s
            if sings(VA, t, t + .25) > 0 and sings(VB, bt, bt + .25) > 0:
                both += .25
        return both / max(1e-6, Te - Ts)

    # ---- 1) mezcla EQ por frases (con continuidad del bombo)
    if match:
        best = None
        for L in ([int(bars)] if bars else [16, 8, 4]):
            post = min(L // 2, 4) if (dA or dB) else L // 2
            b_in, Bk, pre = blend_b(L - post)
            Tsw = phrase_floor(A, A_use - post * barA if not (dA and A_use < Aend) else A_use, 4 if L >= 8 else 1)
            Tsw = min(Tsw, prev_down(A, Aend - post * barA))
            Ts, Te = Tsw - pre * barA, Tsw + post * barA
            if Ts < A["dur"] * .35:
                continue
            c = clash(Ts, Tsw, b_in)
            sc = 2.2 + .06 * (pre + post) - 3 * c - (0 if kd <= 2 else .35 * (kd - 2) * (pre + post) / 8) + epen   # +2 en la rueda = energy boost (vale)
            if dA and dB:
                sc += .9 + (.5 if hp and abs(s - 1) <= .02 else 0)     # hard techno al mismo tempo: lo mas seguro
            if (dA or dB) and soft_intro:
                sc += .3
            if hA:   # los bombos de A tienen que seguir sonando (a plena fuerza) mientras B entra
                ia, ib = bar_i(A, Ts), bar_i(A, Tsw)
                fa = float(np.mean([drive(A, i, .75) for i in range(ia, ib)])) if ib > ia else 1.0
                if fa < .8:
                    sc -= 2.5 * (.8 - fa)
            if best is None or sc > best[0]:
                best = (sc, pre + post, Ts, Tsw, Te, b_in, c, Bk)
        if best:
            sc, L, Ts, Tsw, Te, b_in, c, Bk = best
            if jump:
                why = ["B entra directamente en sus bombos principales, a plena fuerza (se salta lo de antes: intro o ritmo suave)",
                       "%d compases bombo sobre bombo; cambio de graves al acabar la frase" % L]
            else:
                why = ["%d compases encima; cambio de graves en %s" % (L, "el primer bombo de B" if soft_intro and (dA or dB) else "la mitad")]
            if c > .05:
                why.append("se pisan las voces un %d%% del tiempo" % (c * 100))
            add("eq", sc, why, ts=Ts, b_in=b_in, bars=L, tsw=Tsw, te=Te)
    # ---- 2) bucle de frase (un trozo que se repite: se queda en bucle y B entra encima)
    if match and A.get("rep8"):
        cands = []
        n = len(A["rep8"])
        lo = bar_i(A, max(A["dur"] * .62, Aend - 100))   # cerca del final: no se salta media cancion
        for i in range(max(0, lo), min(n, bar_i(A, Aend) - 8)):
            if i % 4:
                continue
            r8, r4 = A["rep8"][i], A["rep4"][i] if i < len(A["rep4"]) else 0
            if r8 >= .93 or r4 >= .95:
                ln = 8 if r8 >= .93 else 4
                if dA and np.mean([kick(A, i + j) for j in range(ln)]) < KICK:
                    continue   # el bucle tiene que llevar bombo si la musica empuja
                L0 = bar_t(A, i)
                lyr = any(L0 - 1 <= r["t0"] <= L0 + ln * barA for r in A.get("repeats") or [])
                cands.append(((r8 if ln == 8 else r4) + (.05 if lyr else 0) + .02 * (i / n), L0, ln, lyr))
        if cands:
            rv, L0, ln, lyr = max(cands)
            L1 = L0 + ln * barA
            pre_b = int(bars) if bars else 8
            b_in, Bk, pre = blend_b(pre_b)
            Ts = L1
            Tsw = Ts + pre * barA
            Te = Tsw + 4 * barA
            weak_end = (dA and A_use < Aend) or (A["musicEnd"] < A["dur"] - 8)
            sc = 2.4 + (1.0 if weak_end else 0) + (.4 if lyr else 0) + 2 * (rv - .93) - (0 if kd <= 1 else .4 * (kd - 1)) + epen
            add("bucle", sc, ["A repite una frase de %d compases%s: se queda en bucle ahí" % (ln, " (con su letra)" if lyr else ""),
                              "B entra encima y el cambio de graves cae en %s" % ("una frase de sus bombos" if jump else "su primer bombo" if soft_intro and (dA or dB) else "una frase")],
                ts=Ts, b_in=b_in, bars=pre + 4, tsw=Tsw, te=Te, loop=[L0, L1])
    # ---- 3) voz primero
    if match and stems and kd <= 2 and lastVA and firstVB:
        Bs = next_down(A, lastVA + .1)
        bV = B["bar0"] + np.floor((firstVB + .25 * B["per"] - B["bar0"]) / barB) * barB
        bin_ = bV - barB if bV - barB >= 0 else bV
        Ts = Bs - (bV - bin_) / s
        Tsw = Bs + 8 * barA
        cover = sings(VB, bV, bV + 8 * barB) / (8 * barB)
        if Ts > A["dur"] * .35 and cover > .3:
            LL = 2 if Aend - Bs >= 2 * barA else 1
            sc = 3.0 - .6 * max(0, kd - 1) + epen + (.5 if Aend - Bs >= barA else 0) + .5 * cover - (.8 if dA and dB else 0)
            add("voz", sc, ["la voz de B entra al acabar la última línea de A, 8 compases sola",
                            "la base de A sigue (en bucle si se le acaba) y se desvanece 8 compases"],
                ts=Ts, b_in=bin_, bs=Bs, tsw=Tsw, te=Tsw + 8 * barA, loop=[Aend - LL * barA, Aend])
    # ---- 4) instrumental primero
    if match and stems and kd <= 2 and lastVA and firstVB and Aend - lastVA < 4 * barA:
        Tv = next_down(A, lastVA - .1)
        bV = B["bar0"] + np.floor((firstVB + .25 * B["per"] - B["bar0"]) / barB) * barB
        Lb = min(8, int((bV - b0) / barB + 1e-6))
        if Lb >= 4:
            bin_ = bV - Lb * barB
            Ts = Tv - (bV - bin_) / s
            sc = 2.8 - .6 * max(0, kd - 1) + epen - (.8 if dA and dB else 0)
            add("inst", sc, ["la instrumental de B (%d compases) sustituye a la de A bajo su voz" % Lb,
                             "B canta justo cuando A acaba su última línea"],
                ts=Ts, b_in=bin_, tm=Ts + (Lb // 2) * barA, tv=Tv, te=Tv + beatA)
    # ---- 5) corte en frase (en un silencio de la voz de A); si A empuja, B entra directamente en sus bombos
    cands = []
    for t in np.arange(next_down(A, A["dur"] * .6), A_use + 1e-3, barA):
        if sings(VA, t - .5 * beatA, t + .1) > 0:
            continue
        if dA and kick(A, bar_i(A, t) - 1) < KICK:
            continue
        ended = any(t - 2 * barA <= b <= t for a, b in VA)
        cands.append(((1 if ended else 0) + .3 * (t / Aend) + (.3 if bar_i(A, t) % 4 == 0 else 0), t))
    if cands:
        _, Tc = max(cands)
        bin_ = b0
        why_b = "su primer compás"
        if dA and soft_intro:
            bin_, why_b = kB_start, "sus bombos (se salta la intro suave)"
        else:
            ch = [g["t0"] for g in B.get("gsections") or [] if g["label"] == "estribillo" and g["t0"] > 4]
            ch = ch or [x["t0"] for x in B.get("sections") or [] if x["label"] == "estribillo" and x["t0"] > 4]
            if ch and A["energy"] - B["energy"] >= 1:
                bin_, why_b = next_down(B, ch[0] - .05), "su estribillo"
        sc = 1.2 + (1.2 if not match else 0) + (.4 if sings(VA, Tc - 2 * barA, Tc) > 0 else 0) + (.5 if dA and soft_intro else 0)
        add("corte", sc, ["A corta en el \"uno\" justo tras una frase, con eco", "B entra en " + why_b],
            ts=Tc, b_in=bin_, te=Tc, stretch=match)
    # ---- 5b) silencio y entrada (musica de mucho empuje): A deja de empujar (un paron o su final, cuando sus bombos
    #      ya se apagan) y B arranca justo ahi con la voz sola que precede a sus bombos ("this is chaos"); sus bombos
    #      caen en el "uno" donde volverian los de A. Sin voz de arranque no hay silencio que llenar: corte o mezcla
    if hp and B.get("kick"):
        nA = len(A["kick"])
        gaps = []
        for g in range(max(3, bar_i(A, A["dur"] * .6)), nA):
            if not drive(A, g) and drive(A, g - 1) and drive(A, g - 2):
                gl = 1
                while g + gl < nA and not drive(A, g + gl):
                    gl += 1
                Tg = bar_t(A, g)
                at_end = g + gl >= nA - 1 or bar_t(A, g + gl) >= A["musicEnd"] - .5
                dip = ebar(A, g - 4, g) - ebar(A, g, g + min(gl, 2))
                if gl > 4 and not at_end:
                    continue        # una seccion entera sin bombo, no un paron
                if not at_end and (dip < 1.0 or Tg < A["dur"] * .72):
                    continue        # un hueco de verdad (cae la energia) y en el ultimo tramo
                if at_end and dip < .5 and A["musicEnd"] - Tg > 4 * barA:
                    continue        # un final que se apaga despacio no es un paron
                gaps.append((dip + .6 * (Tg / A["dur"]) + (.3 if at_end else 0), g, gl, Tg, at_end, dip))
        nB = len(B["kick"])
        VBs = vspans(B)
        launches = []
        for j in range(1, min(nB - 4, bar_i(B, min(B["dur"] * .4, 75)))):
            # arranque: bombo fuerte tras un compas sin bombo, con bombo en el tramo que sigue, y una voz sola justo
            # antes (empieza en los 2 compases previos y llega hasta el bombo)
            if kpow(B, j) < .9 or np.mean([kpow(B, j + x) for x in range(4)]) < .5 or kpow(B, j - 1) >= .5:
                continue
            Tj = bar_t(B, j)
            vs = [a for a, b in VBs if Tj - 2 * barB <= a <= Tj - .3 and b >= Tj - .6 * barB]
            if not vs:
                continue
            gb = 0                      # compases sin bombo justo antes (un paron de verdad = 2 o mas)
            while j - gb - 1 >= 0 and kpow(B, j - gb - 1) < .5:
                gb += 1
            launches.append((1.2 + (.6 if gb >= 2 else 0) - .02 * j, j, Tj, Tj - min(vs), gb))
        if gaps and launches:
            gsc, g, gl, Tg, at_end, dip = max(gaps)
            lsc, j, Tj, pre, gb = max(launches)
            # la voz de B empieza al dejar de empujar A y sus bombos caen en el "uno" siguiente de A que les toque
            nb = max(1 if pre > .5 * beatA else 0, int(np.ceil((pre - .5 * beatA) / barA - 1e-6)))
            if not at_end:
                nb = max(nb, min(gl, 2))
            Tk = Tg + nb * barA
            Tv = Tk - pre              # B empieza aqui (su voz sola), a su tempo
            early = max(0.0, Tg - Tv)  # cuanto se mete la voz de B antes del hueco
            # con el mismo tempo manda el bombo sobre bombo (eq); esto gana con tempos distintos (B entra a su tempo)
            tb = 1.5 if not match else (.6 if abs(s - 1) > .03 else -.4)
            sc = 1.4 + lsc + .8 * float(np.clip(dip, 0, 2)) + tb - 1.5 * early / barA
            why = ["A deja de empujar%s en %s" % (" (su final)" if at_end else " (un parón)", "%d:%02d" % (Tg // 60, Tg % 60)),
                   "B arranca con su voz sola (%.1f s) justo ahí, mientras A se apaga" % pre,
                   "los bombos de B caen en el \"uno\" donde volverían los de A"]
            add("silencio", sc, why, ts=Tv, b_in=Tj - pre, te=Tk, tg=Tg, stretch=False)
    # ---- 6) fundido por bandas
    W = (int(bars) if bars else 4) * barA if match else 6.0
    fb = b0
    pen = 0
    if dA and soft_intro:
        fb = kB_start - (W * s if match else 0)
        fb = max(0.0, fb)
    add("fundido", .6 + (.6 if not match else 0) - pen, ["agudos, medios y graves cambian a destiempo (%.1f s)" % W],
        ts=A_use - W, b_in=fb, te=A_use, stretch=match)
    out.sort(key=lambda x: -x["score"])
    for i, c in enumerate(out):
        c["id"] = i
        c["auto"] = i == 0
    return out
