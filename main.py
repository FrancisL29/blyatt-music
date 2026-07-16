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


def _spot_core(uid):
    # llega al CoreWebView2 nativo de la ventana de pywebview (backend Edge/WebView2)
    try:
        from webview.platforms.winforms import BrowserView
        bv = BrowserView.instances.get(uid)
        core = bv and bv.browser and bv.browser.webview and bv.browser.webview.CoreWebView2
        return core
    except Exception:
        return None


def spotify_login():
    # Spotify metio TOTP en /get_access_token y su bundle captura window.fetch antes de que un
    # monkeypatch por JS pueda actuar -> ambos caminos JS son fragiles. ELEGANTE: interceptamos en la
    # CAPA NATIVA de red (WebResourceRequested de WebView2) el header Authorization: Bearer que el
    # propio web player ya envia a api.spotify.com (token que YA paso el TOTP). Cero JS, cero race.
    w = webview.create_window("Conectar con Spotify", SPOT_URL, width=990, height=760)
    holder = {"tok": ""}

    def on_request(sender, args):
        try:
            uri = args.Request.Uri or ""
            if "api.spotify.com" not in uri and "spclient" not in uri:
                return
            for hdr in args.Request.Headers.GetEnumerator():
                if str(hdr.Key).lower() == "authorization":
                    v = str(hdr.Value)
                    if v.startswith("Bearer "):
                        holder["tok"] = v[7:]   # ligero: solo guarda; el poll valida (no bloquea la UI)
                    break
        except Exception:
            pass

    def wire():
        # espera a que CoreWebView2 inicialice y engancha el interceptor (pywebview ya puso el filtro '*')
        for _ in range(60):
            if _APP_CLOSING.is_set():
                return
            core = _spot_core(w.uid)
            if core:
                try:
                    core.WebResourceRequested += on_request
                except Exception:
                    pass
                return
            time.sleep(0.5)

    def poll():
        last = ""
        for _ in range(600):   # 20 min max para login manual
            if _APP_CLOSING.is_set():
                break
            time.sleep(2)
            t = holder["tok"]
            if t and t != last:   # solo valida tokens nuevos (evita revalidar el mismo)
                last = t
                try:
                    if app.spot_set_token(t):
                        break
                except Exception:
                    pass
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
