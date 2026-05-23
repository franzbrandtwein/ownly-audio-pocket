[app]
title = Ownly Audio Pocket
package.name = ownlyaudiopocket
package.domain = de.ownly
version = 1.0.0

source.dir = .
source.include_exts = py,png
source.exclude_dirs = music,dist,.certs,.github,__pycache__

icon.filename = %(source.dir)s/icon.png

# ── Python / Kivy ──────────────────────────────────────────────────────────
# p4a v2024.01.21 uses Python 3.11.5 and NDK r25b (stable, avoids 3.14 ABI)
requirements = python3==3.11.5,kivy==2.3.0,android,jnius

# ZXing barcode scanner bundled directly in APK (no opencv needed on Android)
# v3.6.0: CaptureActivity extends Activity (not AppCompatActivity) → Kivy-compatible
# v4.x broke this by requiring AppCompat theme context → crashes on launch
android.gradle_dependencies = com.journeyapps:zxing-android-embedded:3.6.0, androidx.media3:media3-exoplayer:1.3.1

p4a.branch = v2024.01.21

# ── Android ───────────────────────────────────────────────────────────────
android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,CAMERA,WAKE_LOCK,READ_EXTERNAL_STORAGE
android.api = 35
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False
android.enable_androidx = True
android.release_artifact = apk
android.debug = False

# ── UI ────────────────────────────────────────────────────────────────────
orientation = portrait
fullscreen = 0

[buildozer]
log_level = 2
warn_on_root = 0
