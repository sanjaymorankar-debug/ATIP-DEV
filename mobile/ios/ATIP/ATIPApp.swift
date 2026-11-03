import SwiftUI

/// ATIP for iOS (2026-10-26; tabs and banner 2026-11-02; UI fixes v2 2026-11-03): a native shell around ATIP's own
/// mobile screens -- Home (/home), Watchlist (/watchlist), Trade (/command), Sessions (/sessions) and More (/more) --
/// with a native status banner polled from GET /api/v2/shell/state every 60 s (GET /api/status on an older server).
/// A normal run shows only the environment pill (PAPER / SANDBOX / LIVE) and a small "● Running" pill; stopped
/// (21:00-06:00 IST, "ATIP stopped — resumes 06:00 IST"), resuming, the kill switch, LIVE money, a manual override
/// and ATIP not reachable get the full-width strip. Colours follow the web tokens, light or dark.
/// The web views send "ATIPApp/<version>" in the user agent so the page shell hides its own navigation.
/// The app holds no credentials and places no orders; writes go through the web pages and are refused while
/// ATIP is stopped.
@main
struct ATIPApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}
