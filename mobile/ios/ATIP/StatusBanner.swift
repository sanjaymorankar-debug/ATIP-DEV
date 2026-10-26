import SwiftUI

/// One /api/status reply, reduced to what the banner shows.
struct OperatingState: Equatable {
    var status = "UNKNOWN"
    var banner = "Checking ATIP status…"
    var lastUpdated = ""
    var nextStop = ""
    var liveFeeds = true
    var reachable = true

    /// The banner line, with the same wording as the web banner (dashboard/global_asia.py).
    var text: String {
        if !reachable { return "ATIP not reachable — long-press to change the server" }
        let lu = " · last updated " + (lastUpdated.isEmpty ? "—" : lastUpdated)
        switch status {
        case "STOPPED": return "⏸ \(banner) · read-only, no orders\(lu)"
        case "RESUMING": return "↻ \(banner)\(lu)"
        case "RUNNING": return "● \(banner) · stops \(nextStop.suffix(5)) IST" + (liveFeeds ? "" : " · no live feeds today") + lu
        default: return banner
        }
    }

    var colours: (Color, Color) {
        if !reachable { return (Color(hex: 0x78350F), Color(hex: 0xFDE68A)) }
        switch status {
        case "STOPPED": return (Color(hex: 0x7C2D12), Color(hex: 0xFFEDD5))
        case "RESUMING": return (Color(hex: 0x1E3A8A), Color(hex: 0xDBEAFE))
        case "RUNNING": return (Color(hex: 0x064E3B), Color(hex: 0xD1FAE5))
        default: return (Color(hex: 0x334155), .white)
        }
    }
}

@MainActor
final class StatusModel: ObservableObject {
    @Published var state = OperatingState()

    func run(server: String) async {
        while !Task.isCancelled {
            await poll(server: server)
            try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
        }
    }

    func poll(server: String) async {
        guard let url = URL(string: server.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")) + "/api/status") else {
            state.reachable = false; return
        }
        do {
            var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8)
            req.httpMethod = "GET"
            let (data, _) = try await URLSession.shared.data(for: req)
            let js = (try JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            state = OperatingState(status: js["status"] as? String ?? "UNKNOWN",
                                   banner: js["banner"] as? String ?? "",
                                   lastUpdated: js["last_updated"] as? String ?? "",
                                   nextStop: js["next_stop"] as? String ?? "",
                                   liveFeeds: js["live_feeds"] as? Bool ?? true, reachable: true)
        } catch {
            state.reachable = false
        }
    }
}

struct StatusBanner: View {
    let state: OperatingState
    var body: some View {
        let (bg, fg) = state.colours
        Text(state.text)
            .font(.footnote.weight(.semibold))
            .foregroundStyle(fg)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .padding(.horizontal, 16)
            .background(bg)
            .accessibilityLabel("ATIP status: \(state.text)")
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}
