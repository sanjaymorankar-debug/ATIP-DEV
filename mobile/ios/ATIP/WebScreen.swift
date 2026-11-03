import SwiftUI
import UIKit
import WebKit

/// One ATIP web screen in a WKWebView, with pull-to-refresh. Local storage is on: /m keeps its last-good copy.
/// ATIP's own links (/stock/<SYMBOL>, the chart and market depth, included) open in this tab; a link tapped to
/// another site opens in Safari. alert() and confirm() from the pages show as native alerts.
struct WebScreen: UIViewRepresentable {
    let url: URL?
    /// Bumped when this tab is tapped while it is open: go back to the tab's own page (as on Android).
    var home = 0

    /// Appended to the default user agent (2026-11-02): the page shell hides its own left rail / bottom tab bar
    /// for "ATIPApp/", because the app has native tabs.
    static let userAgentTag = "ATIPApp/" +
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "2026.11.03")

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.applicationNameForUserAgent = [config.applicationNameForUserAgent, Self.userAgentTag]
            .compactMap { $0 }.joined(separator: " ")       // keeps WebKit's own "Mobile/…" part
        let web = WKWebView(frame: .zero, configuration: config)
        web.allowsBackForwardNavigationGestures = true      // swipe back after opening a section from More
        web.isOpaque = false                                // the bg token, not a white flash, before a page paints
        web.backgroundColor = .axBg
        web.scrollView.backgroundColor = .axBg
        let refresh = UIRefreshControl()
        refresh.tintColor = .axBrand
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.reload(_:)), for: .valueChanged)
        web.scrollView.refreshControl = refresh
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
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

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
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

        /// A page on the ATIP server: the host of the address set in the app, or of the page on screen.
        func isAtip(_ link: URL, page: URL?) -> Bool {
            guard let host = link.host?.lowercased() else { return false }
            return host == url?.host?.lowercased() || host == page?.host?.lowercased()
        }

        /// ATIP's own pages -- /stock/<SYMBOL>, a section opened from More -- open in this tab, as do redirects,
        /// frames and script navigations; only a link the user taps to another site opens in Safari.
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard navigationAction.navigationType == .linkActivated,
                  navigationAction.targetFrame?.isMainFrame ?? true,                 // nil: target=_blank
                  let link = navigationAction.request.url,
                  ["http", "https"].contains(link.scheme?.lowercased() ?? ""),
                  !isAtip(link, page: webView.url) else {
                decisionHandler(.allow); return
            }
            UIApplication.shared.open(link)
            decisionHandler(.cancel)
        }

        /// target=_blank or window.open(): an ATIP page opens in this tab (WKWebView would otherwise drop it),
        /// another site in Safari.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let link = navigationAction.request.url {
                if isAtip(link, page: webView.url) { webView.load(navigationAction.request) }
                else { UIApplication.shared.open(link) }
            }
            return nil
        }

        /// alert() from a page.
        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
            if !present(alert, over: webView) { completionHandler() }
        }

        /// confirm() from a page (the order ticket asks before it sends): OK is true, Cancel false.
        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
            if !present(alert, over: webView) { completionHandler(false) }
        }

        /// Shows the alert over whatever is on screen; false when nothing can show it (WebKit must still get an answer).
        private func present(_ alert: UIAlertController, over webView: WKWebView) -> Bool {
            guard var top = webView.window?.rootViewController else { return false }
            while let next = top.presentedViewController { top = next }
            top.present(alert, animated: true)
            return true
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
