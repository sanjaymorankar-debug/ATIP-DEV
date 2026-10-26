# ATIP mobile apps (Android + iOS) — 2026-10-26

ATIP had no native app before this change (tracker UXR-10: `/m` was a read-only PWA). These are thin native
shells around ATIP's own mobile screens, so every UI change on the server reaches the phone without a store
release:

| Tab | Page | What it shows |
|---|---|---|
| Today | `/m` | Market Health, Nifty/VIX, global, buys/sells, Trade of the Day, P&L, pipeline, feeds, alerts |
| Command | `/command` | Command Center (UXR-01): halt switch, risk budget, pending confirmations, 10 sessions |
| Global & Asia | `/global-asia` | Nikkei 225, TOPIX, KOSPI, KOSDAQ, TAIEX, KSE-100: last, % change, signal, session; Asia Sentiment Score; Pakistan risk flag; global markets |

**Native banner.** Both apps poll `GET /api/status` every 60 s and show, above the tabs:
`⏸ ATIP stopped — resumes 06:00 IST · read-only, no orders · last updated …` (21:00–06:00 IST or a manual stop),
`↻ ATIP resuming …` while the 06:00 sequence runs, `● ATIP running · stops 21:00 IST …` otherwise. Same wording
and colours as the web banner. Long-press it to set the server address (default `http://192.168.1.10:8000`).

The apps hold no token and place no orders; any write happens inside the web pages (dashboard token,
refused with HTTP 423 while ATIP is stopped). Signals for the Asian markets are informational only.

## Build

**Android** (`mobile/android`, Kotlin, minSdk 26): open in Android Studio (Ladybug or newer) or
`gradle wrapper && ./gradlew assembleDebug`. Not built on the owner's Mac on 2026-10-26: no Android SDK or
Java runtime is installed there.

**iOS** (`mobile/ios`, SwiftUI, iOS 16+): `brew install xcodegen && cd mobile/ios && xcodegen`, then open
`ATIP.xcodeproj` in Xcode and run on a simulator or a device (a free Apple ID can sideload to your own phone;
the App Store needs an Apple Developer account). Not built on 2026-10-26: only the Command Line Tools are
installed (no Xcode, no iOS SDK); all Swift files pass `swiftc -parse`.

**Reaching ATIP from the phone.** ATIP listens on the Mac. Either set `dashboard.host` to `0.0.0.0` in
`atip_data/config.json` and use the Mac's LAN address on the same Wi-Fi, or use a hosted Test/Prod server
once one exists (then switch the address to https and set `cleartextTrafficPermitted` to false on Android).
