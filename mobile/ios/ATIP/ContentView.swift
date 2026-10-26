import SwiftUI

struct ContentView: View {
    @AppStorage("server") private var server = "http://192.168.1.10:8000"
    @StateObject private var status = StatusModel()
    @State private var editing = false

    var body: some View {
        VStack(spacing: 0) {
            StatusBanner(state: status.state)
                .onLongPressGesture { editing = true }
                .accessibilityHint("Long-press to change the ATIP server address")
            TabView {
                WebScreen(url: url("/m")).tabItem { Label("Today", systemImage: "chart.line.uptrend.xyaxis") }
                WebScreen(url: url("/command")).tabItem { Label("Command", systemImage: "switch.2") }
                WebScreen(url: url("/global-asia")).tabItem { Label("Global & Asia", systemImage: "globe.asia.australia") }
            }
        }
        .task(id: server) { await status.run(server: server) }
        .alert("ATIP server address", isPresented: $editing) {
            TextField("http://192.168.1.10:8000", text: $server)
                .textInputAutocapitalization(.never).keyboardType(.URL)
            Button("OK") {}
        }
    }

    private func url(_ path: String) -> URL? {
        URL(string: server.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")) + path)
    }
}
