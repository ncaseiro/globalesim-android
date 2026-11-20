import UIKit
import WebKit

/// GlobaleSIM store screen for iPhone.
///
/// Loads the store (app.php on globalesim.tel) in a web view and gives the page the same small
/// bridge the Android app has, `window.GlobaleSIM`, so the page needs no iPhone-specific code:
///   - deviceInfo()        → JSON about this phone (built in at page start, so it can stay synchronous)
///   - installToPhone(ac)  → opens Apple's eSIM installer with the plan (iOS 17.4+), else copies the code
///   - copy(text)          → copies text
///   - openExternal(url)   → opens a link in Safari (PayPal, help pages)
/// Calling: iPhones use the store's built-in web phone (WebRTC) while the app is open,
/// so `voice` is false here and the page loads its browser calling engine.
/// Only pages on globalesim.tel may use the bridge; every other link opens in Safari.
final class StoreViewController: UIViewController, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {

    static let storeURL = URL(string: "https://globalesim.tel/app.php")!
    static weak var current: StoreViewController?
    static let storeHost = "globalesim.tel"

    private var web: WKWebView!
    private let progress = UIProgressView(progressViewStyle: .bar)
    private let offline = UIStackView()
    private var progressObs: NSKeyValueObservation?
    private var pending: URL?
    private weak var bridgeUCC: WKUserContentController?

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    override func viewDidLoad() {
        super.viewDidLoad()
        Self.current = self
        view.backgroundColor = UIColor(red: 0x0E / 255, green: 0x1A / 255, blue: 0x2B / 255, alpha: 1)

        let cfg = WKWebViewConfiguration()
        cfg.allowsInlineMediaPlayback = true                       // call audio and ringtone play inside the page
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.applicationNameForUserAgent = "GlobaleSIMApp/1 GlobaleSIMiOS/1"
        let ucc = WKUserContentController()
        ucc.add(WeakHandler(self), name: "gs")
        ucc.addUserScript(WKUserScript(source: Self.bridgeJS(), injectionTime: .atDocumentStart, forMainFrameOnly: true))
        cfg.userContentController = ucc
        bridgeUCC = ucc

        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsBackForwardNavigationGestures = true
        web.scrollView.contentInsetAdjustmentBehavior = .always
        web.isOpaque = false
        web.backgroundColor = view.backgroundColor
        if #available(iOS 16.4, *) { web.isInspectable = false }
        let refresh = UIRefreshControl()
        refresh.addTarget(self, action: #selector(reload(_:)), for: .valueChanged)
        web.scrollView.refreshControl = refresh

        web.translatesAutoresizingMaskIntoConstraints = false
        progress.translatesAutoresizingMaskIntoConstraints = false
        progress.progressTintColor = UIColor(red: 0.96, green: 0.65, blue: 0.14, alpha: 1)
        view.addSubview(web)
        view.addSubview(progress)
        NSLayoutConstraint.activate([
            web.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            web.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            web.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            progress.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            progress.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            progress.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        progressObs = web.observe(\.estimatedProgress) { [weak self] w, _ in
            self?.progress.progress = Float(w.estimatedProgress)
            self?.progress.isHidden = w.estimatedProgress >= 1
        }
        buildOffline()
        web.load(URLRequest(url: pending ?? Self.storeURL))
        pending = nil
    }

    // MARK: deep links
    func open(deepLink url: URL) {
        guard url.scheme == "globalesim" else { return }
        var target = Self.storeURL
        if url.host == "order", let t = url.pathComponents.last, t.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil {
            target = URL(string: "\(Self.storeURL.absoluteString)?order=\(t)")!
        } else if url.host == "phone" {
            // already on the phone page: just sign in again (no reload, so an arriving call isn't lost)
            if let cur = web?.url, Self.isStore(cur), cur.query?.contains("phone") == true { wake(); return }
            target = URL(string: "\(Self.storeURL.absoluteString)?phone")!
        }
        if web == nil { pending = target } else { web.load(URLRequest(url: target)) }
    }

    /// The in-app phone signs in again right away (after the background, or when a call alert arrives).
    func wake() {
        guard let web, Self.isStore(web.url) else { return }
        web.evaluateJavaScript("window.GSWake && window.GSWake()", completionHandler: nil)
    }

    /// Apple gave this iPhone its push address: the phone page sends it to the voice server.
    func pushToken(_ hex: String) {
        guard let web, Self.isStore(web.url), hex.range(of: "^[0-9a-f]{64,200}$", options: .regularExpression) != nil else { return }
        web.evaluateJavaScript("window.GSPushToken='\(hex)';window.dispatchEvent(new CustomEvent('gs-push',{detail:'\(hex)'}))", completionHandler: nil)
    }

    // MARK: the bridge
    private static func bridgeJS() -> String {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        let info: [String: Any] = [
            "esim": true,                 // every iPhone that runs this app (XS and newer) has eSIM
            "ios": "\(v.majorVersion).\(v.minorVersion)",
            "model": UIDevice.current.model,
            "app": 1,
            "platform": "ios",
            "voice": false,               // calls use the page's web phone (WebRTC) while the app is open
            "mic": true,
            "cardManager": false,         // iPhones can't write plans onto a physical SIM card
            "esimLink": v.majorVersion > 17 || (v.majorVersion == 17 && v.minorVersion >= 4),
            "pushToken": UserDefaults.standard.string(forKey: AppDelegate.tokenKey) ?? "",
        ]
        let json = String(data: try! JSONSerialization.data(withJSONObject: info), encoding: .utf8)!
        return """
        (function () {
          if (location.protocol !== 'https:' || !/(^|\\.)globalesim\\.tel$/.test(location.hostname)) return;
          const post = (fn, arg) => window.webkit.messageHandlers.gs.postMessage({ fn: fn, arg: String(arg == null ? '' : arg) });
          window.GlobaleSIM = {
            deviceInfo: () => JSON.stringify(\(json)),
            copy: t => post('copy', t),
            installToPhone: ac => post('installToPhone', ac),
            installToCard: ac => post('installToCard', ac),
            openExternal: u => post('openExternal', u),
          };
        })();
        """
    }

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, Self.isStore(message.frameInfo.request.url ?? web.url),
              let body = message.body as? [String: Any], let fn = body["fn"] as? String else { return }
        let arg = (body["arg"] as? String) ?? ""
        switch fn {
        case "copy":
            UIPasteboard.general.string = String(arg.prefix(500)); toast("Copied")
        case "installToPhone":
            installToPhone(arg)
        case "installToCard":
            toast("iPhones can't write to a GlobaleSIM card. Use an Android phone or our card station.")
        case "openExternal":
            if let u = URL(string: arg), u.scheme == "https" { UIApplication.shared.open(u) }
        default: break
        }
    }

    private func installToPhone(_ ac: String) {
        guard ac.hasPrefix("LPA:1$"), ac.count < 300 else { return toast("Invalid activation code") }
        UIPasteboard.general.string = ac
        // Apple's eSIM install link (iOS 17.4+): opens Settings → Add eSIM with the plan filled in.
        var c = URLComponents(string: "https://esimsetup.apple.com/esim_qrcode_provisioning")!
        c.queryItems = [URLQueryItem(name: "carddata", value: ac)]
        if #available(iOS 17.4, *), let u = c.url {
            UIApplication.shared.open(u) { ok in if !ok { self.manualInstall() } }
        } else {
            manualInstall()
        }
    }

    private func manualInstall() {
        let a = UIAlertController(title: "Install your eSIM",
            message: "Your code is copied.\n\nOpen Settings → Cellular (or Mobile Data) → Add eSIM → Use QR Code → Enter Details Manually, then paste the code.",
            preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Open Settings", style: .default) { _ in
            if let u = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(u) }
        })
        a.addAction(UIAlertAction(title: "OK", style: .cancel))
        present(a, animated: true)
    }

    // MARK: navigation — our site stays in the app, everything else opens in Safari
    private static func isStore(_ u: URL?) -> Bool {
        guard let u, u.scheme == "https", let h = u.host?.lowercased() else { return false }
        return h == storeHost || h == "www.\(storeHost)"
    }

    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let u = a.request.url else { return decisionHandler(.cancel) }
        if u.scheme == "about" || u.scheme == "blob" || u.scheme == "data" { return decisionHandler(.allow) }
        if u.scheme == "globalesim" { open(deepLink: u); return decisionHandler(.cancel) }
        if a.targetFrame?.isMainFrame == false && u.scheme == "https" { return decisionHandler(.allow) }   // embedded frames (e.g. PayPal buttons)
        if Self.isStore(u) { return decisionHandler(.allow) }
        if ["https", "http", "mailto", "tel", "sms"].contains(u.scheme ?? "") { UIApplication.shared.open(u) }
        decisionHandler(.cancel)
    }

    // target=_blank links: open ours here, others in Safari
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let u = a.request.url { if Self.isStore(u) { w.load(a.request) } else if u.scheme == "https" { UIApplication.shared.open(u) } }
        return nil
    }

    // Microphone for calls: only our own pages may ask (iOS still shows its own permission prompt the first time)
    @available(iOS 15.0, *)
    func webView(_ w: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        let ours = origin.protocol == "https" && (origin.host == Self.storeHost || origin.host == "www.\(Self.storeHost)")
        decisionHandler(ours && type == .microphone ? .grant : .deny)
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = UIAlertController(title: nil, message: m, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(a, animated: true)
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = UIAlertController(title: nil, message: m, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        present(a, animated: true)
    }

    func webView(_ w: WKWebView, didStartProvisionalNavigation n: WKNavigation!) {
        if let ucc = bridgeUCC {   // keep the device info current (push address may have arrived since the last page)
            ucc.removeAllUserScripts()
            ucc.addUserScript(WKUserScript(source: Self.bridgeJS(), injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
    }

    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        offline.isHidden = true; web.scrollView.refreshControl?.endRefreshing()
    }
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { failed(e) }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { failed(e) }
    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { w.reload() }

    private func failed(_ e: Error) {
        web.scrollView.refreshControl?.endRefreshing()
        if (e as NSError).code == NSURLErrorCancelled { return }
        offline.isHidden = false
    }

    @objc private func reload(_ s: Any?) {
        offline.isHidden = true
        if web.url == nil { web.load(URLRequest(url: Self.storeURL)) } else { web.reload() }
    }

    private func buildOffline() {
        let t = UILabel(); t.text = "No connection"; t.font = .boldSystemFont(ofSize: 20); t.textColor = .white
        let s = UILabel(); s.text = "Check your internet and try again."; s.textColor = UIColor(white: 1, alpha: 0.7); s.numberOfLines = 0; s.textAlignment = .center
        let b = UIButton(type: .system); b.setTitle("Try again", for: .normal); b.titleLabel?.font = .boldSystemFont(ofSize: 17)
        b.tintColor = UIColor(red: 0.96, green: 0.65, blue: 0.14, alpha: 1)
        b.addTarget(self, action: #selector(reload(_:)), for: .touchUpInside)
        [t, s, b].forEach(offline.addArrangedSubview)
        offline.axis = .vertical; offline.spacing = 10; offline.alignment = .center; offline.isHidden = true
        offline.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(offline)
        NSLayoutConstraint.activate([
            offline.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            offline.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            offline.widthAnchor.constraint(lessThanOrEqualTo: view.widthAnchor, constant: -48),
        ])
    }

    private func toast(_ text: String) {
        let l = PaddedLabel(); l.text = text; l.numberOfLines = 0; l.textAlignment = .center
        l.textColor = .white; l.font = .systemFont(ofSize: 15, weight: .semibold)
        l.backgroundColor = UIColor(white: 0.1, alpha: 0.92); l.layer.cornerRadius = 12; l.clipsToBounds = true
        l.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(l)
        NSLayoutConstraint.activate([
            l.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            l.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -90),
            l.widthAnchor.constraint(lessThanOrEqualTo: view.widthAnchor, constant: -48),
        ])
        UIView.animate(withDuration: 0.3, delay: 2.2, options: []) { l.alpha = 0 } completion: { _ in l.removeFromSuperview() }
    }
}

/// Keeps WKUserContentController from holding the view controller forever.
private final class WeakHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ t: WKScriptMessageHandler) { target = t }
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) { target?.userContentController(c, didReceive: m) }
}

private final class PaddedLabel: UILabel {
    override func drawText(in r: CGRect) { super.drawText(in: r.insetBy(dx: 16, dy: 10)) }
    override var intrinsicContentSize: CGSize { let s = super.intrinsicContentSize; return CGSize(width: s.width + 32, height: s.height + 20) }
}
