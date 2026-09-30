"""Actualizaciones automaticas de la app de escritorio instalada (.exe) desde las Releases de GitHub.

Flujo: al arrancar (y cada 6 h) consulta la ultima release; si es mas nueva descarga el instalador en
segundo plano y verifica su SHA-256 (el que publica GitHub para el asset). Queda "listo": la UI ofrece
reiniciar ya y, si no, se instala en silencio al cerrar Blyatt (como Discord / VS Code).
Solo corre en la app empaquetada (sys.frozen); desde el codigo fuente o en el server no hace nada.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

REPO = "FrancisL29/blyatt-music"
API = "https://api.github.com/repos/%s/releases/latest" % REPO
ASSET_RE = re.compile(r"^Blyatt-Setup-.*\.exe$", re.I)
EVERY = 6 * 3600

_lock = threading.Lock()
_state = {"version": "", "latest": None, "ready": False, "checking": False, "downloading": False, "error": None,
          "notes": "", "url": ""}
_path = None       # instalador descargado y verificado
_dir = None
_applied = False


def _ver(s):
    n = [int(x) for x in re.findall(r"\d+", s or "")[:3]]
    return tuple(n + [0] * (3 - len(n)))


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Blyatt-Updater",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def status():
    with _lock:
        return dict(_state)


def check_async():
    """Para la UI: arranca la consulta en segundo plano; el progreso se lee con status()."""
    with _lock:
        if _state["checking"]:
            return dict(_state)
        _state["checking"] = True   # ya marcado: el poll de la UI no ve un "terminado" falso
    threading.Thread(target=check, daemon=True).start()
    return status()


def check():
    """Consulta GitHub y descarga la version nueva si la hay. Devuelve el estado."""
    with _lock:
        if _state["downloading"]:
            return dict(_state)
        _state["checking"] = True
        _state["error"] = None
    try:
        return _check()
    finally:
        with _lock:
            _state["checking"] = False


def _check():
    global _path
    try:
        try:
            with _get(API) as r:
                rel = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:   # repo sin releases publicadas: nada que actualizar
                return status()
            raise
        tag = rel.get("tag_name") or ""
        with _lock:
            _state["latest"] = tag.lstrip("v")
            _state["notes"] = (rel.get("body") or "")[:4000]
            _state["url"] = rel.get("html_url") or ""
        if rel.get("draft") or rel.get("prerelease") or _ver(tag) <= _ver(_state["version"]):
            return status()
        asset = next((a for a in rel.get("assets") or [] if ASSET_RE.match(a.get("name") or "")), None)
        if not asset:
            return status()
        dest = os.path.join(_dir, asset["name"])
        if _path == dest and os.path.isfile(dest):
            return status()
        with _lock:
            _state["downloading"] = True
        try:
            _download(asset, dest)
        finally:
            with _lock:
                _state["downloading"] = False
        _path = dest
        with _lock:
            _state["ready"] = True
    except Exception as e:
        with _lock:
            _state["error"] = str(e)[:200]
    return status()


def _download(asset, dest):
    want = (asset.get("digest") or "").lower()   # "sha256:<hex>" (GitHub lo calcula al subir el asset)
    tmp = dest + ".part"
    h = hashlib.sha256()
    size = 0
    with _get(asset["browser_download_url"], timeout=60) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
            h.update(b)
            size += len(b)
    if asset.get("size") and size != asset["size"]:
        os.remove(tmp)
        raise IOError("descarga incompleta")
    if want.startswith("sha256:") and h.hexdigest() != want[7:]:
        os.remove(tmp)
        raise IOError("el instalador descargado no coincide con el de GitHub")
    os.replace(tmp, dest)


def apply(relaunch=True):
    """Lanza el instalador en silencio (espera a que Blyatt cierre y, si relaunch, lo reabre)."""
    global _applied
    if not _path or _applied:
        return False
    _applied = True
    args = [_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
    if relaunch:
        args.append("/RELAUNCH")
    # proceso independiente: sobrevive al cierre de Blyatt (os._exit) y no hereda handles
    subprocess.Popen(args, close_fds=True, creationflags=0x00000008 | 0x00000200)   # DETACHED | NEW_GROUP
    return True


def on_exit():
    # "se actualiza sola": si hay una version descargada y el usuario no reinicio, se instala al cerrar
    if _state["ready"]:
        try:
            apply(relaunch=False)
        except Exception:
            pass


def start(version, data_dir):
    global _dir
    _state["version"] = version
    _dir = os.path.join(data_dir, "updates")
    os.makedirs(_dir, exist_ok=True)
    for n in os.listdir(_dir):   # instaladores de versiones ya instaladas
        try:
            os.remove(os.path.join(_dir, n))
        except OSError:
            pass

    def loop():
        time.sleep(15)   # que el arranque (server, ventana, yt-dlp) no compita con la consulta
        while True:
            check()
            time.sleep(EVERY)
    threading.Thread(target=loop, daemon=True).start()
