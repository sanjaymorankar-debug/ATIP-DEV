import SwiftUI

struct ContentView: View {
    @AppStorage("server") private var server = "http://192.168.1.10:8000"
    @StateObject private var status = StatusModel()
    @State private var editing = false
    @State private var tab = 0
    @State private var home = [0, 0, 0, 0, 0]       // per tab: bumped when the open tab is tapped again

    var body: some View {
        VStack(spacing: 0) {
            StatusBanner(state: status.state)
                .onLongPressGesture { editing = true }
                .accessibilityHint("Long-press to change the ATIP server address")
            // 2026-11-02: the five tabs of the page shell (Home, Trade, Sessions, Reports, More), the same as Android.
            // Sessions shows S1-S10 with gross vs actual P&L (after charges and estimated tax).
            TabView(selection: Binding(get: { tab }, set: { if $0 == tab { home[$0] += 1 }; tab = $0 })) {
                screen(0, "/home").tabItem { Label("Home", systemImage: "house") }.tag(0)
                screen(1, "/command").tabItem { Label("Trade", systemImage: "chart.line.uptrend.xyaxis") }.tag(1)
                screen(2, "/sessions").tabItem { Label("Sessions", systemImage: "square.grid.2x2") }.tag(2)
                screen(3, "/reports").tabItem { Label("Reports", systemImage: "doc.text") }.tag(3)
                screen(4, "/more").tabItem { Label("More", systemImage: "ellipsis.circle") }.tag(4)
            }
        }
        .task(id: server) { await status.run(server: server) }
        .alert("ATIP server address", isPresented: $editing) {
            TextField("http://192.168.1.10:8000", text: $server)
                .textInputAutocapitalization(.never).keyboardType(.URL)
            Button("OK") {}
        }
    }

    private func screen(_ i: Int, _ path: String) -> WebScreen {
        WebScreen(url: URL(string: server.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")) + path), home: home[i])
    }
}
