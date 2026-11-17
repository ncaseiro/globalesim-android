#!/usr/bin/env python3
"""
Turn an upstream OpenEUICC checkout into the GlobaleSIM app.

    python3 scripts/apply_overlay.py <path-to-OpenEUICC> [--app-id tel.globalesim.app]
            [--app-name GlobaleSIM] [--store-url https://globalesim.tel/app.php]

What it does (EasyEUICC = the "app-unpriv" module):
  1. copies overlay/ (StoreActivity.kt) into the checkout
  2. copies branding/res/ (your icons, optional) over app-unpriv/src/main/res
  3. sets applicationId and the app name
  4. makes StoreActivity the launcher + adds the globalesim://order/<token> deep link
  5. re-labels the old EasyEUICC main screen as the "card manager"
     (action tel.globalesim.CARD_MANAGER) so the store can open it

Safe to run once per fresh checkout. Exits non-zero if the upstream layout
changed so the build fails loudly instead of shipping something half-patched.
"""
import argparse, pathlib, re, shutil, sys
from urllib.parse import urlparse

HERE = pathlib.Path(__file__).resolve().parent.parent
CARD_ACTION = "tel.globalesim.CARD_MANAGER"


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("upstream")
    ap.add_argument("--app-id", default="tel.globalesim.app")
    ap.add_argument("--app-name", default="GlobaleSIM")
    ap.add_argument("--store-url", default="https://globalesim.tel/app.php")
    a = ap.parse_args()

    up = pathlib.Path(a.upstream).resolve()
    mod = up / "app-unpriv"
    if not mod.is_dir():
        die(f"{mod} not found — is this an OpenEUICC checkout?")

    # 1. overlay sources
    for src in (HERE / "overlay").rglob("*"):
        if src.is_file():
            dst = up / src.relative_to(HERE / "overlay")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"  + {dst.relative_to(up)}")

    kt = mod / "src/main/java/tel/globalesim/app/StoreActivity.kt"
    host = urlparse(a.store_url).hostname or die("bad --store-url")
    s = kt.read_text()
    s = re.sub(r'const val STORE_URL = "[^"]*"', f'const val STORE_URL = "{a.store_url}"', s)
    s = re.sub(r'const val STORE_HOST = "[^"]*"', f'const val STORE_HOST = "{host}"', s)
    kt.write_text(s)

    # 2. branding (optional): branding/res/mipmap-*/ic_launcher.png etc.
    bres = HERE / "branding/res"
    if bres.is_dir():
        for src in bres.rglob("*"):
            if src.is_file():
                dst = mod / "src/main/res" / src.relative_to(bres)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"  ~ {dst.relative_to(up)}")

    # 3a. applicationId
    gradle = next((p for p in (mod / "build.gradle.kts", mod / "build.gradle") if p.exists()), None) or die("app-unpriv build.gradle not found")
    g = gradle.read_text()
    g2, n = re.subn(r'applicationId(\s*=\s*|\s+)"[^"]+"', lambda m: f'applicationId{m.group(1)}"{a.app_id}"', g)
    if n == 0:
        die(f"applicationId not found in {gradle.name}")
    gradle.write_text(g2)
    print(f"  applicationId -> {a.app_id}")

    # 3b. app name
    named = False
    for strings in (mod / "src/main/res").glob("values*/strings.xml"):
        t = strings.read_text()
        t2, n = re.subn(r'(<string name="app_name"[^>]*>)[^<]*(</string>)', lambda m: m.group(1) + a.app_name + m.group(2), t)
        if n:
            strings.write_text(t2)
            named = True
    if not named:
        print("  WARNING: app_name string not found — launcher label may still say EasyEUICC")

    # 4/5. manifests
    manifests = [mod / "src/main/AndroidManifest.xml", up / "app-common/src/main/AndroidManifest.xml"]
    relabelled = 0
    for mf in manifests:
        if not mf.exists():
            continue
        x = mf.read_text()

        def fix(m):
            block = m.group(0)
            if "android.intent.category.LAUNCHER" not in block:
                return block
            block = block.replace("android.intent.action.MAIN", CARD_ACTION)
            block = block.replace("android.intent.category.LAUNCHER", "android.intent.category.DEFAULT")
            return block

        x2 = re.sub(r"<intent-filter\b.*?</intent-filter>", fix, x, flags=re.S)
        if x2 != x:
            relabelled += x.count("android.intent.category.LAUNCHER")
            mf.write_text(x2)
    if relabelled == 0:
        die("no launcher activity found in the upstream manifests (layout changed?)")
    print(f"  card manager: re-labelled {relabelled} launcher entr{'y' if relabelled == 1 else 'ies'}")

    mf = manifests[0]
    x = mf.read_text()
    if "tel.globalesim.app.StoreActivity" not in x:
        store = """
        <activity
            android:name="tel.globalesim.app.StoreActivity"
            android:exported="true"
            android:launchMode="singleTask"
            android:configChanges="orientation|screenSize|screenLayout|keyboardHidden|uiMode"
            android:theme="@android:style/Theme.Material.NoActionBar">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
            <intent-filter>
                <action android:name="android.intent.action.VIEW" />
                <category android:name="android.intent.category.DEFAULT" />
                <category android:name="android.intent.category.BROWSABLE" />
                <data android:scheme="globalesim" android:host="order" />
            </intent-filter>
        </activity>
"""
        if "</application>" not in x:
            die("no <application> element in app-unpriv manifest")
        x = x.replace("</application>", store + "\n    </application>", 1)
        if "android.permission.INTERNET" not in x:
            x = x.replace("<application", '<uses-permission android:name="android.permission.INTERNET" />\n\n    <application', 1)
        mf.write_text(x)
    print("  StoreActivity is the launcher (deep link globalesim://order/<token>)")

    # 6. Brand GlobaleSIM plans on the card: profiles from ESIMAccess are named
    #    "ESIM_ACCESS_CARRIER" with provider "Carrier". Show "GlobaleSIM" instead
    #    (a nickname the customer sets still wins).
    lpa = up / "app-common/src/main/java/im/angry/openeuicc/util/LPAUtils.kt"
    t = lpa.read_text()
    old = "val LocalProfileInfo.displayName: String\n    get() = nickName.ifEmpty { name }"
    if old not in t:
        die("LPAUtils.displayName changed upstream")
    t = t.replace(old, '''private val GS_PROFILE = Regex("esim[ _]?access", RegexOption.IGNORE_CASE)

val LocalProfileInfo.isGlobaleSim: Boolean
    get() = GS_PROFILE.containsMatchIn(name) || GS_PROFILE.containsMatchIn(providerName)

val LocalProfileInfo.displayName: String
    get() = nickName.ifEmpty { if (isGlobaleSim) "GlobaleSIM" else name }

val LocalProfileInfo.displayProvider: String
    get() = if (isGlobaleSim || (providerName.equals("Carrier", true) && GS_PROFILE.containsMatchIn(name))) "GlobaleSIM" else providerName''')
    lpa.write_text(t)
    frag = up / "app-common/src/main/java/im/angry/openeuicc/ui/EuiccManagementFragment.kt"
    f = frag.read_text()
    if "provider.text = profile.providerName" not in f:
        die("EuiccManagementFragment provider line changed upstream")
    frag.write_text(f.replace("provider.text = profile.providerName", "provider.text = profile.displayProvider"))
    print("  GlobaleSIM plans show as 'GlobaleSIM'")

    # 8. Google Play: every upload needs a higher versionCode. Upstream counts its own git
    #    commits, which doesn't change when only our overlay changes, so the build passes
    #    GS_VERSION_CODE / GS_VERSION_NAME (from the CI run number). Also point the app's
    #    web association at our domain instead of easyeuicc.org.
    bg = mod / "build.gradle.kts"
    g = bg.read_text()
    if "targetSdk = " not in g:
        die("app-unpriv build.gradle.kts changed upstream (targetSdk)")
    if "GS_VERSION_CODE" not in g:
        g = re.sub(r"(\n(\s*)targetSdk = \d+\n)",
                   r'\1\2System.getenv("GS_VERSION_CODE")?.toIntOrNull()?.let { versionCode = it }\n'
                   r'\2System.getenv("GS_VERSION_NAME")?.takeIf { it.isNotBlank() }?.let { versionName = it }\n',
                   g, count=1)
    g = re.sub(r'emitAssetStatements\([^)]*\)', 'emitAssetStatements("https://globalesim.tel")', g)
    bg.write_text(g)
    print("  Play versioning from GS_VERSION_CODE")

    # 9. Keep GlobaleSIM colors: upstream uses Material You (wallpaper colors) on
    #    Android 12+, which replaces our navy/amber theme. Turn it off.
    app_kt = up / "app-common/src/main/java/im/angry/openeuicc/OpenEuiccApplication.kt"
    k = app_kt.read_text()
    if "DynamicColors.applyToActivitiesIfAvailable(this)" in k:
        app_kt.write_text(k.replace("DynamicColors.applyToActivitiesIfAvailable(this)",
                                    "// GlobaleSIM: brand colors, no wallpaper colors"))
    dlg = up / "app-common/src/main/java/im/angry/openeuicc/ui/BaseMaterialDialogFragment.kt"
    d = dlg.read_text()
    d = d.replace("DynamicColors.wrapContextIfAvailable(wrappedContext)", "wrappedContext")
    dlg.write_text(d)
    print("  Material You off (GlobaleSIM colors everywhere)")

    # 10. Google Play: native libs must support 16 KB memory pages. Upstream puts the
    #     max-page-size flag before $(CLEAR_VARS) in lpac-jni.mk, which wipes it, so
    #     liblpac-jni.so ships 4 KB aligned. Set it for every module in Application.mk.
    appmk = up / "libs/lpac-jni/src/main/jni/Application.mk"
    m = appmk.read_text()
    if "max-page-size=16384" not in m:
        if "APP_LDFLAGS :=" in m:
            m = m.replace("APP_LDFLAGS :=", "APP_LDFLAGS := -Wl,-z,max-page-size=16384 -Wl,-z,common-page-size=16384", 1)
        else:
            m += "\nAPP_LDFLAGS += -Wl,-z,max-page-size=16384 -Wl,-z,common-page-size=16384\n"
        appmk.write_text(m)
    print("  16 KB page size for native libs")

    # 11. Google Play: turn on R8 code optimization/obfuscation for the release build
    #     (Play flags apps under 25% obfuscation). Keep everything the native code and
    #     the WebView look up by name.
    g = bg.read_text()
    rel = re.search(r"release\s*\{[^}]*?isMinifyEnabled\s*=\s*false", g, re.S)
    if rel:
        g = g[:rel.start()] + rel.group(0).replace("isMinifyEnabled = false", "isMinifyEnabled = true\n            isShrinkResources = true") + g[rel.end():]
        bg.write_text(g)
    rules = mod / "proguard-rules.pro"
    extra = """
# ---- GlobaleSIM (added by apply_overlay.py) ----
# lpac native code finds these classes, methods and fields by name (JNI)
-keep class net.typeblog.lpac_jni.** { *; }
-keepclasseswithmembernames,includedescriptorclasses class * { native <methods>; }
# Store page bridge: window.GlobaleSIM.*
-keepclassmembers class * { @android.webkit.JavascriptInterface <methods>; }
-keep class tel.globalesim.app.StoreActivity$Bridge { *; }
-keepattributes *Annotation*,Signature,InnerClasses,EnclosingMethod,SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
-dontwarn **
"""
    r = rules.read_text() if rules.exists() else ""
    if "GlobaleSIM (added by apply_overlay.py)" not in r:
        rules.write_text(r + extra)
    print("  R8 optimization on (minify + resource shrink)" if rel else "  WARNING: release minify flag not found")


    # 12. GlobaleSIM Phone: Linphone SDK (calls over data), mic permission, R8 keep rules
    #     Newest stable 5.x from Linphone's Maven repo (no-video build: smaller).
    import json as _json, urllib.request as _ur
    ver = "5.4.+"
    try:
        meta = _ur.urlopen("https://download.linphone.org/maven_repository/org/linphone/no-video/linphone-sdk-android/maven-metadata.xml", timeout=30).read().decode()
        vers = [v for v in re.findall(r"<version>([^<]+)</version>", meta) if re.fullmatch(r"5\.\d+\.\d+", v)]
        if vers:
            ver = sorted(vers, key=lambda v: tuple(int(x) for x in v.split(".")))[-1]
    except Exception as e:
        print(f"  WARNING: could not read Linphone versions ({e}); using {ver}")
    repo_line = 'maven { url = uri("https://download.linphone.org/maven_repository") }'
    settings = up / "settings.gradle.kts"
    added_repo = False
    if settings.exists():
        st = settings.read_text()
        if "download.linphone.org" not in st:
            m = re.search(r"dependencyResolutionManagement\s*\{.*?repositories\s*\{", st, re.S)
            if m:
                st = st[:m.end()] + "\n        " + repo_line + st[m.end():]
                settings.write_text(st); added_repo = True
        else:
            added_repo = True
    g = bg.read_text()
    if not added_repo and "download.linphone.org" not in g:
        g = "repositories {\n    " + repo_line + "\n}\n" + g
    if "linphone-sdk-android" not in g:
        m = re.search(r"\ndependencies\s*\{", g)
        if not m:
            die("no dependencies block in app-unpriv build.gradle.kts")
        g = g[:m.end()] + f'\n    implementation("org.linphone.no-video:linphone-sdk-android:{ver}")' + g[m.end():]
    bg.write_text(g)
    x = mf.read_text()
    for perm in ("RECORD_AUDIO", "MODIFY_AUDIO_SETTINGS", "BLUETOOTH_CONNECT", "WAKE_LOCK",
                 # ring with the app closed: background line, notifications, lock-screen call screen
                 "POST_NOTIFICATIONS", "FOREGROUND_SERVICE", "FOREGROUND_SERVICE_SPECIAL_USE", "FOREGROUND_SERVICE_MICROPHONE",
                 "RECEIVE_BOOT_COMPLETED", "VIBRATE", "USE_FULL_SCREEN_INTENT", "REQUEST_IGNORE_BATTERY_OPTIMIZATIONS"):
        if f"android.permission.{perm}" not in x:
            x = x.replace("<application", f'<uses-permission android:name="android.permission.{perm}" />\n    <application', 1)
    if "tel.globalesim.app.VoiceService" not in x:
        comps = """
        <service
            android:name="tel.globalesim.app.VoiceService"
            android:exported="false"
            android:foregroundServiceType="specialUse|microphone">
            <property
                android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
                android:value="Keeps the customer's GlobaleSIM Phone (VoIP) line registered so incoming calls ring while the app is closed" />
        </service>
        <receiver android:name="tel.globalesim.app.VoiceBootReceiver" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.BOOT_COMPLETED" />
                <action android:name="android.intent.action.MY_PACKAGE_REPLACED" />
            </intent-filter>
        </receiver>
        <receiver android:name="tel.globalesim.app.CallActionReceiver" android:exported="false" />
"""
        x = x.replace("</application>", comps + "\n    </application>", 1)
    if 'android:host="phone"' not in x:
        x = x.replace('<data android:scheme="globalesim" android:host="order" />',
                      '<data android:scheme="globalesim" android:host="order" />\n                <data android:scheme="globalesim" android:host="phone" />', 1)
    mf.write_text(x)
    rules = mod / "proguard-rules.pro"
    r = rules.read_text() if rules.exists() else ""
    if "org.linphone" not in r:
        rules.write_text(r + "\n# Linphone SDK (JNI)\n-keep class org.linphone.** { *; }\n-keep class tel.globalesim.app.VoiceManager { *; }\n")
    print(f"  Phone: Linphone SDK {ver}, mic permission, repo {'settings' if added_repo else 'module'}")

    # 7. Wording: say GlobaleSIM everywhere in the card manager
    for strings in (mod / "src/main/res").glob("values*/strings.xml"):
        s = strings.read_text()
        s2 = s.replace("EasyEUICC", a.app_name)
        if s2 != s:
            strings.write_text(s2)
    print("Done.")


if __name__ == "__main__":
    main()
