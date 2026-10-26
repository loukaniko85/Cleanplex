# Cleanplex Bridge (Android)

An experimental sideloadable Android app that applies Cleanplex segments **without**
Plex's remote-control API. It attaches to Plex's active Android `MediaSession` (the
same mechanism the system media notification uses) and issues seeks or volume mutes.

This exists because Plex removed Companion remote control from Plex for Mobile
2025.10+ and Plex Web never supported it. It does **not** modify the Plex app.

## Requirements

- Android 8.0+ (phone, tablet or Android TV — tested target: Nvidia Shield).
- The Plex app (`com.plexapp.android`) playing on the same device.
- **Notification access** for Cleanplex Bridge (Android requires it for one app to
  read another app's MediaSession).
- Plex must expose the `SEEK_TO` playback action. The app shows
  `Seek supported: true/false`; if false, no seek is possible and the app can only
  report that. (You can check from a PC with
  `adb shell dumpsys media_session | grep -i -A5 plex`.)

## Install

Build the APK (see below) or download the `cleanplex-bridge-debug` artifact from the
**Build Android bridge** workflow run, then:

```bash
adb install -r app-debug.apk
```

On a Shield without a PC, use the Downloader app to fetch the APK from a URL you
control, or push it with `adb connect <shield-ip>:5555`.

## Setup

1. Open **Cleanplex Bridge**.
2. Set the Cleanplex URL (e.g. `http://10.0.24.2:7979`) and press **Start bridge**.
3. Press **Grant notification access** and enable Cleanplex Bridge.
   On Android TV the path is usually **Settings → Apps → Special app access →
   Notification access**.
4. Play a title in the Plex app. The bridge screen shows the Plex title, position,
   whether `SEEK_TO` is supported, how many segments it loaded, and the last action.

The bridge polls every second and refreshes segments from Cleanplex every 5 seconds
(`GET /api/sessions/bridge`). Segment semantics match the server: widen by the pre/post
buffers, trigger inside the look-ahead, seek past the widened end, mute until the end.

## Limitations

- **Technical:** requires `SEEK_TO`. Some Plex builds do not expose it; the app will say
  so. Volume muting uses the device music stream, so it affects the whole device while
  muted (restored afterwards).
- **Matching:** the bridge matches the MediaSession title to Cleanplex's active session
  title. If two sessions play at once, the title disambiguates; otherwise the single
  session is used.
- **Background:** Android may stop the foreground service if battery optimisations are
  aggressive; whitelist the app if needed.
- **Security:** the bridge endpoint is LAN-facing and unauthenticated like the rest of
  the API in default mode. Do not expose Cleanplex to the internet.
- **Legal:** this uses public Android APIs only. It does not decompile, patch or
  redistribute Plex.

## Build

Requires JDK 17 and Android SDK 35:

```bash
cd android-bridge
gradle testDebugUnitTest assembleDebug
# APK: app/build/outputs/apk/debug/app-debug.apk
```

CI (`.github/workflows/android.yml`) runs the unit tests (`SegmentPlanner`) and uploads
the debug APK on every change.
