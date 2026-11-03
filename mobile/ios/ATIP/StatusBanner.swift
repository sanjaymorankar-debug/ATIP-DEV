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
    var overrideMode: String? = nil         // the manual override's mode (override.mode); nil: no override
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
        if let o = js["override"] as? [String: Any] { overrideMode = o["mode"] as? String ?? "override" }
        role = js["role"] as? String ?? ""
    }

    init() {}

    /// UI fixes v2 (H10, 2026-11-03), as on the web: a normal run -- RUNNING, kill switch off, not LIVE, no manual
    /// override, ATIP answering -- shows only the environment pill and a small "● Running" pill. The full-width
    /// strip is for what is not normal: stopped, resuming, halted, LIVE money, a manual override, not reachable.
    var isNormal: Bool { reachable && status == "RUNNING" && !halted && env != "LIVE" && overrideMode == nil }

    /// The run pill's words when normal, else the strip's, with the same wording as the web banner
    /// (dashboard/global_asia.py). The strip always carries a symbol (● running, ⏸ stopped, ↻ resuming, ⛔ halted)
    /// so it never depends on colour alone.
    var text: String {
        if !reachable { return "ATIP not reachable — long-press to change the server" }
        if isNormal { return "● Running" + (role == "VIEWER" ? " · view only" : "") }
        var state: [String]
        switch status {
        case "STOPPED": state = ["⏸ \(banner)", "read-only, no orders"]
        case "RESUMING": state = ["↻ \(banner)"]
        case "RUNNING": state = ["● \(banner)", "stops \(nextStop.suffix(5)) IST"] + (liveFeeds ? [] : ["no live feeds today"])
        default: state = [banner.isEmpty ? "ATIP status unknown" : banner]
        }
        var parts: [String]
        if halted {
            let reason = haltReason ?? ""
            parts = ["⛔ Trading halted (kill switch on)" + (reason.isEmpty ? "" : ": \(reason)")]
            parts += status == "RUNNING" ? ["no order of any kind is placed until you resume it on Trade › Command Center"] : state
        } else if liveRun {
            parts = ["● LIVE: orders use real money"] + state
        } else {
            parts = state
        }
        if let overrideMode { parts.append("manual \(overrideMode)") }
        if role == "VIEWER" { parts.append("view only") }
        if let lastUpdated { parts.append("last updated " + (lastUpdated.isEmpty ? "—" : lastUpdated)) }
        return parts.joined(separator: " · ")
    }

    private var liveRun: Bool { env == "LIVE" && status == "RUNNING" }

    /// (background, text): the strip's colours, or the run pill's when normal.
    var colours: (Color, Color) {
        if !reachable { return (Color(hex: 0x78350F), Color(hex: 0xFDE68A)) }
        if halted || liveRun { return (Color(hex: 0x7F1D1D), Color(hex: 0xFEE2E2)) }
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
    @Environment(\.pixelLength) private var hairline

    var body: some View {
        let (bg, fg) = state.colours
        Group {
            if state.isNormal {
                // normal (H10): only the pills, on the surface colour, with the web top bar's bottom border
                HStack(spacing: 8) {
                    if let env = state.shownEnv { EnvBadge(env: env) }
                    Pill(text: state.text, fill: bg, ink: fg)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .background(Color.axSurface)
                .overlay(alignment: .bottom) { Rectangle().fill(Color.axBorder).frame(height: hairline) }
            } else {
                // not normal: the full-width strip in the state's colours
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
            }
        }
        .contentShape(Rectangle())                          // the whole row takes the long-press
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("ATIP status: " + (state.shownEnv.map { "\($0) environment. " } ?? "") + state.text)
    }
}

/// The always-visible environment pill (PAPER / SANDBOX / LIVE): the word is always shown, never colour alone.
struct EnvBadge: View {
    let env: String
    var body: some View {
        Pill(text: env, fill: Color(hex: OperatingState.envColours[env] ?? 0x334155), ink: .white, bold: env == "LIVE")
    }
}

/// A small rounded pill: the environment pill, and the normal state's "● Running" (#064E3B / #D1FAE5).
struct Pill: View {
    let text: String
    let fill: Color
    let ink: Color
    var bold = false
    var body: some View {
        Text(text)
            .font(.caption.weight(bold ? .bold : .semibold))
            .foregroundStyle(ink)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Capsule().fill(fill))
            .fixedSize()
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}
