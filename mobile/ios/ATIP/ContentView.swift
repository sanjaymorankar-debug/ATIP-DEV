import SwiftUI
import UIKit

struct ContentView: View {
    @AppStorage("server") private var server = "http://192.168.1.10:8000"
    @StateObject private var status = StatusModel()
    @State private var editing = false
    @State private var tab = 0
    @State private var home = [0, 0, 0, 0, 0]       // per tab: bumped when the open tab is tapped again

    init() {
        // 2026-11-03: the tab bar in the web tokens, as the web tab bar draws it -- surface background, muted items,
        // the brand colour for the open tab; light or dark with the phone's setting (the colours are dynamic)
        let bar = UITabBarAppearance()
        bar.configureWithOpaqueBackground()
        bar.backgroundColor = .axSurface
        bar.shadowColor = .axBorder
        for item in [bar.stackedLayoutAppearance, bar.inlineLayoutAppearance, bar.compactInlineLayoutAppearance] {
            item.normal.iconColor = .axMuted
            item.normal.titleTextAttributes = [.foregroundColor: UIColor.axMuted]
            item.selected.iconColor = .axBrand
            item.selected.titleTextAttributes = [.foregroundColor: UIColor.axBrand]
        }
        UITabBar.appearance().standardAppearance = bar
        UITabBar.appearance().scrollEdgeAppearance = bar
    }

    var body: some View {
        VStack(spacing: 0) {
            StatusBanner(state: status.state)
                .onLongPressGesture { editing = true }
                .accessibilityHint("Long-press to change the ATIP server address")
            // 2026-11-03: the five tabs (Home, Watchlist, Trade, Sessions, More), the same as Android. Watchlist
            // replaces Reports, which stays under More on the web. Sessions shows S1-S10 with gross vs actual P&L
            // (after charges and estimated tax).
            TabView(selection: Binding(get: { tab }, set: { if $0 == tab { home[$0] += 1 }; tab = $0 })) {
                screen(0, "/home").tabItem { Label("Home", systemImage: "house") }.tag(0)
                screen(1, "/watchlist").tabItem { Label("Watchlist", systemImage: "list.bullet.rectangle") }.tag(1)
                screen(2, "/command").tabItem { Label("Trade", systemImage: "chart.line.uptrend.xyaxis") }.tag(2)
                screen(3, "/sessions").tabItem { Label("Sessions", systemImage: "square.grid.2x2") }.tag(3)
                screen(4, "/more").tabItem { Label("More", systemImage: "ellipsis.circle") }.tag(4)
            }
            .tint(Color.axBrand)
        }
        .background(Color.axSurface.ignoresSafeArea())      // behind the status bar, as the compact banner row
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
