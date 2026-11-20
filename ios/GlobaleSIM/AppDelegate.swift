import UIKit
import UserNotifications

@main
final class AppDelegate: UIResponder, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    static let tokenKey = "gs.pushToken"

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // "Incoming call" alerts while the app is closed: the voice server sends an Apple push before it rings.
        let nc = UNUserNotificationCenter.current()
        nc.delegate = self
        nc.requestAuthorization(options: [.alert, .sound, .badge]) { ok, _ in
            if ok { DispatchQueue.main.async { application.registerForRemoteNotifications() } }
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        UserDefaults.standard.set(hex, forKey: Self.tokenKey)
        StoreViewController.current?.pushToken(hex)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NSLog("GlobaleSIM: push registration failed: \(error)")
    }

    // A call alert while the app is open: no banner (the in-app phone rings) — just make sure it is signed in right now.
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        if n.request.content.userInfo["call"] != nil {
            StoreViewController.current?.wake()
            done([])
        } else {
            done([.banner, .sound, .list])
        }
    }

    // The person tapped the alert: open the phone, signed in, so the call rings in the app.
    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let u = (r.notification.request.content.userInfo["url"] as? String).flatMap(URL.init(string:)) ?? URL(string: "globalesim://phone")!
        if let vc = StoreViewController.current { vc.open(deepLink: u) } else { SceneDelegate.pendingLink = u }
        done()
    }

    func application(_ application: UIApplication, configurationForConnecting connectingSceneSession: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        UISceneConfiguration(name: "Default", sessionRole: connectingSceneSession.role)
    }
}

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    static var pendingLink: URL?
    var window: UIWindow?
    private var store: StoreViewController?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let ws = scene as? UIWindowScene else { return }
        let vc = StoreViewController()
        store = vc
        let w = UIWindow(windowScene: ws)
        w.rootViewController = vc
        w.makeKeyAndVisible()
        window = w
        if let url = connectionOptions.urlContexts.first?.url ?? Self.pendingLink { vc.open(deepLink: url); Self.pendingLink = nil }
    }

    // globalesim://order/<token>, globalesim://phone — back from PayPal in Safari, or from a call alert
    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        if let url = URLContexts.first?.url { store?.open(deepLink: url) }
    }

    private var wentAway: Date?
    func sceneDidEnterBackground(_ scene: UIScene) { wentAway = Date() }

    // Back from the background: the web phone was paused by iOS — start it fresh so calls have sound.
    func sceneDidBecomeActive(_ scene: UIScene) {
        if let u = Self.pendingLink { store?.open(deepLink: u); Self.pendingLink = nil; wentAway = nil; return }
        let away = wentAway.map { Date().timeIntervalSince($0) } ?? 0
        wentAway = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in self?.store?.resumed(after: away) }
    }
}
