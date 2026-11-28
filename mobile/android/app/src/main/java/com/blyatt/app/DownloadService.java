package com.blyatt.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

/**
 * Servicio en primer plano (tipo dataSync) mientras hay descargas del modo offline en marcha (o se esta liberando
 * espacio). Las descargas viven en la WebView: sin este servicio, al salir de la app o apagar la pantalla Android
 * congela el proceso y corta la red, y la cola se para o falla. Muestra el progreso en una notificacion y mantiene
 * CPU y Wi-Fi despiertos solo mientras dura.
 */
public class DownloadService extends Service {
    static final int NOTIF_ID = 8;
    static final String CHANNEL = "wave_downloads";
    static volatile DownloadService inst;
    static volatile Notification pending;
    static volatile boolean wanted;

    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;

    static Notification build(Context c, String title, String text, int done, int total) {
        NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm != null && nm.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Descargas", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
        Intent open = new Intent(c, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(c, 8, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        NotificationCompat.Builder b = new NotificationCompat.Builder(c, CHANNEL)
            .setSmallIcon(R.drawable.ic_np_note)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setSilent(true)
            .setShowWhen(false)
            .setCategory(NotificationCompat.CATEGORY_PROGRESS)
            .setContentIntent(pi);
        if (total > 0) b.setProgress(total, Math.min(done, total), false);
        else b.setProgress(0, 0, true);
        return b.build();
    }

    /** Arranca el servicio o actualiza su notificacion. */
    static void update(Context c, String title, String text, int done, int total) {
        Notification n = build(c, title, text, done, total);
        pending = n;
        wanted = true;
        DownloadService s = inst;
        if (s == null) {
            try {
                ContextCompat.startForegroundService(c, new Intent(c, DownloadService.class));
            } catch (Exception ignored) {
                // Android 12+: no se puede arrancar desde segundo plano. Se reintenta en la siguiente actualizacion
            }
        } else {
            NotificationManager nm = (NotificationManager) c.getSystemService(Context.NOTIFICATION_SERVICE);
            try { if (nm != null) nm.notify(NOTIF_ID, n); } catch (Exception ignored) {}
        }
    }

    static void stop() {
        wanted = false;
        pending = null;
        DownloadService s = inst;
        if (s != null) {
            s.stopForeground(true);
            s.stopSelf();
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        inst = this;
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "wave:downloads");
        wake.setReferenceCounted(false);
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        if (wm != null) {
            wifi = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "wave:downloads");
            wifi.setReferenceCounted(false);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // startForegroundService obliga a llamar a startForeground aunque ya no haga falta (si no, la app se cierra)
        Notification n = pending != null ? pending : build(this, "Wave", "", 0, 0);
        try {
            if (Build.VERSION.SDK_INT >= 29) startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            else startForeground(NOTIF_ID, n);
        } catch (Exception e) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (!wanted) {
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        try {
            if (!wake.isHeld()) wake.acquire(6 * 60 * 60 * 1000L);
            if (wifi != null && !wifi.isHeld()) wifi.acquire();
        } catch (Exception ignored) {}
        return START_NOT_STICKY;
    }

    // Android 15: dataSync tiene un tope de horas al dia; al agotarlo hay que parar
    @Override
    public void onTimeout(int startId, int fgsType) {
        stop();
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // la app se cerro desde recientes: la cola (en la WebView) muere con ella y sigue al volver a abrirla
        stop();
    }

    @Override
    public void onDestroy() {
        try {
            if (wake != null && wake.isHeld()) wake.release();
            if (wifi != null && wifi.isHeld()) wifi.release();
        } catch (Exception ignored) {}
        inst = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
