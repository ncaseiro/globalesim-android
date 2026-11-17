package tel.globalesim.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.app.NotificationManager
import android.provider.Settings
import android.telephony.euicc.EuiccManager
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import org.json.JSONObject

/**
 * GlobaleSIM store screen.
 *
 * Loads the store (app.php on globalesim.tel) in a WebView and gives the page a small
 * bridge, window.GlobaleSIM, so it can install a purchased eSIM:
 *   - installToPhone(ac): copy the activation code and open Android's eSIM settings
 *   - installToCard(ac):  copy the code and open the built-in card manager
 *                         (the EasyEUICC screen this app is built on), where the customer
 *                         taps + and pastes it; the card manager writes it to the card
 *                         in the SIM slot.
 * Only pages on STORE_HOST may use the bridge; every other link opens in the browser.
 * Deep link globalesim://order/<token> (used after PayPal in the browser) reopens the order.
 */
class StoreActivity : Activity() {

    companion object {
        // ---- change these two lines if your store lives somewhere else ----
        const val STORE_URL = "https://globalesim.tel/app.php"
        const val STORE_HOST = "globalesim.tel"
        // Card manager = the EasyEUICC main screen, re-labelled by scripts/apply_overlay.py
        const val ACTION_CARD_MANAGER = "tel.globalesim.CARD_MANAGER"
        // Upstream download wizard; accepts lpa:1$smdp$matchingId and pre-fills the code
        const val DOWNLOAD_WIZARD = "im.angry.openeuicc.ui.wizard.DownloadWizardActivity"
    }

    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Color.parseColor("#0E1A2B")
        window.navigationBarColor = Color.parseColor("#0E1A2B")

        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#0E1A2B"))
        web.overScrollMode = View.OVER_SCROLL_NEVER
        setContentView(web)

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            userAgentString = "$userAgentString GlobaleSIMApp/1"
        }
        web.addJavascriptInterface(Bridge(), "GlobaleSIM")
        // GlobaleSIM Phone: native calling engine, reports to the page as window.gsVoiceEvent(json)
        runCatching { VoiceManager.init(this) }
        VoiceManager.listener = { js ->
            runOnUiThread {
                web.evaluateJavascript("window.gsVoiceEvent && window.gsVoiceEvent($js)", null)
                if (js.contains("\"ended\":true") && !VoiceManager.inCall()) showOverLock(false)
            }
        }
        // keep the line signed in while the app is closed (rings like a normal phone)
        VoiceService.start(this)
        handleCallIntent(intent)
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                // Only top-level navigations; never hijack iframes (payment widgets, maps…)
                if (!request.isForMainFrame) return false
                // Store pages marked ext=1 (PayPal checkout) must run in the real browser
                if (isStore(u) && u.getQueryParameter("ext") != "1") return false
                openOutside(u)
                return true
            }
        }

        if (savedInstanceState != null) web.restoreState(savedInstanceState)
        else web.loadUrl(urlFor(intent))
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleCallIntent(intent)
        if (intent.data != null) web.loadUrl(urlFor(intent))
    }

    override fun onResume() { super.onResume(); VoiceManager.uiVisible = true }
    override fun onPause() { VoiceManager.uiVisible = false; super.onPause() }

    /** Opened from the incoming-call notification: show over the lock screen, answer if asked. */
    private var answerAfterMic = false
    private fun handleCallIntent(i: Intent?) {
        if (i?.getBooleanExtra(CallNotif.EXTRA_INCOMING, false) != true) return
        showOverLock(true)
        if (i.getBooleanExtra(CallNotif.EXTRA_ANSWER, false)) {
            i.removeExtra(CallNotif.EXTRA_ANSWER)
            if (hasMic()) VoiceManager.answer() else { answerAfterMic = true; askMic() }
        }
    }
    private fun showOverLock(on: Boolean) {
        if (Build.VERSION.SDK_INT >= 27) { setShowWhenLocked(on); setTurnScreenOn(on) }
    }

    // ---- what the phone must allow so calls ring when the app is closed ----
    private fun notifOk() = Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
    private fun overlayOk() = Settings.canDrawOverlays(this)
    private fun batteryOk() = getSystemService(PowerManager::class.java)?.isIgnoringBatteryOptimizations(packageName) ?: true
    private fun fullScreenOk() = Build.VERSION.SDK_INT < 34 || (getSystemService(NotificationManager::class.java)?.canUseFullScreenIntent() ?: true)

    override fun onDestroy() {
        VoiceManager.listener = null
        super.onDestroy()
    }

    private val MIC_REQUEST = 7
    private val NOTIF_REQUEST = 8
    private fun hasMic() = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    private fun askMic() {
        val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 31) perms += Manifest.permission.BLUETOOTH_CONNECT
        requestPermissions(perms.toTypedArray(), MIC_REQUEST)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == MIC_REQUEST) {
            if (answerAfterMic) { answerAfterMic = false; if (hasMic()) VoiceManager.answer() }
            val js = JSONObject().put("type", "perm").put("mic", hasMic()).toString()
            web.evaluateJavascript("window.gsVoiceEvent && window.gsVoiceEvent($js)", null)
        }
        if (requestCode == NOTIF_REQUEST) {
            VoiceService.start(this)
            val js = JSONObject().put("type", "bg").toString()
            web.evaluateJavascript("window.gsVoiceEvent && window.gsVoiceEvent($js)", null)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    @Deprecated("Deprecated in Java")
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (web.canGoBack()) {
            web.goBack()
        } else {
            super.onBackPressed()
        }
    }

    /** globalesim://order/<token> → store order page */
    private fun urlFor(i: Intent?): String {
        val d = i?.data ?: return STORE_URL
        if (d.scheme == "globalesim" && d.host == "phone") return if (i.getBooleanExtra("gs_texts", false)) "$STORE_URL?phone&texts=1" else "$STORE_URL?phone"
        if (d.scheme == "globalesim" && d.host == "order") {
            val token = d.lastPathSegment ?: ""
            if (Regex("^[a-f0-9]{32}$").matches(token)) return "$STORE_URL?order=$token"
        }
        return STORE_URL
    }

    private fun isStore(u: Uri): Boolean =
        u.scheme == "https" && (u.host == STORE_HOST || u.host == "www.$STORE_HOST")

    private fun openOutside(u: Uri) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, u).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) {
            toast("No app can open this link")
        }
    }

    private fun toast(msg: String) = runOnUiThread { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() }

    private fun copyText(label: String, text: String) {
        val cm = getSystemService(ClipboardManager::class.java)
        cm?.setPrimaryClip(ClipData.newPlainText(label, text))
    }

    private fun phoneHasEsim(): Boolean {
        if (!packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY_EUICC)) return false
        return getSystemService(EuiccManager::class.java)?.isEnabled == true
    }

    /** Methods callable from the store page as window.GlobaleSIM.xxx() */
    inner class Bridge {

        private fun validCode(ac: String) = ac.startsWith("LPA:1$") && ac.length < 300

        @JavascriptInterface
        fun deviceInfo(): String = JSONObject()
            .put("esim", phoneHasEsim())
            .put("android", Build.VERSION.SDK_INT)
            .put("model", "${Build.MANUFACTURER} ${Build.MODEL}")
            .put("app", 5)
            .put("voice", true)
            .put("voiceBg", true)
            .put("mic", hasMic())
            .put("cardManager", true)
            .toString()

        @JavascriptInterface
        fun copy(text: String) {
            copyText("GlobaleSIM", text.take(500))
            toast("Copied")
        }

        @JavascriptInterface
        fun installToPhone(ac: String) {
            if (!validCode(ac)) return toast("Invalid activation code")
            copyText("eSIM activation code", ac)
            runOnUiThread {
                val tries = buildList {
                    add(Intent(EuiccManager.ACTION_MANAGE_EMBEDDED_SUBSCRIPTIONS))
                    add(Intent("android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"))
                    add(Intent(Settings.ACTION_NETWORK_OPERATOR_SETTINGS))
                    add(Intent(Settings.ACTION_WIRELESS_SETTINGS))
                }
                val ok = tries.any { i ->
                    try { startActivity(i); true } catch (e: Exception) { false }
                }
                Toast.makeText(
                    this@StoreActivity,
                    if (ok) "Code copied. Tap \"Add eSIM\" → \"Enter code manually\" and paste."
                    else "Code copied. Open Settings → SIMs → Add eSIM and paste it.",
                    Toast.LENGTH_LONG
                ).show()
            }
        }

        /** Open the card manager (plans on the physical eSIM card in this phone): view, add, switch, rename, delete. */
        @JavascriptInterface
        fun openCardManager() {
            runOnUiThread {
                try {
                    startActivity(Intent(ACTION_CARD_MANAGER).setPackage(packageName))
                } catch (e: Exception) {
                    Toast.makeText(this@StoreActivity, "Card manager not available", Toast.LENGTH_LONG).show()
                }
            }
        }

        @JavascriptInterface
        fun installToCard(ac: String) {
            if (!validCode(ac)) return toast("Invalid activation code")
            copyText("eSIM activation code", ac)
            runOnUiThread {
                // 1) Open the card writer (download wizard) with the code already filled in:
                //    the customer just picks the card and taps Next → Download.
                try {
                    val lpa = Uri.parse("lpa:" + ac.substring(4)) // "LPA:1$…" → "lpa:1$…"
                    startActivity(
                        Intent(Intent.ACTION_VIEW, lpa).setClassName(packageName, DOWNLOAD_WIZARD)
                    )
                    Toast.makeText(
                        this@StoreActivity,
                        "Choose your GlobaleSIM card, then tap Next to install. Keep the app open until it finishes.",
                        Toast.LENGTH_LONG
                    ).show()
                    return@runOnUiThread
                } catch (e: Exception) {
                    // fall through to the card manager + paste flow
                }
                try {
                    startActivity(Intent(ACTION_CARD_MANAGER).setPackage(packageName))
                    Toast.makeText(
                        this@StoreActivity,
                        "Code copied. Tap + (Download) and choose paste / enter the code. Keep the app open until it finishes.",
                        Toast.LENGTH_LONG
                    ).show()
                } catch (e: Exception) {
                    Toast.makeText(this@StoreActivity, "Card manager not available", Toast.LENGTH_LONG).show()
                }
            }
        }

        // ---------------- GlobaleSIM Phone ----------------
        /** Asks for the microphone if needed; returns true when calls can use it. */
        @JavascriptInterface
        fun voiceReady(): Boolean {
            if (hasMic()) return true
            runOnUiThread { askMic() }
            return false
        }
        @JavascriptInterface fun voiceLogin(json: String) = VoiceManager.login(json)

        /** What's still missing for calls to ring with the app closed: {"notif":bool,"battery":bool,"fullscreen":bool} */
        @JavascriptInterface
        fun voiceBackground(): String = JSONObject()
            .put("notif", notifOk()).put("overlay", overlayOk()).put("battery", batteryOk()).put("fullscreen", fullScreenOk()).toString()

        /** Ask for the next missing one (notifications → battery → full-screen calls). */
        @SuppressLint("BatteryLife")
        @JavascriptInterface
        fun voiceFixBackground() {
            runOnUiThread {
                try {
                    when {
                        !notifOk() -> requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), NOTIF_REQUEST)
                        !overlayOk() -> startActivity(
                            Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
                        !batteryOk() -> startActivity(
                            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
                        !fullScreenOk() && Build.VERSION.SDK_INT >= 34 -> startActivity(
                            Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, Uri.parse("package:$packageName")))
                    }
                } catch (e: Exception) {
                    startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
                }
            }
        }
        @JavascriptInterface fun voiceLogout() = VoiceManager.logout()
        @JavascriptInterface fun voiceState(): String = VoiceManager.snapshot()
        @JavascriptInterface
        fun voiceCall(number: String) {
            if (!hasMic()) { runOnUiThread { askMic() }; return }
            VoiceManager.call(number)
        }
        @JavascriptInterface
        fun voiceAnswer() {
            if (!hasMic()) { runOnUiThread { askMic() }; return }
            VoiceManager.answer()
        }
        @JavascriptInterface fun voiceDecline() = VoiceManager.decline()
        @JavascriptInterface fun voiceHangup() = VoiceManager.hangup()
        @JavascriptInterface fun voiceDtmf(d: String) = VoiceManager.dtmf(d)
        @JavascriptInterface fun voiceMute(on: Boolean) = VoiceManager.mute(on)
        @JavascriptInterface fun voiceSpeaker(on: Boolean) = VoiceManager.speaker(on)

        @JavascriptInterface
        fun openExternal(url: String) {
            val u = Uri.parse(url)
            if (isStore(u)) runOnUiThread { openOutside(u) }
        }
    }
}
