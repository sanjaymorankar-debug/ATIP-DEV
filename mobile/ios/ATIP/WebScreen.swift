import SwiftUI
import WebKit

/// One ATIP web screen in a WKWebView, with pull-to-refresh. Local storage is on: /m keeps its last-good copy.
struct WebScreen: UIViewRepresentable {
    let url: URL?

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        let web = WKWebView(frame: .zero, configuration: config)
        let refresh = UIRefreshControl()
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.reload(_:)), for: .valueChanged)
        web.scrollView.refreshControl = refresh
        web.navigationDelegate = context.coordinator
        context.coordinator.web = web
        if let url { web.load(URLRequest(url: url)) }
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        if let url, web.url?.absoluteString.hasPrefix(url.absoluteString) != true, !web.isLoading {
            web.load(URLRequest(url: url))
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, WKNavigationDelegate {
        weak var web: WKWebView?
        @objc func reload(_ sender: UIRefreshControl) { web?.reload() }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            webView.scrollView.refreshControl?.endRefreshing()
        }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            webView.scrollView.refreshControl?.endRefreshing()
        }
    }
}
