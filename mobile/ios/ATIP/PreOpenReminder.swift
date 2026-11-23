import Combine
import Foundation
import UIKit
import UserNotifications

/// The 09:00 IST pre-open checklist reminder (22 Nov 2026), Monday to Friday: five repeating local notifications,
/// one per weekday, at 09:00 in Asia/Kolkata whatever the phone's own time zone.
///
/// Limits, unlike Android (which asks the server at 09:00 and shows the day's bias, regime and expected open):
/// * The words are fixed. iOS cannot fetch the checklist when a local notification fires -- that would need a
///   push service -- so the reminder only says the checklist is there; a tap opens /preopen in the Home tab.
/// * It cannot skip NSE holidays: the weekly triggers repeat every week, holidays included.
@MainActor
final class PreOpenReminder: NSObject, ObservableObject, UNUserNotificationCenterDelegate {
    static let shared = PreOpenReminder()

    /// The full checklist on the server (a web screen).
    nonisolated static let path = "/preopen"
    nonisolated static let title = "ATIP pre-open checklist"
    nonisolated static let body = "09:00 IST: bias, volatility regime and expected open are ready — tap to open"
    /// Stable identifiers (one per weekday): adding them again replaces the ones already set.
    private nonisolated static let idPrefix = "atip.preopen.weekday."

    /// A tapped reminder: the page to open in the Home tab; [id] goes up with each tap, so the view acts once.
    struct Tap: Equatable {
        let path: String
        let id: Int
    }
    @Published private(set) var tapped: Tap?

    /// Called before launch finishes (AppDelegate), so a tap that launched the app still reaches the delegate.
    /// iOS asks for permission only the first time; the five reminders are set again at every launch (same
    /// identifiers), so a new version's words or time apply, and they are in place if notifications are
    /// switched on later in Settings.
    func start() {
        UNUserNotificationCenter.current().delegate = self
        Task {
            _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
            Self.schedule()
        }
    }

    private nonisolated static func schedule() {
        guard let ist = TimeZone(identifier: "Asia/Kolkata") else { return }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = ist
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        content.threadIdentifier = "atip.preopen"
        content.userInfo = ["path": path]
        let center = UNUserNotificationCenter.current()
        for weekday in 2...6 {                      // Calendar weekdays: 1 Sunday, 2 Monday ... 6 Friday, 7 Saturday
            var when = DateComponents()
            when.calendar = calendar
            when.timeZone = ist                     // 09:00 IST, not 09:00 wherever the phone is
            when.weekday = weekday
            when.hour = 9
            when.minute = 0
            let trigger = UNCalendarNotificationTrigger(dateMatching: when, repeats: true)
            center.add(UNNotificationRequest(identifier: idPrefix + String(weekday), content: content,
                                             trigger: trigger))
        }
    }

    /// A tapped reminder (also one that launched the app): its page in the Home tab (ContentView).
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse,
                                            withCompletionHandler completionHandler: @escaping () -> Void) {
        let path = response.notification.request.content.userInfo["path"] as? String
        Task { @MainActor in self.open(path) }
        completionHandler()
    }

    /// The reminder shows as a banner also while ATIP is open.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification,
                                            withCompletionHandler completionHandler:
                                                @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    /// An ATIP path only (e.g. /preopen), never another site.
    private func open(_ path: String?) {
        guard let path, path.hasPrefix("/"), !path.hasPrefix("//") else { return }
        tapped = Tap(path: path, id: (tapped?.id ?? 0) + 1)
    }
}

/// Sets up the pre-open reminder before launch finishes, as UNUserNotificationCenter's delegate must be.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        PreOpenReminder.shared.start()
        return true
    }
}
