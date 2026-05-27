package de.ownly.ownlyaudiopocket;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.IBinder;

/**
 * Foreground service in the MAIN process to keep it alive during background audio.
 *
 * Android 14+ requires FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK services to have
 * an active MediaSession — without it the OS immediately kills the service.
 */
public class ForegroundAudioService extends Service {
    private static final String CHANNEL_ID = "ownly_audio";
    private static final int    NOTIF_ID   = 1;

    private MediaSession mSession;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String track = (intent != null) ? intent.getStringExtra("track") : null;

        // Create/update MediaSession — required for MEDIA_PLAYBACK type on Android 14+
        if (mSession == null) {
            mSession = new MediaSession(this, "OwnlyAudio");
        }
        PlaybackState ps = new PlaybackState.Builder()
            .setState(PlaybackState.STATE_PLAYING,
                      PlaybackState.PLAYBACK_POSITION_UNKNOWN, 1.0f)
            .setActions(PlaybackState.ACTION_PLAY_PAUSE
                      | PlaybackState.ACTION_SKIP_TO_NEXT
                      | PlaybackState.ACTION_SKIP_TO_PREVIOUS)
            .build();
        mSession.setPlaybackState(ps);
        if (track != null && !track.isEmpty()) {
            mSession.setMetadata(new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, track)
                .build());
        }
        mSession.setActive(true);

        createChannel();
        Notification notif = buildNotification(track);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notif,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        } else {
            startForeground(NOTIF_ID, notif);
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (mSession != null) {
            mSession.setActive(false);
            mSession.release();
            mSession = null;
        }
        stopForeground(true);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "Ownly Audio", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            ch.setSound(null, null);
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE))
                .createNotificationChannel(ch);
        }
    }

    private Notification buildNotification(String track) {
        String text = (track != null && !track.isEmpty()) ? track : "Wiedergabe läuft …";
        int icon = getApplicationInfo().icon;
        if (icon == 0) icon = android.R.drawable.ic_media_play;

        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
        }
        builder.setContentTitle("Ownly Audio")
               .setContentText(text)
               .setSmallIcon(icon)
               .setOngoing(true);

        // MediaStyle links notification to MediaSession → proper lock-screen controls
        if (mSession != null) {
            builder.setStyle(new Notification.MediaStyle()
                .setMediaSession(mSession.getSessionToken())
                .setShowActionsInCompactView());
        }

        return builder.build();
    }
}
