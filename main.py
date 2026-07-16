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


SPOT_FETCH_JS = r"""
(() => {
  if (window.__spotBusy) return;
  window.__spotBusy = true;
  fetch("https://open.spotify.com/get_access_token?reason=transport&productType=web_player", { credentials: "include" })
    .then(r => r.ok ? r.json() : null)
    .then(d => { window.__spotTok = (d && d.accessToken) || ""; window.__spotBusy = false; })
    .catch(() => { window.__spotTok = ""; window.__spotBusy = false; });
})(); window.__spotTok || ""
"""


def spotify_login():
    # ventana con el web player real de Spotify (perfil WebView2 persistente: la sesion se reusa).
    # Antes leiamos el script #session (Spotify lo quito); ahora llamamos /get_access_token desde
    # DENTRO de la ventana (usa las cookies sp_dc de la sesion). Tokens anonimos (sin sesion) fallan
    # en la validacion /v1/me y se descartan. Si el usuario ve la pagina publica, debe pulsar
    # "Iniciar sesion" ARRIBA A LA DERECHA; el poll captura el token cuando el login termina.
    w = webview.create_window("Conectar con Spotify", SPOT_URL, width=990, height=760)

    def poll():
        for _ in range(600):   # 20 min max para login manual
            time.sleep(2)
            try:
                if "open.spotify.com" not in (w.get_current_url() or ""):
                    continue
                tok = w.evaluate_js(SPOT_FETCH_JS) or ""
                if tok and app.spot_set_token(tok):
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
    webview.create_window("Blyatt", f"http://127.0.0.1:{PORT}",
                          width=1200, height=800, min_size=(900, 600))
    webview.start(private_mode=False, storage_path=os.path.join(app.BASE, "auth", "webview"))
    httpd.shutdown()
