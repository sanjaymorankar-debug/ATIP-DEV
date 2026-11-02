import SwiftUI

/// ATIP for iOS (2026-10-26; tabs and banner 2026-11-02): a native shell around ATIP's own mobile screens --
/// Home (/home), Trade (/command), Sessions (/sessions), Reports (/reports) and More (/more) -- with a native
/// operating-status banner and environment badge (PAPER / SANDBOX / LIVE) polled from GET /api/v2/shell/state
/// every 60 s (GET /api/status on an older server; STOPPED 21:00-06:00 IST: "ATIP stopped — resumes 06:00 IST").
/// The web views send "ATIPApp/<version>" in the user agent so the page shell hides its own navigation.
/// The app holds no credentials and places no orders; writes go through the web pages and are refused while
/// ATIP is stopped.
@main
struct ATIPApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}
