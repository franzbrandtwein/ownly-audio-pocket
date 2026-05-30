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
android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,CHANGE_WIFI_STATE,CAMERA,WAKE_LOCK,READ_EXTERNAL_STORAGE,FOREGROUND_SERVICE,FOREGROUND_SERVICE_MEDIA_PLAYBACK,POST_NOTIFICATIONS,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
android.api = 35
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False
android.enable_androidx = True
android.release_artifact = apk
android.debug = False

# Allow cleartext HTTP to 127.0.0.1 so ExoPlayer can connect to the local proxy
android.res_xml = res/xml/network_security_config.xml
android.extra_manifest_application_arguments = manifest_app_attrs.txt

# ForegroundAudioService manifest declaration:
#
# We tried `android.extra_manifest_xml` but it injects content at
# <manifest>-level (next to <application>), which is invalid for
# <service> tags — they MUST live inside <application>. The aapt2
# resource linker rejects it with:
#   "unexpected element <service> found in <manifest>"
#
# Instead we rely on p4a_hook.py which runs in `before_apk_build`
# (i.e. BEFORE gradle assembles the APK) and patches the <service>
# element directly into <application>. The hook is idempotent and
# searches the build tree recursively, so all manifest copies that
# gradle's manifest merger touches get the same service block.
android.add_src = src
p4a.hook = p4a_hook.py

# ── UI ────────────────────────────────────────────────────────────────────
orientation = portrait
fullscreen = 0

[buildozer]
log_level = 2
warn_on_root = 0
