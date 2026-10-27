#!/usr/bin/env python3
"""Blyatt - app de escritorio. Sirve la web local y la abre en una ventana nativa (pywebview)."""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import winreg

import webview
from webview.platforms import edgechromium

import app
from app import serve

PORT = int(os.environ.get("BLYATT_PORT") or 8000)   # otro puerto solo para pruebas (el origen fija el localStorage)
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


def google_logout():
    # el login de Google vive en el perfil persistente de WebView2 (auth/webview), compartido con la
    # ventana principal: si no se borran sus cookies, "Iniciar sesion" recaptura la misma cuenta y es
    # imposible entrar con otra. En escritorio la app no depende de cookies propias (localStorage no se toca)
    try:
        main_win.clear_cookies()
    except Exception:
        pass


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


def connect_login():
    # Dispositivos: el hub vive en el server publico (Cloudflare Access). Una vez por mes aprox.: el usuario
    # entra con su codigo de email en esta ventana y se guarda la cookie CF_Authorization para el relay.
    w = webview.create_window("Conectar esta PC con Blyatt", app.CONNECT_REMOTE + "/", width=520, height=760)

    def poll():
        for _ in range(450):   # 15 min max
            if _APP_CLOSING.is_set():
                break
            time.sleep(2)
            try:
                cf = _cookie_value(w.get_cookies(), "CF_Authorization")
                if cf and app.connect_set_access(cf):
                    break
            except Exception:
                pass
        try:
            w.destroy()
        except Exception:
            pass

    threading.Thread(target=poll, daemon=True).start()


def _msgbox(text):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Blyatt", 0x40)
    except Exception:
        pass


# ---------- barra de titulo propia ----------
# Windows 10 no deja colorear la barra nativa: se quita (WM_NCCALCSIZE) y la pagina pinta la suya, transparente.
# Los bordes invisibles de redimension, la sombra y el snap siguen siendo los de Windows; el arrastre lo
# hace WebView2 con la CSS `app-region: drag` (doble clic maximiza, Aero Snap, menu del sistema).
# El procedimiento de ventana es C# (NativeWindow): en Python cada mensaje de la ventana esperaria al GIL y
# la ventana se congelaba mientras el servidor trabaja. Se compila una vez con el csc de .NET Framework.
_u32 = ctypes.windll.user32
_FRAME_CS = r"""
using System; using System.Runtime.InteropServices; using System.Windows.Forms;
namespace BlyattNative {
public class Frame : NativeWindow {
  [DllImport("user32.dll")] static extern bool IsZoomed(IntPtr h);
  [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr h);
  [DllImport("user32.dll")] static extern int GetSystemMetricsForDpi(int i, uint dpi);
  [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] static extern bool PostMessage(IntPtr h, int m, IntPtr w, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  bool away, fix; int w, ht;
  public Frame(IntPtr h) { AssignHandle(h); SetWindowPos(h, IntPtr.Zero, 0, 0, 0, 0, 0x37); }   // FRAMECHANGED
  protected override void WndProc(ref Message m) {
    if (m.Msg == 0x83 && m.WParam != IntPtr.Zero) {   // WM_NCCALCSIZE: bordes de Windows salvo arriba (sin titulo)
      int top = ((RECT)Marshal.PtrToStructure(m.LParam, typeof(RECT))).T;
      base.WndProc(ref m);
      RECT r = (RECT)Marshal.PtrToStructure(m.LParam, typeof(RECT));
      if (IsZoomed(m.HWnd)) {   // maximizada sobresale el grosor del borde: que no se corte arriba
        uint d = GetDpiForWindow(m.HWnd); top += GetSystemMetricsForDpi(33, d) + GetSystemMetricsForDpi(92, d);
      }
      r.T = top; Marshal.StructureToPtr(r, m.LParam, false);
      return;
    }
    if (m.Msg == 0x5) {   // WM_SIZE: WinForms, al restaurar (de maximizada o minimizada), vuelve a sumar el alto
      int k = (int)m.WParam;  // del titulo que ya no existe: se reaplica el ultimo tamano normal
      if (k == 1 || k == 2) away = true;
      else if (k == 0 && away) { away = false; fix = true; PostMessage(m.HWnd, 0x8007, IntPtr.Zero, IntPtr.Zero); }
      else if (k == 0 && !fix) { RECT r; GetWindowRect(m.HWnd, out r); w = r.R - r.L; ht = r.B - r.T; }
    } else if (m.Msg == 0x8007) {   // se reaplica el ultimo tamano normal (la posicion la decide Windows)
      if (w > 0) SetWindowPos(m.HWnd, IntPtr.Zero, 0, 0, w, ht, 0x16);
      fix = false; return;
    }
    base.WndProc(ref m);
  }
}}
"""
_frame = []   # la NativeWindow debe vivir mientras exista la ventana


def _frame_type():
    import hashlib
    import clr
    dll = os.path.join(app.DATA, "cache", "blyatt_frame_%s.dll" % hashlib.sha1(_FRAME_CS.encode()).hexdigest()[:10])
    if not os.path.isfile(dll):
        from Microsoft.CSharp import CSharpCodeProvider
        from System.CodeDom.Compiler import CompilerParameters
        os.makedirs(os.path.dirname(dll), exist_ok=True)
        cp = CompilerParameters(["System.dll", "System.Windows.Forms.dll"], dll)
        cp.GenerateInMemory = False
        res = CSharpCodeProvider().CompileAssemblyFromSource(cp, _FRAME_CS)
        if res.Errors.HasErrors:
            raise RuntimeError("; ".join(str(e) for e in res.Errors))
        for f in os.listdir(os.path.dirname(dll)):   # versiones anteriores
            if f.startswith("blyatt_frame_") and f != os.path.basename(dll):
                try:
                    os.remove(os.path.join(os.path.dirname(dll), f))
                except OSError:
                    pass
    clr.AddReference(dll)
    from BlyattNative import Frame
    return Frame


def _strip_caption(native):
    """En el hilo de WinForms. Si algo falla queda la barra de Windows (y la pagina no pinta la suya)."""
    try:
        _frame.append(_frame_type()(native.Handle))
        return True
    except Exception as e:
        print("barra de titulo propia no disponible:", e)
        return False


def _nonclient_regions():
    # `app-region: drag` solo funciona con esta opcion de WebView2, antes de la primera navegacion. La barra de
    # Windows se quita solo si se pudo activar (un WebView2 antiguo sin ella dejaria la ventana sin forma de moverla)
    ready = edgechromium.EdgeChrome.on_webview_ready

    def on_ready(self, sender, args):   # hilo de WinForms
        main = self.pywebview_window is main_win
        try:
            sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
            if main:
                _HWND[0] = main_win.native.Handle.ToInt64()
                _HWND[1] = _strip_caption(main_win.native)
        except Exception as e:
            print("regiones de arrastre de WebView2 no disponibles:", e)
        ready(self, sender, args)
    edgechromium.EdgeChrome.on_webview_ready = on_ready


_HWND = [0, False]   # handle y si se quito la barra de Windows (se guardan en el hilo de WinForms)


def win_cmd(cmd):
    """Botones de la barra propia (min / max-restaurar / cerrar) y su estado. Como los de Windows: WM_SYSCOMMAND
    (animaciones nativas, sin bloquear el hilo HTTP)."""
    h = _HWND[0]
    zoomed = bool(h and _u32.IsZoomed(h))
    sc = {"min": 0xF020, "max": 0xF120 if zoomed else 0xF030, "close": 0xF060}.get(cmd)   # SC_MINIMIZE/RESTORE/MAXIMIZE/CLOSE
    if h and sc:
        _u32.PostMessageW(h, 0x0112, sc, 0)
        if cmd == "max":
            zoomed = not zoomed
    return {"max": zoomed, "frame": _HWND[1]}


# ---------- abrir al iniciar Windows (solo esta cuenta: HKCU\...\Run) ----------
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_cmd():
    if app.FROZEN:
        return '"%s"' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")   # desarrollo: sin consola
    return '"%s" "%s"' % (pyw if os.path.isfile(pyw) else sys.executable, os.path.abspath(__file__))


def autostart(on=None):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if on is True:
            winreg.SetValueEx(k, "Blyatt", 0, winreg.REG_SZ, _launch_cmd())
        elif on is False:
            try:
                winreg.DeleteValue(k, "Blyatt")
            except FileNotFoundError:
                pass
        try:
            return bool(winreg.QueryValueEx(k, "Blyatt")[0])
        except FileNotFoundError:
            return False


def _frozen_setup():
    """App instalada (.exe): una sola instancia, sin ventanas de consola para ffmpeg/node, auto-update."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # el instalador espera a que este mutex desaparezca antes de reemplazar archivos (actualizaciones)
    globals()["_MUTEX"] = k32.CreateMutexW(None, False, "BlyattAppMutex")
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        _msgbox("Blyatt ya está abierto.")
        os._exit(0)
    # sin consola sys.stdout/err son None (yt-dlp y http.server escriben ahi): a un log por sesion
    import sys
    os.makedirs(app.DATA, exist_ok=True)
    sys.stdout = sys.stderr = open(os.path.join(app.DATA, "blyatt.log"), "w", encoding="utf8", buffering=1)
    # el .exe no tiene consola: sin esto cada ffmpeg/node abriria una ventana negra
    _init = subprocess.Popen.__init__

    def _no_window(self, *a, **k):
        k.setdefault("creationflags", 0x08000000)   # CREATE_NO_WINDOW
        _init(self, *a, **k)
    subprocess.Popen.__init__ = _no_window
    import updater
    updater.start(app.VERSION, app.DATA)
    app.UPDATER = updater

    def restart():
        if updater.apply(relaunch=True):   # el instalador espera al mutex y reabre Blyatt al terminar
            _APP_CLOSING.set()
            try:
                main_win.destroy()
            except Exception:
                os._exit(0)
    app.UPDATE_RESTART = restart
    return updater


if __name__ == "__main__":
    # karaoke: el separador en CPU corre en un proceso aparte (multiprocessing); en el .exe ese proceso
    # arranca este mismo programa y aqui se desvia a su tarea (antes del mutex de instancia unica)
    import multiprocessing
    multiprocessing.freeze_support()
    upd = _frozen_setup() if app.FROZEN else None
    # Dispositivos: al transferir la musica a esta PC desde el movil, tiene que poder sonar sin un clic
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--autoplay-policy=no-user-gesture-required")
    app.CONNECTLOGIN = connect_login
    app.WEBLOGIN = google_login
    app.WEBLOGOUT = google_logout
    app.SPOTLOGIN = spotify_login
    app.WINCTL = win_cmd
    app.AUTOSTART = autostart
    _nonclient_regions()
    try:
        httpd = serve(PORT)
    except OSError:
        _msgbox("No se pudo abrir el puerto %d: ¿hay otra copia de Blyatt abierta?" % PORT)
        os._exit(1)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    main_win = webview.create_window("Blyatt", f"http://127.0.0.1:{PORT}",
                                     width=1200, height=800, min_size=(900, 600))
    # al cerrar la ventana principal, avisa a los polls de login para que cierren sus ventanas
    # (si no, una ventana de login abierta mantiene webview.start() vivo = proceso zombie)
    main_win.events.closing += lambda: _APP_CLOSING.set()
    webview.start(private_mode=False, storage_path=os.path.join(app.DATA, "auth", "webview"),
                  icon=os.path.join(app.BASE, "assets", "blyatt.ico"))   # ventana y barra de tareas
    _APP_CLOSING.set()
    if upd:
        upd.on_exit()   # version nueva descargada y sin reiniciar: se instala en silencio al cerrar
    httpd.shutdown()
    os._exit(0)   # garantiza que ningun hilo/ventana rezagado deje el proceso vivo
