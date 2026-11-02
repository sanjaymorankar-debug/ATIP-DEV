import SwiftUI

/// One status reply (GET /api/v2/shell/state, or GET /api/status on an older server), reduced to what the banner shows.
struct OperatingState: Equatable {
    var status = "UNKNOWN"
    var banner = "Checking ATIP status…"
    var lastUpdated: String? = nil          // nil: the reply has no last_updated, so the banner leaves it out
    var nextStop = ""
    var liveFeeds = true
    var env: String? = nil                  // PAPER / SANDBOX / LIVE; nil when unknown (older server): badge hidden
    var halted = false                      // the kill switch (orders/risk.py)
    var haltReason: String? = nil
    var role = ""                           // "VIEWER" on the view-only web server
    var reachable = true

    /// Environment badge colours (design system): PAPER teal, SANDBOX violet, LIVE red.
    static let envColours: [String: UInt32] = ["PAPER": 0x0E7490, "SANDBOX": 0x6941C6, "LIVE": 0xB42318]

    /// Builds the state from a reply; works for both endpoints (the shell state has env / halted, /api/status may not).
    init(json js: [String: Any]) {
        let env = (js["env"] as? String)?.uppercased() ?? ""
        status = js["status"] as? String ?? "UNKNOWN"
        banner = js["banner"] as? String ?? ""
        lastUpdated = js.keys.contains("last_updated") ? (js["last_updated"] as? String ?? "") : nil
        nextStop = js["next_stop"] as? String ?? ""
        liveFeeds = js["live_feeds"] as? Bool ?? true
        self.env = Self.envColours.keys.contains(env) ? env : nil
        halted = js["halted"] as? Bool ?? false
        haltReason = js["halt_reason"] as? String
        role = js["role"] as? String ?? ""
    }

    init() {}

    /// The banner line, with the same wording as the web banner (dashboard/global_asia.py). The state always
    /// carries a symbol (● running, ⏸ stopped, ↻ resuming) so it never depends on colour alone.
    var text: String {
        if !reachable { return "ATIP not reachable — long-press to change the server" }
        var parts: [String]
        switch status {
        case "STOPPED": parts = ["⏸ \(banner)", "read-only, no orders"]
        case "RESUMING": parts = ["↻ \(banner)"]
        case "RUNNING": parts = ["● \(banner)", "stops \(nextStop.suffix(5)) IST"] + (liveFeeds ? [] : ["no live feeds today"])
        default: parts = [banner.isEmpty ? "ATIP status unknown" : banner]
        }
        if halted { parts.insert("Kill switch ON" + (haltReason.map { $0.isEmpty ? "" : " — \($0)" } ?? ""), at: 1) }
        if role == "VIEWER" { parts.append("view only") }
        if let lastUpdated { parts.append("last updated " + (lastUpdated.isEmpty ? "—" : lastUpdated)) }
        return parts.joined(separator: " · ")
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

    /// The badge is only shown while ATIP answers: an unreachable server's environment is not known.
    var shownEnv: String? { reachable ? env : nil }
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

    /// GET /api/v2/shell/state (2026-11-02); a server older than the page shell answers it non-200, so fall back
    /// to GET /api/status (no env there: the badge stays hidden).
    func poll(server: String) async {
        let base = server.trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        guard let shell = URL(string: base + "/api/v2/shell/state"), let legacy = URL(string: base + "/api/status") else {
            state.reachable = false; return
        }
        do {
            var js = try await fetch(shell)
            if js == nil { js = try await fetch(legacy) }
            if let js { state = OperatingState(json: js) } else { state.reachable = false }
        } catch {
            state.reachable = false
        }
    }

    /// The reply as a JSON object; nil when the server answers non-200 or not JSON. Throws when it cannot be reached.
    private func fetch(_ url: URL) async throws -> [String: Any]? {
        let req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 8)
        let (data, response) = try await URLSession.shared.data(for: req)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}

struct StatusBanner: View {
    let state: OperatingState
    var body: some View {
        let (bg, fg) = state.colours
        HStack(spacing: 8) {
            if let env = state.shownEnv { EnvBadge(env: env) }
            Text(state.text)
                .font(.footnote.weight(.semibold))          // 13 pt at the default text size
                .foregroundStyle(fg)
                .lineLimit(3)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
        .background(bg)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("ATIP status: " + (state.shownEnv.map { "\($0) environment. " } ?? "") + state.text)
    }
}

/// The always-visible environment pill (PAPER / SANDBOX / LIVE): the word is always shown, never colour alone.
struct EnvBadge: View {
    let env: String
    var body: some View {
        Text(env)
            .font(.caption.weight(env == "LIVE" ? .bold : .semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Capsule().fill(Color(hex: OperatingState.envColours[env] ?? 0x334155)))
            .fixedSize()
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}
