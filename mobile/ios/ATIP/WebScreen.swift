import SwiftUI
import WebKit

/// One ATIP web screen in a WKWebView, with pull-to-refresh. Local storage is on: /m keeps its last-good copy.
struct WebScreen: UIViewRepresentable {
    let url: URL?
    /// Bumped when this tab is tapped while it is open: go back to the tab's own page (as on Android).
    var home = 0

    /// Appended to the default user agent (2026-11-02): the page shell hides its own left rail / bottom tab bar
    /// for "ATIPApp/", because the app has native tabs.
    static let userAgentTag = "ATIPApp/" +
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "2026.11.02")

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.applicationNameForUserAgent = [config.applicationNameForUserAgent, Self.userAgentTag]
            .compactMap { $0 }.joined(separator: " ")       // keeps WebKit's own "Mobile/…" part
        let web = WKWebView(frame: .zero, configuration: config)
        web.allowsBackForwardNavigationGestures = true      // swipe back after opening a section from More
        let refresh = UIRefreshControl()
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.reload(_:)), for: .valueChanged)
        web.scrollView.refreshControl = refresh
        web.navigationDelegate = context.coordinator
        context.coordinator.web = web
        context.coordinator.url = url
        context.coordinator.home = home
        if let url { web.load(URLRequest(url: url)) }
        return web
    }

    /// Loads only when the tab's page changes (a new server address) or the tab is tapped again, so a page opened
    /// inside the tab (More -> Markets) stays put when the banner refreshes.
    func updateUIView(_ web: WKWebView, context: Context) {
        let c = context.coordinator
        guard c.url != url || c.home != home else { return }
        c.url = url
        c.home = home
        if let url { web.load(URLRequest(url: url)) }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, WKNavigationDelegate {
        weak var web: WKWebView?
        var url: URL?
        var home = 0

        /// Pull-to-refresh reloads the page on screen; after a failed first load there is nothing to reload, so
        /// it loads the tab's page again.
        @objc func reload(_ sender: UIRefreshControl) {
            guard let web else { sender.endRefreshing(); return }
            if web.url != nil, web.reload() != nil { return }
            if let url { web.load(URLRequest(url: url)) } else { sender.endRefreshing() }
        }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            webView.scrollView.refreshControl?.endRefreshing()
        }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            webView.scrollView.refreshControl?.endRefreshing()
        }
        // ATIP not reachable: the request never started, so didFail is not called; stop the spinner here too
        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            webView.scrollView.refreshControl?.endRefreshing()
        }
    }
}
