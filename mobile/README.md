# ATIP mobile apps (Android + iOS) — 2026-10-26, tabs and banner 2026-11-02, UI fixes v2 2026-11-03

ATIP had no native app before this change (tracker UXR-10: `/m` was a read-only PWA). These are thin native
shells around ATIP's own mobile screens, so every UI change on the server reaches the phone without a store
release.

**Version 2026.11.03** (Android `versionCode` 4 / `versionName` 2026.11.03; iOS `CFBundleShortVersionString`
2026.11.03 / `CFBundleVersion` 4). The native tabs follow the page shell:

| Tab | Page | What it shows | Android icon | iOS SF Symbol |
|---|---|---|---|---|
| Home | `/home` | Today: is ATIP safe, running, making money (money labelled Today / Since start) | `home` | `house` |
| Watchlist | `/watchlist` | The symbols you follow; a symbol's chart and market depth (`/stock/<SYMBOL>`) open inside the tab | `list` | `list.bullet.rectangle` |
| Trade | `/command` | Command Center (UXR-01): halt switch, risk budget, pending confirmations, the order ticket | `show_chart` | `chart.line.uptrend.xyaxis` |
| Sessions | `/sessions` | The 10 trading sessions S1–S10; each card shows gross P&L vs actual P&L after charges and estimated tax | `dashboard` | `square.grid.2x2` |
| More | `/more` | Every other section: Markets, Research, Reports, Accounting, Strategy Lab, Wealth, Settings | `more_horiz` | `ellipsis.circle` |

Watchlist replaced the Reports tab on 2026-11-03; Reports is under More. The 2026-10-26 / 2026-10-31 tabs Today
(`/m`), Live (`/m?tab=live`), Command (`/command`) and Global & Asia (`/global-asia`) and the Reports tab's
`/reports` still work on the server. Android icons are Material Icons vector drawables in
`app/src/main/res/drawable/ic_tab_*.xml`.

**The page shell hides its own navigation in the apps.** Both web views append `ATIPApp/<version>` (today
`ATIPApp/2026.11.03`) to the platform's default user agent; the shell hides its left rail / bottom tab bar and its
top-bar run pills for that user agent (or `?app=1`), because the apps have native tabs and a native banner.

**Moving around.** Pull down on any tab (Sessions included) to reload the page on screen. Tap the open tab
again to go back to its own page. Back (Android) or a swipe from the left edge (iOS) goes back to the previous
page inside the tab, for example from a section opened from More. Links to ATIP's own pages, `/stock/<SYMBOL>`
(chart and market depth) included, open inside the current tab, also when the link opens a new window
(`target=_blank`); only a link you tap to another site opens in the phone's browser.

**Light and dark.** The native parts (banner row, tab bar, the background behind the page) use the web tokens of
`dashboard/static/atip/atip.css` and follow the phone's light / dark setting, as the pages do:

| Token | Light | Dark | Used for |
|---|---|---|---|
| `--ax-bg` | #F6F7F9 | #0B1220 | behind the page while it loads |
| `--ax-surface` | #FFFFFF | #111A2C | the compact banner row, the tab bar, behind the status bar |
| `--ax-text` | #101828 | #E6EBF5 | text in native dialogs (Android theme) |
| `--ax-muted` | #5D6B82 | #9AA8C1 | tabs that are not open |
| `--ax-border` | #D0D5DD | #2A3A57 | the lines under the banner row and above the tab bar |
| `--ax-brand` | #1D4ED8 | #60A5FA | the open tab, the pull-to-refresh spinner, dialog buttons |

Android keeps them in `res/values/tokens.xml` and `res/values-night/tokens.xml`; iOS in `ATIP/Tokens.swift`
(dynamic colours). Switching light / dark does not reload the page on screen.

**Native banner.** Both apps poll `GET /api/v2/shell/state` every 60 s and fall back to `GET /api/status`
when that answers non-200 (a server older than the page shell). Since 2026-11-03 (UI fixes v2, H10) the banner is
only loud when something is not normal, the same rule as the web:

- **Normal** (`status` RUNNING, `halted` false, `env` not LIVE, no manual `override`, ATIP answering): a compact
  row on the surface colour with only the environment pill and a small `● Running` pill (bg #064E3B, text
  #D1FAE5; `● Running · view only` on the view-only web server, `role` VIEWER). No full-width coloured strip.
- **Environment pill** (a rounded pill, always shows the word): `PAPER` (#0E7490), `SANDBOX` (#6941C6),
  `LIVE` (#B42318, bold), white text. Hidden when the server does not say (older server, or not reachable).
- **Full-width strip**, 13 sp/pt semibold, with a symbol so it never depends on colour alone. The words come from
  the reply's `banner` (same wording as the web banner):
  - Stopped: `⏸ ATIP stopped — resumes 06:00 IST · read-only, no orders · last updated …` (21:00–06:00 IST or a
    manual stop; bg #7C2D12, text #FFEDD5).
  - Resuming: `↻ ATIP resuming …` while the 06:00 sequence runs (bg #1E3A8A, text #DBEAFE).
  - Kill switch on: `⛔ Trading halted (kill switch on): <reason> · no order of any kind is placed until you
    resume it on Trade › Command Center` (bg #7F1D1D, text #FEE2E2); while stopped or resuming, the stop or resume
    words follow the reason.
  - LIVE environment while running: `● LIVE: orders use real money · ● ATIP running · stops 21:00 IST …`
    (bg #7F1D1D, text #FEE2E2).
  - Manual override while running: `● ATIP running · stops 21:00 IST · manual <mode>` (bg #064E3B, text #D1FAE5).
  - `view only` (`role` VIEWER), `no live feeds today` (`live_feeds` false), `manual <mode>` (`override`) and
    `last updated …` (only when the reply carries `last_updated`) are added to the strip.
  - Not reachable: `ATIP not reachable … — long-press to change` (amber, bg #78350F, text #FDE68A).
- Long-press the banner (the compact row or the strip) to set the server address (default
  `http://192.168.1.10:8000`).

**Order ticket.** On phones the order ticket is a bottom sheet drawn by the web page itself (as are the redesigned
Today cards, the grouped alerts, the watchlist, the pro chart and the market-depth panel). The apps only make room
for it: the keyboard shrinks the page instead of covering the sheet (Android `adjustResize`, and the same by hand
on Android 15, which draws edge to edge; iOS by default), the page's own viewport stops zoom-on-focus jumps, and the
page's `alert()` / `confirm()` show as native dialogs (Android `WebChromeClient`, iOS `WKUIDelegate`).

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

**2026-11-03: not built either.** The Linux box these changes were made on has Gradle 8.14 and Java 21 but no
Android SDK (and no access to Google's Maven repository), no Kotlin compiler, and no Swift or Xcode; iOS cannot be
built on Linux. What was checked is listed under the changes below.

**Reaching ATIP from the phone.** ATIP listens on the Mac. Either set `dashboard.host` to `0.0.0.0` in
`atip_data/config.json` and use the Mac's LAN address on the same Wi-Fi, or use a hosted Test/Prod server
once one exists (then switch the address to https and set `cleartextTrafficPermitted` to false on Android).

## Changes in 2026.11.03 (UI fixes v2, 3 Nov 2026)

**Both apps**

- The banner is only loud when something is not normal (H10): a normal run shows just the environment pill and a
  small `● Running` pill; stopped, resuming, the kill switch, LIVE money, a manual override and not reachable keep
  the full-width strip. The kill-switch and LIVE strips are new (bg #7F1D1D, text #FEE2E2).
- Tabs: Watchlist (`/watchlist`) replaces Reports; the order is Home, Watchlist, Trade, Sessions, More.
- Native colours follow the web tokens, light and dark, with the phone's setting; the open tab is the brand colour.
- ATIP's own links (`/stock/<SYMBOL>` included) open inside the tab; a link tapped to another site opens in the
  phone's browser (before, every link opened inside the app).
- The page's `alert()` and `confirm()` show as native dialogs.
- Version 2026.11.03, user agent `ATIPApp/2026.11.03`.

**Android** (`mobile/android`)

- `MainActivity.kt`: new tabs; compact banner row or full-width strip; token colours, re-applied when light / dark
  switches (no page reload); `shouldOverrideUrlLoading` (ATIP pages inside, other sites to the browser);
  `WebChromeClient` with `onJsAlert` / `onJsConfirm`; the keyboard inset handled on Android 15 (edge to edge).
- `StatusText.kt` (new, moved out of `MainActivity.kt`): the banner's words and colours; no Android classes.
- `AndroidManifest.xml`: `windowSoftInputMode="adjustResize"`.
- `res/drawable/ic_tab_watchlist.xml` (new, Material Icons `list`); `ic_tab_reports.xml` removed.
- `res/values/tokens.xml`, `res/values-night/tokens.xml` (new), `res/color/ax_tab_item.xml` (new),
  `res/values/themes.xml` (DayNight in the tokens), `res/values/strings.xml` (Watchlist in, Reports out).
- `app/build.gradle.kts`: `versionCode` 4, `versionName` 2026.11.03.

**iOS** (`mobile/ios`)

- `ContentView.swift`: new tabs; the tab bar in the tokens (brand for the open tab).
- `StatusBanner.swift`: the normal / not-normal rule, the `override` field, the kill-switch and LIVE strips.
- `Tokens.swift` (new): the web tokens as dynamic light / dark colours.
- `WebScreen.swift`: `WKUIDelegate` (alert, confirm, `target=_blank`); ATIP pages inside, other sites to Safari;
  the bg token behind the page.
- `project.yml`: `CFBundleShortVersionString` 2026.11.03, `CFBundleVersion` 4.

**Checks run (no build).** Android: every XML file is well formed (`xmllint`), every resource the Kotlin, the
theme and the manifest refer to exists, light and dark define the same tokens, and the Kotlin was reviewed by hand
(no Kotlin compiler). iOS: the Swift was reviewed by hand (no Swift compiler on Linux). Build both before release.
