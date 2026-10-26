import SwiftUI

/// ATIP for iOS (2026-10-26): a native shell around ATIP's own mobile screens -- Today (/m), Command
/// Center (/command) and Global & Asia (/global-asia) -- with a native operating-status banner polled from
/// GET /api/status every 60 s (STOPPED 21:00-06:00 IST: "ATIP stopped — resumes 06:00 IST"). The app holds
/// no credentials and places no orders; writes go through the web pages and are refused while ATIP is stopped.
@main
struct ATIPApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}
