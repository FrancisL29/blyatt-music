#!/usr/bin/env python3
"""Blyatt - app de escritorio. Sirve la web local y la abre en una ventana nativa (pywebview)."""
import json
import os
import threading
import time

import webview

import app
from app import serve

PORT = 8000
LOGIN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fmusic.youtube.com"
SPOT_URL = "https://open.spotify.com/"

# se pone True cuando la ventana principal se cierra: los polls de login abortan y cierran su ventana
# (evita que webview.start() siga vivo esperando a una ventana de login abierta -> proceso zombie)
_APP_CLOSING = threading.Event()


def _cookie_header(cookie_list):
    jar = {}
    for c in cookie_list or []:
        try:
            for k, morsel in c.items():
                jar[k] = morsel.value
        except AttributeError:
            pass
    return "; ".join("%s=%s" % (k, v) for k, v in jar.items())


def google_login(silent=False):
    # ventana con el login REAL de Google sobre el perfil persistente de WebView2.
    # El perfil mantiene la sesion viva (rota cookies como un navegador normal);
    # aqui solo se extraen cookies frescas y se guardan en auth/browser.json.
    w = webview.create_window("Conectar con Google", "https://music.youtube.com" if silent else LOGIN_URL,
                              hidden=bool(silent), width=990, height=760)

    def poll():
        tries = 30 if silent else 600   # 1 min silencioso / 20 min para login manual
        for _ in range(tries):
            if _APP_CLOSING.is_set():
                break
            time.sleep(2)
            try:
                if "music.youtube.com" in (w.get_current_url() or ""):
                    ck = _cookie_header(w.get_cookies())
                    if "SAPISID" in ck:
                        ua = None
                        try:
                            ua = w.evaluate_js("navigator.userAgent")
                        except Exception:
                            pass
                        if app.save_browser_cookie(ck, ua):
                            break
            except Exception:
                pass
        try:
            w.destroy()
        except Exception:
            pass

    threading.Thread(target=poll, daemon=True).start()


def _spot_control(uid):
    # control WinForms WebView2 de la ventana pywebview (tiene BeginInvoke -> hilo UI) + su CoreWebView2
    try:
        from webview.platforms.winforms import BrowserView
        bv = BrowserView.instances.get(uid)
        ctrl = bv and bv.browser and bv.browser.webview
        return ctrl
    except Exception:
        return None


_SPOT_LOG = os.path.join(app.BASE, "auth", "spot_debug.log")


def _spot_log(msg):
    # diagnostico de la captura de token (auth/spot_debug.log). Ayuda a ver si el Bearer llega.
    try:
        os.makedirs(os.path.dirname(_SPOT_LOG), exist_ok=True)
        with open(_SPOT_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def spotify_login():
    # Spotify metio TOTP en /get_access_token y su bundle captura window.fetch antes de que un
    # monkeypatch JS pueda actuar -> ambos caminos JS fallan. ELEGANTE: interceptamos en la CAPA
    # NATIVA de red (WebResourceRequested) el header Authorization: Bearer que el player ya envia a
    # api.spotify.com (token que YA paso el TOTP). CRITICO: WebView2 tiene afinidad al hilo UI ->
    # suscribir el evento DEBE hacerse via BeginInvoke (si se hace desde un hilo daemon, deadlock COM
    # = pantalla en blanco). Nos desuscribimos al captar el primer token (WebResourceRequested es
    # BLOQUEANTE por request: dejarlo activo penaliza cada recurso de la pagina).
    w = webview.create_window("Conectar con Spotify", SPOT_URL, width=990, height=760)
    holder = {"tok": "", "core": None, "cdp": None}
    _spot_log("--- login iniciado ---")

    def _grab(bearer, src):
        if bearer and bearer.startswith("Bearer "):
            b = bearer[7:]
            if b and b != holder["tok"]:
                holder["tok"] = b
                _spot_log("bearer capturado via " + src)

    def on_request(sender, args):
        # WebResourceRequested (backup). SIN filtro de host: el web player pega a api-partner/
        # gue1-spclient, no a api.spotify.com. El Bearer solo sale en llamadas API (nunca en estaticos).
        try:
            for hdr in args.Request.Headers.GetEnumerator():
                if str(hdr.Key).lower() == "authorization":
                    _grab(str(hdr.Value), "webresource")
                    break
        except Exception:
            pass

    def on_cdp(sender, args):
        # CDP Network.requestWillBeSent (PRIMARIO): ve los headers ANTES del Service Worker de Spotify,
        # que es punto ciego de WebResourceRequested. Cubre fetch/XHR/SW por igual.
        try:
            d = json.loads(args.ParameterObjectAsJson or "{}")
            hs = ((d.get("request") or {}).get("headers")) or {}
            for k, val in hs.items():
                if k.lower() == "authorization":
                    _grab(str(val), "cdp")
                    break
        except Exception:
            pass

    def wire():
        # espera a CoreWebView2 y suscribe AMBOS interceptores EN EL HILO UI via BeginInvoke
        # (WebView2 tiene afinidad de hilo; hacerlo desde el daemon = deadlock COM = pantalla blanca)
        from System import Action
        for _ in range(120):
            if _APP_CLOSING.is_set():
                return
            ctrl = _spot_control(w.uid)
            core = ctrl and getattr(ctrl, "CoreWebView2", None)
            if ctrl and core:
                holder["core"] = core

                def sub():
                    try:
                        core.WebResourceRequested += on_request   # backup
                    except Exception as e:
                        _spot_log("err WebResourceRequested: " + str(e)[:80])
                    try:
                        core.CallDevToolsProtocolMethod("Network.enable", "{}")
                        rec = core.GetDevToolsProtocolEventReceiver("Network.requestWillBeSent")
                        rec.DevToolsProtocolEventReceived += on_cdp   # primario
                        holder["cdp"] = rec
                        _spot_log("interceptores enganchados (CDP + WebResource)")
                    except Exception as e:
                        _spot_log("err CDP: " + str(e)[:80])
                try:
                    ctrl.BeginInvoke(Action(sub))
                except Exception as e:
                    _spot_log("err BeginInvoke: " + str(e)[:80])
                return
            time.sleep(0.5)
        _spot_log("wire: CoreWebView2 nunca inicializo")

    def unsub():
        # desengancha ambos interceptores EN EL HILO UI (misma afinidad)
        ctrl = _spot_control(w.uid)
        if not ctrl or not holder["core"]:
            return
        from System import Action

        def do():
            try:
                holder["core"].WebResourceRequested -= on_request
            except Exception:
                pass
            try:
                if holder.get("cdp"):
                    holder["cdp"].DevToolsProtocolEventReceived -= on_cdp
            except Exception:
                pass
        try:
            ctrl.BeginInvoke(Action(do))
        except Exception:
            pass

    def poll():
        last = ""
        for _ in range(600):   # 20 min max para login manual
            if _APP_CLOSING.is_set():
                break
            time.sleep(2)
            t = holder["tok"]
            if t and t != last:   # valida solo tokens nuevos (cada Bearer distinto)
                last = t
                try:
                    if app.spot_set_token(t):
                        _spot_log("token VALIDO -> sesion Spotify lista, cerrando ventana")
                        unsub()   # token bueno: deja de interceptar
                        break
                    _spot_log("token rechazado por /me (anonimo o sin scope), sigo esperando")
                except Exception as e:
                    _spot_log("err validando: " + str(e)[:80])
        unsub()
        try:
            w.destroy()
        except Exception:
            pass

    threading.Thread(target=wire, daemon=True).start()
    threading.Thread(target=poll, daemon=True).start()


if __name__ == "__main__":
    app.WEBLOGIN = google_login
    app.SPOTLOGIN = spotify_login
    httpd = serve(PORT)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    main_win = webview.create_window("Blyatt", f"http://127.0.0.1:{PORT}",
                                     width=1200, height=800, min_size=(900, 600))
    # al cerrar la ventana principal, avisa a los polls de login para que cierren sus ventanas
    # (si no, una ventana de login abierta mantiene webview.start() vivo = proceso zombie)
    main_win.events.closing += lambda: _APP_CLOSING.set()
    webview.start(private_mode=False, storage_path=os.path.join(app.BASE, "auth", "webview"))
    _APP_CLOSING.set()
    httpd.shutdown()
    os._exit(0)   # garantiza que ningun hilo/ventana rezagado deje el proceso vivo
