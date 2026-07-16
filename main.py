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


def _cookie_value(cookie_list, name):
    # extrae una cookie concreta (sp_dc) de la lista que devuelve get_cookies
    for c in cookie_list or []:
        try:
            for k, morsel in c.items():
                if k == name:
                    return morsel.value
        except AttributeError:
            pass
    return ""


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


def spotify_login():
    # Login robusto SIN eventos nativos (los intentos de interceptar el token en la capa de red
    # fallaban: afinidad de hilo, Service Worker, no alcanzar CoreWebView2 en ventanas runtime).
    # ELEGANTE: el usuario inicia sesion en el web player, extraemos la cookie sp_dc con get_cookies
    # (API oficial de pywebview, la MISMA que usa el login de Google) y app.spot_set_dc genera el
    # access token desde sp_dc + TOTP. sp_dc dura ~1 anio -> login persistente, sin re-loguear.
    w = webview.create_window("Conectar con Spotify", SPOT_URL, width=990, height=760)

    def poll():
        for _ in range(600):   # 20 min max para login manual
            if _APP_CLOSING.is_set():
                break
            time.sleep(2)
            try:
                if "open.spotify.com" not in (w.get_current_url() or ""):
                    continue
                sp_dc = _cookie_value(w.get_cookies(), "sp_dc")
                if sp_dc and app.spot_set_dc(sp_dc):   # genera+valida el token; True = sesion lista
                    break
            except Exception:
                pass
        try:
            w.destroy()
        except Exception:
            pass

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
