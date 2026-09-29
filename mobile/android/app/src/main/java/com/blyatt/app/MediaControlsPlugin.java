package com.blyatt.app;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.AudioAttributes;
import android.media.AudioDeviceCallback;
import android.media.AudioFocusRequest;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.support.v4.media.MediaMetadataCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;

import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;

import java.io.InputStream;
import java.util.HashSet;
import java.util.Set;
import java.net.HttpURLConnection;
import java.net.URL;

@CapacitorPlugin(
    name = "MediaControls",
    permissions = @Permission(strings = { Manifest.permission.POST_NOTIFICATIONS }, alias = "notifications")
)
public class MediaControlsPlugin extends Plugin {
    private static final String CHANNEL = "blyatt_playback";
    private static final String BTN_ACTION = "com.blyatt.app.MEDIA_BTN";
    private static final int NOTIF_ID = MediaPlaybackService.NOTIF_ID;

    private MediaSessionCompat session;
    private NotificationManager nm;
    private BroadcastReceiver btnReceiver;
    private Bitmap cover;
    private String coverUrl = "";
    private String title = "", artist = "";
    private boolean playing = false, liked = false;
    private long durationMs = 0, positionMs = 0;

    @Override
    public void load() {
        Context ctx = getContext();
        nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Reproducción", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
        session = new MediaSessionCompat(ctx, "blyatt");
        session.setCallback(new MediaSessionCompat.Callback() {
            @Override public void onPlay() { emit("play", null); }
            @Override public void onPause() { emit("pause", null); }
            @Override public void onSkipToNext() { emit("next", null); }
            @Override public void onSkipToPrevious() { emit("prev", null); }
            @Override public void onSeekTo(long pos) { emit("seek", pos); }
            @Override public void onCustomAction(String action, android.os.Bundle extras) {
                if ("like".equals(action)) emit("like", null);
            }
        });
        session.setActive(true);
        btnReceiver = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) { emit(i.getStringExtra("a"), null); }
        };
        ContextCompat.registerReceiver(ctx, btnReceiver, new IntentFilter(BTN_ACTION), ContextCompat.RECEIVER_NOT_EXPORTED);
        watchBluetooth(ctx);
        audioMgr = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
    }

    // ---- app visible o no (entre onStart y onStop la ventana se ve) ----
    private volatile boolean visible = true;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Runnable rehide = () -> {
        if (!visible) getBridge().getWebView().dispatchWindowVisibilityChanged(View.GONE);
    };

    @Override protected void handleOnStart() { visible = true; main.removeCallbacks(rehide); }
    @Override protected void handleOnStop() { visible = false; }

    /**
     * Chromium CONGELA una pagina oculta a los ~5 min si no esta sonando: con la musica en pausa y la
     * app en segundo plano, el "play" de la notificacion llegaba a la WebView pero el JS no corria hasta
     * abrir la app. Se marca la WebView visible un momento (descongela y procesa la accion); al volver
     * a ocultarla ya suena, y una pagina que suena no se congela.
     */
    private void wakeWebView() {
        if (visible || playing) return;   // sonando no se congela: no hace falta
        main.post(() -> {
            getBridge().getWebView().dispatchWindowVisibilityChanged(View.VISIBLE);
            main.removeCallbacks(rehide);
            main.postDelayed(rehide, 8000);
        });
    }

    private void emit(String action, Long posMs) {
        wakeWebView();
        JSObject o = new JSObject();
        o.put("action", action);
        if (posMs != null) o.put("position", posMs / 1000.0);
        notifyListeners("action", o);
    }

    // ---- foco de audio: bajar la musica mientras otra app habla (audio de WhatsApp), como Spotify ----
    // Sin pedir foco el sistema nunca avisa: la musica seguia a tope encima del audio. Con setWillPauseWhenDucked
    // el sistema NO baja el volumen de golpe: lo hace la pagina con un fundido (setDuck en el JS).
    private AudioManager audioMgr;
    private AudioFocusRequest focusReq;
    private boolean hasFocus = false, ducked = false, pausedByFocus = false;

    private final AudioManager.OnAudioFocusChangeListener focusListener = change -> {
        switch (change) {
            case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK:
            case AudioManager.AUDIOFOCUS_LOSS_TRANSIENT:
                if (inCall()) {   // llamada: con la musica de fondo se oiria en la llamada -> pausa y vuelve al colgar
                    if (playing) { pausedByFocus = true; emit("focuspause", null); }
                } else if (!ducked) {   // audio de WhatsApp, notificacion con voz, navegacion...
                    ducked = true; emit("duck", null);
                }
                break;
            case AudioManager.AUDIOFOCUS_GAIN:
                if (ducked) { ducked = false; emit("unduck", null); }
                if (pausedByFocus) { pausedByFocus = false; emit("focusplay", null); }
                break;
            case AudioManager.AUDIOFOCUS_LOSS:   // otra app de musica/video empezo a sonar: se pausa (como Spotify)
                hasFocus = false;
                if (ducked) { ducked = false; emit("unduck", null); }
                pausedByFocus = false;
                if (playing) emit("focuspause", null);
                break;
            default:
                break;
        }
    };

    private boolean inCall() {
        int m = audioMgr.getMode();
        return m == AudioManager.MODE_IN_CALL || m == AudioManager.MODE_IN_COMMUNICATION || m == AudioManager.MODE_RINGTONE;
    }

    private void requestFocus() {
        if (hasFocus || audioMgr == null) return;
        int r;
        if (Build.VERSION.SDK_INT >= 26) {
            if (focusReq == null) {
                focusReq = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                    .setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                    .setWillPauseWhenDucked(true)
                    .setOnAudioFocusChangeListener(focusListener, main)
                    .build();
            }
            r = audioMgr.requestAudioFocus(focusReq);
        } else {
            r = audioMgr.requestAudioFocus(focusListener, AudioManager.STREAM_MUSIC, AudioManager.AUDIOFOCUS_GAIN);
        }
        hasFocus = r == AudioManager.AUDIOFOCUS_REQUEST_GRANTED;
    }

    private void abandonFocus() {
        if (!hasFocus || audioMgr == null) return;
        if (Build.VERSION.SDK_INT >= 26 && focusReq != null) audioMgr.abandonAudioFocusRequest(focusReq);
        else audioMgr.abandonAudioFocus(focusListener);
        hasFocus = false;
        if (ducked) { ducked = false; emit("unduck", null); }
    }

    // ---- pausa al conectar/desconectar un dispositivo de audio Bluetooth (sin permiso de BT) ----
    private final Set<Integer> btIds = new HashSet<>();

    private static boolean isBt(AudioDeviceInfo d) {
        switch (d.getType()) {
            case AudioDeviceInfo.TYPE_BLUETOOTH_A2DP:
            case AudioDeviceInfo.TYPE_BLUETOOTH_SCO:
            case AudioDeviceInfo.TYPE_HEARING_AID:
            case AudioDeviceInfo.TYPE_BLE_HEADSET:
            case AudioDeviceInfo.TYPE_BLE_SPEAKER:
            case AudioDeviceInfo.TYPE_BLE_BROADCAST:
                return d.isSink();
            default:
                return false;
        }
    }

    private void watchBluetooth(Context ctx) {
        AudioManager am = (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
        if (am == null) return;
        // los ya conectados al abrir no cuentan (el callback los reporta al registrarse)
        for (AudioDeviceInfo d : am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) if (isBt(d)) btIds.add(d.getId());
        am.registerAudioDeviceCallback(new AudioDeviceCallback() {
            @Override public void onAudioDevicesAdded(AudioDeviceInfo[] ds) {
                boolean changed = false;
                for (AudioDeviceInfo d : ds) if (isBt(d) && btIds.add(d.getId())) changed = true;
                if (changed && playing) emit("pause", null);
            }
            @Override public void onAudioDevicesRemoved(AudioDeviceInfo[] ds) {
                boolean changed = false;
                for (AudioDeviceInfo d : ds) if (btIds.remove(d.getId())) changed = true;
                if (changed && playing) emit("pause", null);
            }
        }, main);
    }

    private PendingIntent btn(String action, int req) {
        Intent i = new Intent(BTN_ACTION).setPackage(getContext().getPackageName()).putExtra("a", action);
        return PendingIntent.getBroadcast(getContext(), req, i,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    @PluginMethod
    public void update(PluginCall call) {
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(getContext(),
                Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissionForAlias("notifications", call, "permDone");
            return;
        }
        applyUpdate(call);
    }

    @com.getcapacitor.annotation.PermissionCallback
    private void permDone(PluginCall call) {
        applyUpdate(call);   // con o sin permiso: la MediaSession sigue funcionando (botones BT, pantalla bloqueo)
    }

    private void applyUpdate(PluginCall call) {
        title = call.getString("title", title);
        artist = call.getString("artist", artist);
        playing = Boolean.TRUE.equals(call.getBoolean("playing", playing));
        liked = Boolean.TRUE.equals(call.getBoolean("liked", liked));
        Double dur = call.getDouble("duration");
        Double pos = call.getDouble("position");
        if (dur != null) durationMs = (long) (dur * 1000);
        if (pos != null) positionMs = (long) (pos * 1000);
        String cu = call.getString("cover", "");
        if (cu != null && !cu.equals(coverUrl)) {
            coverUrl = cu;
            cover = null;
            fetchCover(cu);
        }
        render();
        if (playing) requestFocus();   // al empezar a sonar: a partir de aqui el sistema avisa de otros audios
        call.resolve();
    }

    private void fetchCover(final String url) {
        if (url == null || url.isEmpty()) return;
        new Thread(() -> {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(8000); c.setReadTimeout(8000);
                try (InputStream in = c.getInputStream()) {
                    Bitmap b = BitmapFactory.decodeStream(in);
                    if (b != null && url.equals(coverUrl)) { cover = b; render(); }
                }
            } catch (Exception ignored) {}
        }).start();
    }

    private void render() {
        MediaMetadataCompat.Builder md = new MediaMetadataCompat.Builder()
            .putString(MediaMetadataCompat.METADATA_KEY_TITLE, title)
            .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, artist)
            .putLong(MediaMetadataCompat.METADATA_KEY_DURATION, durationMs);
        if (cover != null) md.putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, cover);
        session.setMetadata(md.build());

        PlaybackStateCompat.Builder st = new PlaybackStateCompat.Builder()
            .setActions(PlaybackStateCompat.ACTION_PLAY | PlaybackStateCompat.ACTION_PAUSE
                | PlaybackStateCompat.ACTION_PLAY_PAUSE | PlaybackStateCompat.ACTION_SKIP_TO_NEXT
                | PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS | PlaybackStateCompat.ACTION_SEEK_TO)
            .addCustomAction("like", liked ? "Quitar me gusta" : "Me gusta",
                liked ? R.drawable.ic_np_heart : R.drawable.ic_np_heart_outline)
            .setState(playing ? PlaybackStateCompat.STATE_PLAYING : PlaybackStateCompat.STATE_PAUSED,
                positionMs, playing ? 1f : 0f);
        session.setPlaybackState(st.build());

        Intent open = new Intent(getContext(), MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPi = PendingIntent.getActivity(getContext(), 0, open,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        NotificationCompat.Builder nb = new NotificationCompat.Builder(getContext(), CHANNEL)
            .setSmallIcon(R.drawable.ic_np_note)
            .setContentTitle(title)
            .setContentText(artist)
            .setLargeIcon(cover)
            .setContentIntent(openPi)
            .setOnlyAlertOnce(true)
            .setOngoing(playing)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .addAction(liked ? R.drawable.ic_np_heart : R.drawable.ic_np_heart_outline,
                "Me gusta", btn("like", 4))
            .addAction(R.drawable.ic_np_prev, "Anterior", btn("prev", 1))
            .addAction(playing ? R.drawable.ic_np_pause : R.drawable.ic_np_play,
                playing ? "Pausa" : "Reproducir", btn(playing ? "pause" : "play", 2))
            .addAction(R.drawable.ic_np_next, "Siguiente", btn("next", 3))
            .setStyle(new androidx.media.app.NotificationCompat.MediaStyle()
                .setMediaSession(session.getSessionToken())
                .setShowActionsInCompactView(1, 2, 3));
        Notification n = nb.build();
        // el servicio en primer plano se arranca al primer play (con la app visible: Android 12+
        // prohibe arrancarlo desde segundo plano) y sigue vivo mientras haya pista cargada, pausada
        // incluida, para que reanudar desde la notificacion no necesite re-arrancarlo
        if (MediaPlaybackService.inst == null && playing) {
            MediaPlaybackService.pending = n;
            try {
                ContextCompat.startForegroundService(getContext(),
                    new Intent(getContext(), MediaPlaybackService.class));
            } catch (Exception ignored) {}
        }
        MediaPlaybackService.pending = n;
        try {
            nm.notify(NOTIF_ID, n);
        } catch (Exception ignored) {}
        MediaPlaybackService.setPlaying(playing);
    }

    @PluginMethod
    public void hide(PluginCall call) {
        abandonFocus();
        getContext().stopService(new Intent(getContext(), MediaPlaybackService.class));
        nm.cancel(NOTIF_ID);
        call.resolve();
    }
}
