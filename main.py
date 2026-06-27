#!/usr/bin/env python3
"""Blyatt - app de escritorio. Sirve la web local y la abre en una ventana nativa (pywebview)."""
import threading
import webview
from app import serve

PORT = 8000

if __name__ == "__main__":
    httpd = serve(PORT)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    webview.create_window("Blyatt", f"http://127.0.0.1:{PORT}",
                          width=1200, height=800, min_size=(900, 600))
    webview.start()
    httpd.shutdown()
