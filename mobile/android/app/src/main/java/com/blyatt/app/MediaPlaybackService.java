package com.blyatt.app;

import android.app.Notification;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/**
 * Servicio en primer plano (tipo mediaPlayback) mientras hay musica cargada.
 * Sin el, Android (Doze / app standby / freezer de HiOS) corta la red y congela el proceso con
 * la pantalla apagada: las pistas ya precargadas suenan, pero la descarga de la siguiente falla
 * y la reproduccion se detiene. Los locks mantienen CPU y Wi-Fi despiertos SOLO mientras suena.
 */
public class MediaPlaybackService extends Service {
    static final int NOTIF_ID = 7;
    static volatile Notification pending;
    static volatile MediaPlaybackService inst;

    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;

    @Override
    public void onCreate() {
        super.onCreate();
        inst = this;
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "blyatt:playback");
        wake.setReferenceCounted(false);
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            int mode = Build.VERSION.SDK_INT >= 29 ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
            wifi = wm.createWifiLock(mode, "blyatt:playback");
            wifi.setReferenceCounted(false);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification n = pending;
        if (n != null) {
            try {
                if (Build.VERSION.SDK_INT >= 29) {
                    startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
                } else {
                    startForeground(NOTIF_ID, n);
                }
            } catch (Exception e) {
                stopSelf();
            }
        } else {
            stopSelf();
        }
        return START_NOT_STICKY;
    }

    static void setPlaying(boolean playing) {
        MediaPlaybackService s = inst;
        if (s == null) return;
        try {
            if (playing) {
                if (!s.wake.isHeld()) s.wake.acquire(6 * 60 * 60 * 1000L);
                if (s.wifi != null && !s.wifi.isHeld()) s.wifi.acquire();
            } else {
                if (s.wake.isHeld()) s.wake.release();
                if (s.wifi != null && s.wifi.isHeld()) s.wifi.release();
            }
        } catch (Exception ignored) {}
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // el usuario cerro la app desde recientes: la musica (que vive en la WebView) muere con ella
        stopForeground(true);
        stopSelf();
    }

    @Override
    public void onDestroy() {
        setPlaying(false);
        inst = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
