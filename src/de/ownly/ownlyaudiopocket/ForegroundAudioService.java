package de.ownly.ownlyaudiopocket;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

/**
 * Foreground service running in the MAIN process (same as ExoPlayer/activity).
 * Without this, Android kills the main process in the background even though
 * the p4a Python service declares a foreground notification in a separate process.
 */
public class ForegroundAudioService extends Service {
    private static final String CHANNEL_ID = "ownly_audio";
    private static final int NOTIF_ID = 1;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        createChannel();
        Notification notif = buildNotification(
            intent != null ? intent.getStringExtra("track") : null);
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

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            return new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Ownly Audio")
                .setContentText(text)
                .setSmallIcon(icon)
                .setOngoing(true)
                .build();
        } else {
            return new Notification.Builder(this)
                .setContentTitle("Ownly Audio")
                .setContentText(text)
                .setSmallIcon(icon)
                .setOngoing(true)
                .build();
        }
    }
}
