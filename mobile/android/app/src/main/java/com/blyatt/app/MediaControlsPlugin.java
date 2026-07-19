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
import android.os.Build;
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
import java.net.HttpURLConnection;
import java.net.URL;

@CapacitorPlugin(
    name = "MediaControls",
    permissions = @Permission(strings = { Manifest.permission.POST_NOTIFICATIONS }, alias = "notifications")
)
public class MediaControlsPlugin extends Plugin {
    private static final String CHANNEL = "blyatt_playback";
    private static final String BTN_ACTION = "com.blyatt.app.MEDIA_BTN";
    private static final int NOTIF_ID = 7;

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
    }

    private void emit(String action, Long posMs) {
        JSObject o = new JSObject();
        o.put("action", action);
        if (posMs != null) o.put("position", posMs / 1000.0);
        notifyListeners("action", o);
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
                liked ? android.R.drawable.btn_star_big_on : android.R.drawable.btn_star_big_off)
            .setState(playing ? PlaybackStateCompat.STATE_PLAYING : PlaybackStateCompat.STATE_PAUSED,
                positionMs, playing ? 1f : 0f);
        session.setPlaybackState(st.build());

        Intent open = new Intent(getContext(), MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPi = PendingIntent.getActivity(getContext(), 0, open,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        NotificationCompat.Builder nb = new NotificationCompat.Builder(getContext(), CHANNEL)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentTitle(title)
            .setContentText(artist)
            .setLargeIcon(cover)
            .setContentIntent(openPi)
            .setOnlyAlertOnce(true)
            .setOngoing(playing)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .addAction(liked ? android.R.drawable.btn_star_big_on : android.R.drawable.btn_star_big_off,
                "Me gusta", btn("like", 4))
            .addAction(android.R.drawable.ic_media_previous, "Anterior", btn("prev", 1))
            .addAction(playing ? android.R.drawable.ic_media_pause : android.R.drawable.ic_media_play,
                playing ? "Pausa" : "Reproducir", btn(playing ? "pause" : "play", 2))
            .addAction(android.R.drawable.ic_media_next, "Siguiente", btn("next", 3))
            .setStyle(new androidx.media.app.NotificationCompat.MediaStyle()
                .setMediaSession(session.getSessionToken())
                .setShowActionsInCompactView(1, 2, 3));
        try {
            nm.notify(NOTIF_ID, nb.build());
        } catch (Exception ignored) {}
    }

    @PluginMethod
    public void hide(PluginCall call) {
        nm.cancel(NOTIF_ID);
        call.resolve();
    }
}
