# Ownly Audio Pocket — PRD

## Original Problem Statement
> Die android app in diesem repo stoppt das abspielen des Tracks wenn eine andere App in den Vordergrund geholt wird. Behebe den Fehler.

## Architecture After Refactor (Jan 2026)

```
Activity-Prozess (main, Python/Kivy)        Service-Prozess (:audio, Java)
─────────────────────────────────────       ───────────────────────────────
- UI (Track-Liste, Player-Bar, Settings)    - ExoPlayer-Instanz
- Server-Discovery, SOAP-Client             - Player.Listener (state, error,
- Lokaler HTTP-Proxy für Streaming             position-tick)
- _ServicePlayer-Wrapper (cached State)     - MediaSession + Foreground-
                                              Notification (mediaPlayback type)
- Sendet Intents:                           - Eigener PARTIAL_WAKE_LOCK
    PLAY (url, track, handleFocus)          - Eigene Lifecycle, vom Activity-
    PAUSE / RESUME / STOP / SEEK              Prozess entkoppelt
- Empfängt Broadcasts:
    STATE / POSITION / ENDED / ERROR        Wird automatisch durch das erste
                                            PLAY-Intent als ForegroundService
                                            gestartet.
```

## Bug Fix Timeline

### Fix #1 — ExoPlayer Audio Focus
Wechsel `setAudioAttributes(..., handleAudioFocus=False)` damit ExoPlayer nicht
bei jedem Vordergrund-Wechsel pausiert.

### Fix #2 — User-Setting „Bei Anruf/anderer Media-App pausieren"
Toggle in den Einstellungen, `respect_audio_focus`, default OFF.

### Fix #3 — Akku-Optimierungs-Ausnahme
`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` + Button im Settings-Popup +
Auto-Prompt beim ersten Track-Start.

### Fix #4 — ExoPlayer in separate Service-Prozess verschieben (dieser Schritt)
**Symptom nach Fix #1-#3:** Auf Oukitel C59 Pro / Android 15 fror Android
trotz Foreground-Service + Akku-Ausnahme den Python-Prozess nach ~13 s im
Hintergrund ein. ExoPlayer (im selben Prozess) verstummte nach Buffer-Ende.

**Ursache:** Oukitel/Android 15 nutzt die freezer cgroup für jeden Prozess
ohne sichtbare UI. Der Foreground-Service-Typ `mediaPlayback` schützt nur
*den Service-Prozess*, nicht den Activity-Prozess.

**Lösung:** Der Java-Service wird in einen eigenen OS-Prozess
(`android:process=":audio"`) ausgelagert und erhält seine eigene
ExoPlayer-Instanz. Activity und Service kommunizieren ausschließlich über
Intents (rein) und Broadcasts (raus). Selbst wenn der Activity-Prozess
eingefroren wird, läuft der `:audio`-Prozess als Foreground-Service mit
mediaPlayback-Typ unbeeinträchtigt weiter.

**Files Touched:**
- `/app/src/de/ownly/ownlyaudiopocket/ForegroundAudioService.java` — komplett neu (~270 Zeilen): besitzt ExoPlayer, Player.Listener, MediaSession, Notification, Wake-Lock; Intent-API für PLAY/PAUSE/RESUME/STOP/SEEK; Broadcasts für STATE/POSITION/ENDED/ERROR
- `/app/p4a_hook.py` — Manifest-Patch um `android:process=":audio"` erweitert
- `/app/kivy_app.py`:
  - Neue `_ServicePlayer`-Wrapper-Klasse (mimt ExoPlayer-API per Intents/Broadcasts)
  - `_send_audio_intent(action, **extras)` Helper
  - `_register_audio_broadcasts()` + `_on_audio_broadcast()` für State-Empfang
  - `_setup_exoplayer()` von ~150 Zeilen jnius-Code auf ~25 Zeilen Intent-Dispatch reduziert
  - `_start_audio_service()` → No-Op (Service startet sich via PLAY-Intent selbst)
  - `_stop_audio_service()` → sendet STOP-Intent
  - `on_stop()`, `play_idx()`, `_trigger_next_android_bg()` — kein `_old_sound`-Stash mehr nötig
  - Watcher liest State direkt vom Wrapper (kein HandlerThread-Post mehr)

## Limitations / Known Edge Cases
- **Streaming-Tracks (nicht gecacht) im Hintergrund:** Der lokale Proxy
  läuft weiterhin im Activity-Prozess. Wenn der Activity-Prozess eingefroren
  wird WÄHREND ein Track gestreamt wird (Download noch nicht fertig),
  hängt ExoPlayer im Service nach Buffer-Ende. **Workaround:** Auto-Cache
  aktivieren — sobald der Download fertig ist, wäre der nächste Play vom
  lokalen File unabhängig vom Activity-Prozess.
- **Auto-Next bei eingefrorenem Activity-Prozess:** Wenn ein Track endet
  während Activity eingefroren ist, kommt der `ENDED`-Broadcast erst beim
  Auftauen an. Der nächste Track startet dann verspätet.

## Verification Steps
1. APK via GitHub Actions bauen, installieren
2. Beim ersten Track-Start: Akku-Optimierungs-Dialog → Zulassen
3. Gecachten Track abspielen → andere App öffnen → 5+ Min warten → Musik läuft weiter
4. Log: regelmäßige `watcher: alive` + `svc: ...` Broadcasts auch nach Stunden
5. Bei Anruf/anderer Media-App: Verhalten je nach Setting („Bei Anruf pausieren")

## Backlog
- P2: Sleep-Timer-Feature
- P2: Toter Code aufräumen (`_get_exo_listener_class`, `_get_exo_runnable_class`, `_AudioFocusListener`, `_exo_handler`, `_old_sound`, `_mp_listener`)
- P3: Auto-Next-Queue an Service übergeben, damit auch im Freeze-Fenster nahtlos durchgespielt wird
- P3: Lokalen Proxy in Java-Server umschreiben → kompletter Aktivity-Unabhängigkeit auch beim Streaming
