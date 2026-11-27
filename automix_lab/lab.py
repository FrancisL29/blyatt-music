"""Laboratorio de Automix (PC). Ejecutar:  py automix_lab/lab.py   ->  http://localhost:8040

Busca canciones (con el buscador, la descarga, la letra y el genero de Wave), las analiza, propone transiciones
ordenadas por puntuacion y las renderiza a un clip para escucharlas y compararlas. Usa el mismo motor que el
servidor de Wave (paquete automix/); aqui ademas se puede separar la voz (Spleeter) para las plantillas de voz.
"""
import hashlib
import json
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import app                      # noqa: E402  (buscador, audio, letra, genero de Wave)
from automix import analysis as an, mix, planner, service as svc   # noqa: E402

CACHE = os.path.join(HERE, "cache")
svc.configure(app, CACHE)
SPL_DIR = os.path.join(ROOT, "models", "spleeter-2stems")
_spl = None


def _jload(p):
    with open(p, encoding="utf8") as f:
        return json.load(f)


def _spleeter():
    global _spl
    if _spl is None:
        _spl = an.Spleeter(SPL_DIR)
    return _spl


def load(vid, inst=True):
    """(mezcla, instrumental o None)"""
    x = svc.load(vid)
    ip = os.path.join(CACHE, vid + "_inst.npy")
    return x, (np.load(ip).astype(np.float32) if inst and os.path.isfile(ip) else None)


def analyze(vid, title, artist, dur, stems=True):
    sep = (lambda x: _spleeter().instrumental(x)) if stems and os.path.isdir(SPL_DIR) else None
    return svc.analyze(vid, title, artist, dur, sep=sep)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send(200, open(os.path.join(HERE, "lab.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/api/search":
                res = app.search(q.get("q", ""))
                items = []
                for s in (res or {}).get("sections", []):
                    for it in s.get("items") or []:
                        if it.get("id") and it.get("duration") and re.fullmatch(r"[\w-]{11}", it["id"]):
                            items.append({k: it.get(k) for k in ("id", "title", "artist", "duration", "cover", "subtitle")})
                return self._send(200, items[:12])
            if u.path == "/api/analyze":
                dur = sum(int(x) * 60 ** i for i, x in enumerate(reversed((q.get("dur") or "0").split(":"))) if x.isdigit())
                return self._send(200, analyze(q["id"], q.get("title", ""), q.get("artist", ""), dur, q.get("stems", "1") == "1"))
            if u.path == "/api/plan":
                A = _jload(os.path.join(CACHE, q["a"] + ".json"))
                B = _jload(os.path.join(CACHE, q["b"] + ".json"))
                bars = int(q["bars"]) if q.get("bars", "").isdigit() else None
                return self._send(200, planner.plan(A, B, stems=q.get("stems", "1") == "1", bars=bars))
            if u.path.startswith("/clip/") and re.fullmatch(r"/clip/[0-9a-f]{16}\.wav", u.path):
                return self._send(200, open(os.path.join(CACHE, u.path[6:]), "rb").read(), "audio/wav")
            return self._send(404, {"error": "no"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": str(e)[:300]})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if u.path == "/api/render":
                A = _jload(os.path.join(CACHE, d["a"] + ".json"))
                B = _jload(os.path.join(CACHE, d["b"] + ".json"))
                stems = bool(d.get("stems", True))
                xa, ia = load(d["a"], stems)
                xb, ib = load(d["b"], stems)
                t = time.time()
                wav, marks, info = mix.render(A, B, {"A": {"mix": xa, "inst": ia}, "B": {"mix": xb, "inst": ib}}, d["plan"], stems)
                info["renderSecs"] = round(time.time() - t, 2)
                name = hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16] + ".wav"
                open(os.path.join(CACHE, name), "wb").write(wav)
                return self._send(200, {"url": "/clip/" + name, "marks": marks, "info": info})
            return self._send(404, {"error": "no"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": str(e)[:300]})


class _Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"     # en Windows dejaria a dos laboratorios compartir el puerto


def main(port=8040, open_browser=True):
    srv = _Server(("127.0.0.1", port), H)
    print("Laboratorio de Automix: http://localhost:%d" % port, flush=True)
    if open_browser:
        threading.Timer(.8, lambda: webbrowser.open("http://localhost:%d" % port)).start()
    srv.serve_forever()


if __name__ == "__main__":
    pa = [a for a in sys.argv if a.startswith("--port=")]
    main(port=int(pa[0][7:]) if pa else 8040, open_browser="--no-browser" not in sys.argv)
