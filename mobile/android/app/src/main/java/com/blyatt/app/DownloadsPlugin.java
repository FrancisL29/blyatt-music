package com.blyatt.app;

import android.app.Activity;
import android.view.WindowManager;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Descargas del modo offline:
 *  - keepScreen({on}): pantalla encendida (la WebView de Android no trae navigator.wakeLock)
 *  - progress({active, title, text, done, total}): servicio en primer plano con el progreso mientras se descarga
 */
@CapacitorPlugin(name = "Downloads")
public class DownloadsPlugin extends Plugin {
    @PluginMethod
    public void keepScreen(PluginCall call) {
        boolean on = Boolean.TRUE.equals(call.getBoolean("on", false));
        Activity a = getActivity();
        if (a != null) {
            a.runOnUiThread(() -> {
                if (on) a.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                else a.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            });
        }
        call.resolve(new JSObject());
    }

    @PluginMethod
    public void progress(PluginCall call) {
        if (!Boolean.TRUE.equals(call.getBoolean("active", false))) {
            DownloadService.stop();
        } else {
            DownloadService.update(getContext(), call.getString("title", "Wave"), call.getString("text", ""),
                call.getInt("done", 0), call.getInt("total", 0));
        }
        call.resolve(new JSObject());
    }
}
