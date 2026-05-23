"""Foreground service for Ownly Audio Pocket.

Runs in a separate Android process. Its sole job: call startForeground()
with a persistent notification so that Android keeps the ENTIRE app
package (including the activity with ExoPlayer) alive and un-throttled
while music is playing.
"""
import time
from jnius import autoclass  # type: ignore

PythonService = autoclass('org.kivy.android.PythonService')
Build         = autoclass('android.os.Build')
svc = PythonService.mService

CHANNEL_ID = 'ownly_audio'
NOTIF_ID   = 1

# Create notification channel (required API 26+)
if Build.VERSION.SDK_INT >= 26:
    NotifChannel = autoclass('android.app.NotificationChannel')
    NotifManager = autoclass('android.app.NotificationManager')
    nm = svc.getSystemService(svc.NOTIFICATION_SERVICE)
    ch = NotifChannel(CHANNEL_ID, 'Ownly Audio', NotifManager.IMPORTANCE_LOW)
    ch.setShowBadge(False)
    nm.createNotificationChannel(ch)

# Resolve a small icon — app icon if available, otherwise a built-in system icon
try:
    _icon_id = svc.getApplicationInfo().icon
    if _icon_id == 0:
        raise ValueError('no icon')
except Exception:
    # android.R.drawable.ic_dialog_info is available since API 1
    _icon_id = autoclass('android.R$drawable').ic_dialog_info

# Build minimal foreground notification and call startForeground immediately.
# startForeground MUST be called within 5 seconds or Android kills the whole app.
try:
    if Build.VERSION.SDK_INT >= 26:
        builder = autoclass('android.app.Notification$Builder')(svc, CHANNEL_ID)
    else:
        builder = autoclass('android.app.Notification$Builder')(svc)
    builder.setContentTitle('Ownly Audio')
    builder.setContentText('Wiedergabe läuft …')
    builder.setSmallIcon(_icon_id)
    builder.setOngoing(True)
    notif = builder.build()
except Exception:
    # Last resort: use NotificationCompat via raw reflection — just get any Notification
    Notification = autoclass('android.app.Notification')
    notif = Notification()

svc.startForeground(NOTIF_ID, notif)

# Keep the service (and thus the process) alive until stopped externally
while True:
    time.sleep(5)
