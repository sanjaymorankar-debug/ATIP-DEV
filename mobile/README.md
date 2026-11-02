# ATIP mobile apps (Android + iOS) — 2026-10-26, tabs and banner 2026-11-02

ATIP had no native app before this change (tracker UXR-10: `/m` was a read-only PWA). These are thin native
shells around ATIP's own mobile screens, so every UI change on the server reaches the phone without a store
release.

**Version 2026.11.02** (Android `versionCode` 3 / `versionName` 2026.11.02; iOS `CFBundleShortVersionString`
2026.11.02 / `CFBundleVersion` 3). The native tabs follow the new page shell:

| Tab | Page | What it shows | Android icon | iOS SF Symbol |
|---|---|---|---|---|
| Home | `/home` | Today: is ATIP safe, running, making money | `home` | `house` |
| Trade | `/command` | Command Center (UXR-01): halt switch, risk budget, pending confirmations | `show_chart` | `chart.line.uptrend.xyaxis` |
| Sessions | `/sessions` | The 10 trading sessions S1–S10; each card shows gross P&L vs actual P&L after charges and estimated tax | `dashboard` | `square.grid.2x2` |
| Reports | `/reports` | Periodic reports and the archive | `description` | `doc.text` |
| More | `/more` | Every other section: Markets, Research, Accounting, Strategy Lab, Wealth, Settings | `more_horiz` | `ellipsis.circle` |

They replace the 2026-10-26 / 2026-10-31 tabs Today (`/m`), Live (`/m?tab=live`), Command (`/command`) and
Global & Asia (`/global-asia`). Those URLs still work on the server.
Android icons are Material Icons vector drawables in `app/src/main/res/drawable/ic_tab_*.xml`.

**The page shell hides its own navigation in the apps.** Both web views append `ATIPApp/<version>` (today
`ATIPApp/2026.11.02`) to the platform's default user agent; the shell hides its left rail / bottom tab bar
for that user agent (or `?app=1`), because the apps have native tabs.

**Moving around.** Pull down on any tab (Sessions included) to reload the page on screen. Tap the open tab
again to go back to its own page. Back (Android) or a swipe from the left edge (iOS) goes back to the previous
page inside the tab, for example from a section opened from More.

**Native banner.** Both apps poll `GET /api/v2/shell/state` every 60 s and fall back to `GET /api/status`
when that answers non-200 (a server older than the page shell). Above the page they show the environment badge
and the run state:

- **Environment badge** (a rounded pill, always shows the word): `PAPER` (#0E7490), `SANDBOX` (#6941C6),
  `LIVE` (#B42318, bold), white text. Hidden when the server does not say (older server, or not reachable).
- **Run state**, 13 sp/pt semibold, with a symbol so it never depends on colour alone:
  `● ATIP running · stops 21:00 IST · last updated …` (bg #064E3B, text #D1FAE5),
  `⏸ ATIP stopped — resumes 06:00 IST · read-only, no orders · last updated …` (21:00–06:00 IST or a manual stop;
  bg #7C2D12, text #FFEDD5), `↻ ATIP resuming …` while the 06:00 sequence runs (bg #1E3A8A, text #DBEAFE).
  The words come from the reply's `banner` (same wording as the web banner).
- `Kill switch ON — <reason>` right after the state while `halted` is true; `view only` on the view-only web
  server (`role` VIEWER); `no live feeds today` when `live_feeds` is false; `last updated` only when the reply
  carries `last_updated`.
- Not reachable: `ATIP not reachable … — long-press to change` (amber). Long-press the banner to set the
  server address (default `http://192.168.1.10:8000`).

The apps hold no token and place no orders; any write happens inside the web pages (dashboard token,
refused with HTTP 423 while ATIP is stopped). Signals for the Asian markets are informational only.

## Build

**Android** (`mobile/android`, Kotlin, minSdk 26, targetSdk 35): open in Android Studio (Ladybug or newer) or

    cd mobile/android && gradle wrapper && ./gradlew assembleDebug
    # APK: app/build/outputs/apk/debug/app-debug.apk

**iOS** (`mobile/ios`, SwiftUI, iOS 16+):

    brew install xcodegen
    cd mobile/ios && xcodegen
    xcodebuild -project ATIP.xcodeproj -scheme ATIP -sdk iphonesimulator \
      -destination 'generic/platform=iOS Simulator' build

or open `ATIP.xcodeproj` in Xcode and run on a simulator or a device (a free Apple ID can sideload to your own
phone; the App Store needs an Apple Developer account).

**Not built on the owner's Mac (2026-10-26, 2026-11-02).** It has no Android SDK, Gradle or Java runtime, and
only the Xcode Command Line Tools (no Xcode, no iOS SDK, no xcodegen). Checks run instead on 2026-11-02:
all Swift files pass `swiftc -parse`, and type-check cleanly (Swift 5 and Swift 6 language modes) against the
Mac Catalyst UIKit in the Command Line Tools' macOS SDK; the banner logic was run on macOS against sample
`/api/v2/shell/state` and `/api/status` replies. The Kotlin was reviewed by hand only (no compiler on the Mac).

**Reaching ATIP from the phone.** ATIP listens on the Mac. Either set `dashboard.host` to `0.0.0.0` in
`atip_data/config.json` and use the Mac's LAN address on the same Wi-Fi, or use a hosted Test/Prod server
once one exists (then switch the address to https and set `cleartextTrafficPermitted` to false on Android).
