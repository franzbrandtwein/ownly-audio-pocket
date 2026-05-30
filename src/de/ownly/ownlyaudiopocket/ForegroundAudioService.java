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
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.util.Log;

import androidx.media3.common.AudioAttributes;
import androidx.media3.common.C;
import androidx.media3.common.MediaItem;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.exoplayer.ExoPlayer;

/**
 * Foreground service that OWNS the ExoPlayer instance and runs in its own
 * Android process ("{@code android:process=":audio"}" — see p4a_hook.py).
 *
 * See PRD.md for the rationale.  In-app debug traces are emitted through
 * BROADCAST_DEBUG so they show up in the user-visible debug log; everything
 * also goes to {@code adb logcat -s OwnlyAudio:V}.
 */
public class ForegroundAudioService extends Service {

    private static final String TAG = "OwnlyAudio";

    // ── Intent actions (activity → service) ─────────────────────────────────
    public static final String ACTION_PLAY         = "de.ownly.ownlyaudiopocket.PLAY";
    public static final String ACTION_PAUSE        = "de.ownly.ownlyaudiopocket.PAUSE";
    public static final String ACTION_RESUME       = "de.ownly.ownlyaudiopocket.RESUME";
    public static final String ACTION_STOP         = "de.ownly.ownlyaudiopocket.STOP";
    public static final String ACTION_SEEK         = "de.ownly.ownlyaudiopocket.SEEK";
    public static final String ACTION_HANDLE_FOCUS = "de.ownly.ownlyaudiopocket.HANDLE_FOCUS";

    // ── Broadcasts (service → activity) ─────────────────────────────────────
    public static final String BROADCAST_STATE    = "de.ownly.ownlyaudiopocket.STATE";
    public static final String BROADCAST_POSITION = "de.ownly.ownlyaudiopocket.POSITION";
    public static final String BROADCAST_ENDED    = "de.ownly.ownlyaudiopocket.ENDED";
    public static final String BROADCAST_ERROR    = "de.ownly.ownlyaudiopocket.ERROR";
    public static final String BROADCAST_DEBUG    = "de.ownly.ownlyaudiopocket.DEBUG";

    private static final String CHANNEL_ID = "ownly_audio";
    private static final int    NOTIF_ID   = 1;

    private MediaSession mSession;
    private ExoPlayer    mPlayer;
    private Handler      mPositionHandler;
    private Runnable     mPositionTick;
    private PowerManager.WakeLock mWakeLock;
    private String       mTrackLabel  = "";
    private boolean      mHandleFocus = false;
    private boolean      mIsForeground = false;
    private boolean      mInitFailed   = false;
    private String       mInitError    = null;

    @Override
    public void onCreate() {
        super.onCreate();
        Log.d(TAG, "Service.onCreate() in process " + android.os.Process.myPid());
        emitDebug("onCreate pid=" + android.os.Process.myPid());

        createChannel();

        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            mWakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "OwnlyAudioPocket:AudioService");
            mWakeLock.setReferenceCounted(false);
        } catch (Throwable t) {
            Log.w(TAG, "WakeLock setup failed", t);
            mWakeLock = null;
        }

        // Build ExoPlayer on the service main thread (has its own Looper).
        try {
            AudioAttributes audioAttrs = new AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                .build();

            mPlayer = new ExoPlayer.Builder(this).build();
            mPlayer.setAudioAttributes(audioAttrs, mHandleFocus);
            mPlayer.setHandleAudioBecomingNoisy(false);
            mPlayer.setWakeMode(C.WAKE_MODE_NETWORK);

            mPlayer.addListener(new Player.Listener() {
                @Override
                public void onPlaybackStateChanged(int state) {
                    Intent i = new Intent(BROADCAST_STATE)
                        .setPackage(getPackageName())
                        .putExtra("state", state);
                    sendBroadcast(i);
                    if (state == Player.STATE_ENDED) {
                        sendBroadcast(new Intent(BROADCAST_ENDED).setPackage(getPackageName()));
                    }
                }

                @Override
                public void onIsPlayingChanged(boolean isPlaying) {
                    Intent i = new Intent(BROADCAST_STATE)
                        .setPackage(getPackageName())
                        .putExtra("isPlaying", isPlaying);
                    sendBroadcast(i);
                    if (isPlaying) {
                        acquireWake();
                        startPositionTicks();
                    } else {
                        stopPositionTicks();
                    }
                }

                @Override
                public void onPlayerError(PlaybackException error) {
                    String msg = "ExoPlayer Fehler " + error.errorCode;
                    try { msg += ": " + error.getMessage(); } catch (Throwable ignored) {}
                    Log.e(TAG, "onPlayerError: " + msg, error);
                    Intent i = new Intent(BROADCAST_ERROR)
                        .setPackage(getPackageName())
                        .putExtra("msg", msg);
                    sendBroadcast(i);
                }
            });
            emitDebug("ExoPlayer ready");
        } catch (Throwable t) {
            Log.e(TAG, "ExoPlayer init failed", t);
            mInitFailed = true;
            mInitError = t.getClass().getSimpleName() + ": " + t.getMessage();
            emitDebug("init FAIL " + mInitError);
        }

        // Position broadcaster — emits POSITION every 1s while playing.
        mPositionTick = new Runnable() {
            @Override
            public void run() {
                if (mPlayer != null) {
                    try {
                        long pos = mPlayer.getCurrentPosition();
                        long dur = mPlayer.getDuration();
                        Intent i = new Intent(BROADCAST_POSITION)
                            .setPackage(getPackageName())
                            .putExtra("pos", pos)
                            .putExtra("dur", dur);
                        sendBroadcast(i);
                    } catch (Throwable ignored) {}
                }
                if (mPositionHandler != null) {
                    mPositionHandler.postDelayed(this, 1000);
                }
            }
        };
        mPositionHandler = new Handler(Looper.getMainLooper());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = (intent != null) ? intent.getAction() : null;
        Log.d(TAG, "onStartCommand action=" + action);
        emitDebug("onStartCommand " + (action != null ? action.replace("de.ownly.ownlyaudiopocket.", "") : "null"));

        if (action == null) {
            // Re-create case (START_STICKY after process death) — make sure
            // we re-enter foreground state so we don't get killed by ANR
            // for not calling startForeground() within 5s of being created.
            if (!mIsForeground) startInForeground();
            return START_STICKY;
        }

        // Must call startForeground within 5s of startForegroundService.
        if (!mIsForeground) {
            startInForeground();
        }

        if (mInitFailed) {
            emitDebug("ignoring " + action + " — init failed earlier: " + mInitError);
            return START_STICKY;
        }

        try {
            switch (action) {
                case ACTION_PLAY: {
                    String url   = intent.getStringExtra("url");
                    String track = intent.getStringExtra("track");
                    boolean handleFocus = intent.getBooleanExtra("handleFocus", false);
                    emitDebug("PLAY url=" + (url != null ? url.substring(Math.max(0, url.length() - 40)) : "null"));
                    if (url != null) playUrl(url, track, handleFocus);
                    break;
                }
                case ACTION_PAUSE: {
                    if (mPlayer != null) mPlayer.pause();
                    break;
                }
                case ACTION_RESUME: {
                    if (mPlayer != null) mPlayer.play();
                    break;
                }
                case ACTION_STOP: {
                    if (mPlayer != null) mPlayer.stop();
                    stopPositionTicks();
                    releaseWake();
                    stopForeground(true);
                    mIsForeground = false;
                    stopSelf();
                    return START_NOT_STICKY;
                }
                case ACTION_SEEK: {
                    long pos = intent.getLongExtra("pos", 0L);
                    if (mPlayer != null) mPlayer.seekTo(pos);
                    break;
                }
                case ACTION_HANDLE_FOCUS: {
                    boolean handleFocus = intent.getBooleanExtra("handleFocus", false);
                    applyHandleFocus(handleFocus);
                    break;
                }
            }
        } catch (Throwable t) {
            Log.e(TAG, "onStartCommand action=" + action + " failed", t);
            emitDebug("action " + action + " EXC: " + t.getMessage());
            Intent i = new Intent(BROADCAST_ERROR)
                .setPackage(getPackageName())
                .putExtra("msg", "action " + action + " failed: " + t.getMessage());
            sendBroadcast(i);
        }
        return START_STICKY;
    }

    private void playUrl(String url, String trackLabel, boolean handleFocus) {
        mTrackLabel = (trackLabel != null) ? trackLabel : "";
        applyHandleFocus(handleFocus);

        Uri uri;
        if (url.startsWith("file://")) {
            uri = Uri.parse(url);
        } else if (url.startsWith("/")) {
            uri = Uri.fromFile(new java.io.File(url));
        } else {
            uri = Uri.parse(url);
        }
        emitDebug("playUrl uri=" + uri.toString().substring(Math.max(0, uri.toString().length() - 60)));

        mPlayer.setMediaItem(MediaItem.fromUri(uri));
        mPlayer.prepare();
        mPlayer.play();

        updateSession();
        updateNotification();
        acquireWake();
        startPositionTicks();
        emitDebug("playUrl OK");
    }

    private void applyHandleFocus(boolean handleFocus) {
        mHandleFocus = handleFocus;
        if (mPlayer == null) return;
        try {
            AudioAttributes audioAttrs = new AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC)
                .build();
            mPlayer.setAudioAttributes(audioAttrs, mHandleFocus);
        } catch (Throwable ignored) {}
    }

    private void startInForeground() {
        if (mSession == null) {
            try {
                mSession = new MediaSession(this, "OwnlyAudio");
                mSession.setActive(true);
            } catch (Throwable t) {
                Log.w(TAG, "MediaSession setup failed", t);
            }
        }
        updateSession();
        Notification notif = buildNotification(mTrackLabel);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIF_ID, notif,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
            } else {
                startForeground(NOTIF_ID, notif);
            }
            mIsForeground = true;
            emitDebug("startForeground OK");
        } catch (Throwable t) {
            Log.e(TAG, "startForeground failed", t);
            emitDebug("startForeground FAIL " + t.getMessage());
        }
    }

    private void updateSession() {
        if (mSession == null) return;
        try {
            PlaybackState ps = new PlaybackState.Builder()
                .setState(PlaybackState.STATE_PLAYING,
                          PlaybackState.PLAYBACK_POSITION_UNKNOWN, 1.0f)
                .setActions(PlaybackState.ACTION_PLAY_PAUSE
                          | PlaybackState.ACTION_SKIP_TO_NEXT
                          | PlaybackState.ACTION_SKIP_TO_PREVIOUS)
                .build();
            mSession.setPlaybackState(ps);
            if (mTrackLabel != null && !mTrackLabel.isEmpty()) {
                mSession.setMetadata(new MediaMetadata.Builder()
                    .putString(MediaMetadata.METADATA_KEY_TITLE, mTrackLabel)
                    .build());
            }
        } catch (Throwable ignored) {}
    }

    private void updateNotification() {
        if (!mIsForeground) return;
        try {
            Notification notif = buildNotification(mTrackLabel);
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.notify(NOTIF_ID, notif);
        } catch (Throwable ignored) {}
    }

    private void startPositionTicks() {
        if (mPositionHandler == null || mPositionTick == null) return;
        mPositionHandler.removeCallbacks(mPositionTick);
        mPositionHandler.postDelayed(mPositionTick, 500);
    }

    private void stopPositionTicks() {
        if (mPositionHandler != null && mPositionTick != null) {
            mPositionHandler.removeCallbacks(mPositionTick);
        }
    }

    private void acquireWake() {
        try { if (mWakeLock != null && !mWakeLock.isHeld()) mWakeLock.acquire(); }
        catch (Throwable ignored) {}
    }

    private void releaseWake() {
        try { if (mWakeLock != null && mWakeLock.isHeld()) mWakeLock.release(); }
        catch (Throwable ignored) {}
    }

    private void emitDebug(String msg) {
        try {
            Intent i = new Intent(BROADCAST_DEBUG)
                .setPackage(getPackageName())
                .putExtra("msg", msg);
            sendBroadcast(i);
        } catch (Throwable ignored) {}
    }

    @Override
    public void onDestroy() {
        Log.d(TAG, "Service.onDestroy()");
        stopPositionTicks();
        releaseWake();
        if (mPlayer != null) {
            try { mPlayer.release(); } catch (Throwable ignored) {}
            mPlayer = null;
        }
        if (mSession != null) {
            try {
                mSession.setActive(false);
                mSession.release();
            } catch (Throwable ignored) {}
            mSession = null;
        }
        stopForeground(true);
        mIsForeground = false;
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
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.createNotificationChannel(ch);
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

        if (mSession != null) {
            try {
                builder.setStyle(new Notification.MediaStyle()
                    .setMediaSession(mSession.getSessionToken())
                    .setShowActionsInCompactView());
            } catch (Throwable ignored) {}
        }
        return builder.build();
    }
}
