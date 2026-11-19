# GlobaleSIM iPhone app

The same store as the Android app (`app.php` on globalesim.tel) inside an iPhone app.

| | Android app | iPhone app (version 1) |
|---|---|---|
| Buy eSIM plans | ✓ | ✓ |
| Install the eSIM | opens Android's eSIM screen | opens Apple's **Add eSIM** with the plan filled in (iOS 17.4+; older iOS: code copied + steps) |
| Write a plan onto a GlobaleSIM card | ✓ | ✗ (iPhones can't — every iPhone has eSIM anyway) |
| Phone: calls while the app is open | ✓ native | ✓ the store's web phone (WebRTC) |
| Phone: ring when the app is closed | ✓ | later (needs CallKit + VoIP push — version 2) |

```
ios/
├── project.yml                 → turned into the Xcode project by XcodeGen in GitHub Actions
└── GlobaleSIM/
    ├── AppDelegate.swift       app + deep links (globalesim://order/<token>, globalesim://phone)
    ├── StoreViewController.swift  web view + window.GlobaleSIM bridge
    ├── Info.plist              microphone text, URL scheme
    └── Assets.xcassets         icon (1024×1024, from the Play Store icon)
.github/workflows/ios.yml       builds on every push to the `ios` branch
```

No Mac needed: GitHub builds it on a Mac for you.

## Getting it onto your iPhone (TestFlight)

1. **Apple Developer Program** — enroll at developer.apple.com/programs ($99/year).
   Enroll as the **organization First Class Services LLC** so the store shows the company name; Apple asks for the
   company's free **D-U-N-S number** (look it up / request it from the enrollment page — can take a few days).
2. **App Store Connect → Apps → +** → New App: name *GlobaleSIM*, bundle ID **tel.globalesim.app**
   (register it first under Certificates, Identifiers & Profiles → Identifiers), SKU `globalesim-ios`.
3. **App Store Connect → Users and Access → Integrations → App Store Connect API → +**: role *App Manager*.
   Download the `.p8` key (you can only download it once) and note the **Key ID** and **Issuer ID**.
4. In GitHub: **repo → Settings → Secrets and variables → Actions → New repository secret**, add four secrets
   (type them there yourself — never in chat or email):
   - `APPSTORE_KEY_ID` – the Key ID
   - `APPSTORE_ISSUER_ID` – the Issuer ID
   - `APPSTORE_KEY_P8` – the whole text of the `.p8` file
   - `APPLE_TEAM_ID` – Membership details → Team ID
5. Push to the `ios` branch (or Actions → *Build GlobaleSIM iPhone app* → Run). About 15 minutes later the build is in
   App Store Connect → TestFlight. Add yourself as a tester and install it with Apple's **TestFlight** app.

## App Review notes (when you submit to the App Store)
- eSIM plans and the SIM card are used outside the app, so paying with PayPal outside the app is allowed.
- Phone plans (calling minutes) used *inside* the app can be questioned by Apple — keep phone-plan purchase
  on the website for the first submission if the reviewer objects.
- Give the reviewer a demo login and a test eSIM order in the review notes.
