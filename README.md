# GlobaleSIM Android app

Customers buy eSIM data and install it:

- **Phone with eSIM** → the app copies the code and opens Android's eSIM screen.
- **Phone without eSIM** → they put a GlobaleSIM card in the SIM slot, and the app writes the plan onto the card.

The app is **EasyEUICC** (the open-source eSIM-card manager from the OpenEUICC project) with the **GlobaleSIM store** as its home screen. The store itself is `app.php` on your server, so prices, plans and design change without a new app release.

```
globalesim-android/
├── overlay/…/StoreActivity.kt   store screen + window.GlobaleSIM bridge
├── scripts/apply_overlay.py     patches upstream EasyEUICC → GlobaleSIM
├── branding/res/                (optional) your icons, e.g. mipmap-xxxhdpi/ic_launcher.png
└── .github/workflows/build.yml  GitHub builds + signs the APK for you
```

## 1. Server: put the store online
1. Upload `app.php` to `globalesim.tel/app.php`. With no keys set it runs in **demo mode** (sample plans, fake payment), so open it in a browser and click through first.
2. Create `app.config.php` next to it:
   ```php
   <?php return [
     'DB_PASS'          => '...',
     'ESIMACCESS_CODE'  => '...',      // live plans
     'PAYPAL_ENV'       => 'sandbox',  // switch to 'live' when ready
     'PAYPAL_CLIENT_ID' => '...',
     'PAYPAL_SECRET'    => '...',
     'MARKUP'           => 1.45,
   ];
   ```
3. In `simwriter.config.php`, add `'ORDERS_TABLE' => 'gs_app_orders'`. Paid card orders from the app then show up in your writing-station queue.

## 2. One-time signing key (keep it forever)
On any computer with Java:
```
keytool -genkeypair -v -keystore globalesim.jks -alias globalesim -keyalg RSA -keysize 2048 -validity 10000
```
Back it up in two places. **If you lose this file you can never update the app, and every card programmed to trust it stops trusting new builds.**

Convert it for GitHub:
- Windows PowerShell: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("globalesim.jks")) | Set-Clipboard`
- Mac/Linux: `base64 -w0 globalesim.jks`

## 3. Build on GitHub (nothing to install)
1. Create a GitHub repo and upload this folder. A **public** repo also covers the license (below).
2. Go to Settings → Secrets and variables → Actions, and add:
   `KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`, `KEY_ALIAS` (`globalesim`), `KEY_PASSWORD`
3. Go to Actions → **Build GlobaleSIM APK** → Run workflow. About 10 minutes later, download **GlobaleSIM-apk** from the run.
   It contains the APK and `cert-fingerprints.txt`.
4. To publish a release, push a tag like `v1.0.0`. The APK is attached to a GitHub Release.

## 4. Test on a phone
Install the APK. Android will ask to allow installing from your browser or files app. Requires Android 11+.
- Store → buy a plan (demo or PayPal sandbox) → **Install on GlobaleSIM card** → the card manager opens → tap **+** → paste the code.
- If the card manager says it can't access the card, the **card's allow-list (ARA-M) doesn't include your app**. See step 5. You can still test your cards with the free EasyEUICC app.

## 5. Cards that trust your app (ARA-M)
Removable eSIM cards only let approved apps write to them. When you order cards from a wholesaler, send them **both lines from `cert-fingerprints.txt`** (SHA-1 and SHA-256) and ask them to add the fingerprints to the card's **ARA-M rules**. Order samples first and test them with the APK.

## License
EasyEUICC/OpenEUICC is GPL. Because the app includes it, you must make your app's source available. This public repo plus the upstream project satisfies that. Your server code (`app.php`) is not part of the app and stays private.

## Google Play (later)
Use the same signing key with Play App Signing. eSIM data is used outside the app, which is why most eSIM apps use their own checkout. Still, read Play's Payments policy before submitting. Also, apps that talk to the SIM need a clear privacy policy page.
