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


# Spotify metio TOTP en /get_access_token: un fetch directo ya no da token valido. En vez de
# reimplementar el TOTP, INTERCEPTAMOS el Bearer real que el propio web player usa en cada peticion
# a api/spclient.spotify.com (fetch + XHR monkeypatch). El web player ya hizo el TOTP -> su token
# es valido. Se instala un hook una vez y el token se captura en la siguiente request del player
# (playback state, etc. disparan requests constantes). Fallback: /get_access_token por si acaso.
SPOT_FETCH_JS = r"""
(() => {
  if (!window.__spotHook) {
    window.__spotHook = true;
    window.__spotTok = window.__spotTok || "";
    const grab = (v) => {
      try {
        if (typeof v === "string" && v.slice(0, 7) === "Bearer ") window.__spotTok = v.slice(7);
      } catch (e) {}
    };
    const digHeaders = (h) => {
      try {
        if (!h) return;
        if (typeof h.get === "function") grab(h.get("authorization") || h.get("Authorization"));
        else if (typeof h === "object") { for (const k in h) if (k.toLowerCase() === "authorization") grab(h[k]); }
      } catch (e) {}
    };
    const of = window.fetch;
    window.fetch = function (input, init) {
      try {
        if (input instanceof Request) digHeaders(input.headers);
        if (init && init.headers) digHeaders(init.headers);
      } catch (e) {}
      return of.apply(this, arguments);
    };
    const os = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
      try { if (String(k).toLowerCase() === "authorization") grab(v); } catch (e) {}
      return os.apply(this, arguments);
    };
    // fallback: endpoint clasico (puede fallar por TOTP, pero es gratis intentarlo)
    fetch("https://open.spotify.com/get_access_token?reason=transport&productType=web_player", { credentials: "include" })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d && d.accessToken && !window.__spotTok) window.__spotTok = d.accessToken; })
      .catch(() => {});
  }
  return window.__spotTok || "";
})()
"""


def spotify_login():
    # ventana con el web player real de Spotify (perfil WebView2 persistente: la sesion se reusa).
    # El token Bearer se intercepta de las propias peticiones del web player (ver SPOT_FETCH_JS).
    # Tokens anonimos (sin sesion) fallan en la validacion /v1/me y se descartan. Si el usuario
    # ve la pagina publica, debe pulsar "Iniciar sesion" ARRIBA A LA DERECHA.
    w = webview.create_window("Conectar con Spotify", SPOT_URL, width=990, height=760)

    def poll():
        for _ in range(600):   # 20 min max para login manual
            if _APP_CLOSING.is_set():   # cerraron Blyatt: no dejar esta ventana viva
                break
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
    main_win = webview.create_window("Blyatt", f"http://127.0.0.1:{PORT}",
                                     width=1200, height=800, min_size=(900, 600))
    # al cerrar la ventana principal, avisa a los polls de login para que cierren sus ventanas
    # (si no, una ventana de login abierta mantiene webview.start() vivo = proceso zombie)
    main_win.events.closing += lambda: _APP_CLOSING.set()
    webview.start(private_mode=False, storage_path=os.path.join(app.BASE, "auth", "webview"))
    _APP_CLOSING.set()
    httpd.shutdown()
    os._exit(0)   # garantiza que ningun hilo/ventana rezagado deje el proceso vivo
