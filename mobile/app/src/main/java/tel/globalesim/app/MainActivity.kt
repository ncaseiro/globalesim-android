package tel.globalesim.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.NotificationManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.telephony.euicc.EuiccManager
import android.view.View
import android.view.WindowInsets
import android.webkit.GeolocationPermissions
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.Toast
import org.json.JSONObject

/**
 * GlobaleSIM Mobile — the phone-plan app.
 *
 * Shows globalesim.tel/app.php?mobile (home with data left, keypad, calls, texts, top-ups, plan)
 * in a WebView and gives the page window.GlobaleSIM: the native calling engine (VoiceManager),
 * the network the phone is on (Wi-Fi or cellular, shown on screen and used to save data),
 * and eSIM install help. Only pages on STORE_HOST may use the bridge; other links open outside.
 */
class MainActivity : Activity() {

    companion object {
        const val STORE_URL = "https://globalesim.tel/app.php"
        const val HOME_URL = "$STORE_URL?mobile"
        const val STORE_HOST = "globalesim.tel"
    }

    private lateinit var web: WebView
    private var netCb: ConnectivityManager.NetworkCallback? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val blue = Color.parseColor("#0B3D91")
        web = WebView(this)
        web.setBackgroundColor(Color.parseColor("#F5F8FC"))
        web.overScrollMode = View.OVER_SCROLL_NEVER
        val root = FrameLayout(this)
        root.setBackgroundColor(blue)
        root.addView(web, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        // draw edge to edge and pad by the system bars ourselves (Android 15+ does edge to edge anyway)
        if (Build.VERSION.SDK_INT >= 30) window.setDecorFitsSystemWindows(false)
        else window.decorView.systemUiVisibility = View.SYSTEM_UI_FLAG_LAYOUT_STABLE or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
        root.setOnApplyWindowInsetsListener { v, insets ->
            if (Build.VERSION.SDK_INT >= 30) {
                val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
                val ime = insets.getInsets(WindowInsets.Type.ime())
                v.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            } else {
                v.setPadding(insets.systemWindowInsetLeft, insets.systemWindowInsetTop, insets.systemWindowInsetRight, insets.systemWindowInsetBottom)
            }
            insets
        }
        setContentView(root)

        with(web.settings) {
            javaScriptEnabled = true
            domStorageEnabled = true
            // GlobaleSIMApp = "inside our Android app" for the server; GlobaleSIMMobile = this app
            userAgentString = "$userAgentString GlobaleSIMApp/1 GlobaleSIMMobile/1"
            setGeolocationEnabled(true)
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onGeolocationPermissionsShowPrompt(origin: String, callback: GeolocationPermissions.Callback) {
                if (!isStore(Uri.parse(origin))) { callback.invoke(origin, false, false); return }
                if (hasLocation()) { callback.invoke(origin, true, false); return }
                pendingGeo = origin to callback
                requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION), GEO_REQUEST)
            }
        }
        web.addJavascriptInterface(Bridge(), "GlobaleSIM")
        runCatching { VoiceManager.init(this) }
        VoiceManager.listener = { js ->
            runOnUiThread {
                web.evaluateJavascript("window.gsVoiceEvent && window.gsVoiceEvent($js)", null)
                if (js.contains("\"ended\":true") && !VoiceManager.inCall()) showOverLock(false)
            }
        }
        VoiceService.start(this)
        handleCallIntent(intent)
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val u = request.url
                if (!request.isForMainFrame) return false
                if (u.scheme == "tel") { openOutside(u); return true }
                if (isStore(u) && u.getQueryParameter("ext") != "1") return false
                openOutside(u)
                return true
            }
        }
        watchNetwork()
        if (savedInstanceState != null) web.restoreState(savedInstanceState) else web.loadUrl(urlFor(intent))
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleCallIntent(intent)
        if (intent.data != null) web.loadUrl(urlFor(intent))
    }

    override fun onResume() { super.onResume(); VoiceManager.uiVisible = true; sendNet() }
    override fun onPause() { VoiceManager.uiVisible = false; super.onPause() }

    override fun onDestroy() {
        VoiceManager.listener = null
        netCb?.let { runCatching { getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(it) } }
        super.onDestroy()
    }

    // ---- network: tell the page whether calls are on Wi-Fi (free) or cellular (uses data) ----
    private fun netType(): String {
        val cm = getSystemService(ConnectivityManager::class.java) ?: return "none"
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return "none"
        return when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cell"
            else -> "other"
        }
    }
    private fun sendNet() {
        if (!::web.isInitialized) return
        val js = JSONObject().put("type", "net").put("net", netType()).toString()
        runOnUiThread { web.evaluateJavascript("window.gsVoiceEvent && window.gsVoiceEvent($js)", null) }
    }
    private fun watchNetwork() {
        val cm = getSystemService(ConnectivityManager::class.java) ?: return
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = sendNet()
            override fun onLost(network: Network) = sendNet()
            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = sendNet()
        }
        runCatching { cm.registerDefaultNetworkCallback(cb); netCb = cb }
    }

    // ---- incoming call from the notification: show over the lock screen, answer if asked ----
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

    private fun notifOk() = Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
    private fun overlayOk() = Settings.canDrawOverlays(this)
    private fun batteryOk() = getSystemService(PowerManager::class.java)?.isIgnoringBatteryOptimizations(packageName) ?: true
    private fun fullScreenOk() = Build.VERSION.SDK_INT < 34 || (getSystemService(NotificationManager::class.java)?.canUseFullScreenIntent() ?: true)

    private val MIC_REQUEST = 7
    private val NOTIF_REQUEST = 8
    private val GEO_REQUEST = 9
    private var pendingGeo: Pair<String, GeolocationPermissions.Callback>? = null
    private fun hasLocation() = checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED ||
        checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
    private fun hasMic() = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    private fun askMic() {
        val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 31) perms += Manifest.permission.BLUETOOTH_CONNECT
        requestPermissions(perms.toTypedArray(), MIC_REQUEST)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        when (requestCode) {
            MIC_REQUEST -> {
                if (answerAfterMic) { answerAfterMic = false; if (hasMic()) VoiceManager.answer() }
                val js = JSONObject().put("type", "perm").put("mic", hasMic()).toString()
                web.evaluateJavascript("window.gsVoiceEvent && window.gsVoiceEvent($js)", null)
            }
            GEO_REQUEST -> { pendingGeo?.let { (o, cb) -> cb.invoke(o, hasLocation(), false) }; pendingGeo = null }
            NOTIF_REQUEST -> {
                VoiceService.start(this)
                val js = JSONObject().put("type", "bg").toString()
                web.evaluateJavascript("window.gsVoiceEvent && window.gsVoiceEvent($js)", null)
            }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) { super.onSaveInstanceState(outState); web.saveState(outState) }

    @Deprecated("Deprecated in Java")
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        // the page handles "back" between its own screens first
        web.evaluateJavascript("(window.gsBack && window.gsBack()) ? 1 : 0") { r ->
            if (r != "1") { if (web.canGoBack()) web.goBack() else super.onBackPressed() }
        }
    }

    /** Notifications open globalesim://phone (texts: gs_texts) → the matching screen. */
    private fun urlFor(i: Intent?): String {
        val d = i?.data ?: return HOME_URL
        if (d.scheme == "globalesim" && d.host == "phone") return if (i.getBooleanExtra("gs_texts", false)) "$HOME_URL&tab=texts" else HOME_URL
        return HOME_URL
    }

    private fun isStore(u: Uri): Boolean = u.scheme == "https" && (u.host == STORE_HOST || u.host == "www.$STORE_HOST")

    private fun openOutside(u: Uri) {
        try { startActivity(Intent(Intent.ACTION_VIEW, u).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
        catch (e: Exception) { toast("No app can open this link") }
    }
    private fun toast(msg: String) = runOnUiThread { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() }
    private fun copyText(label: String, text: String) {
        getSystemService(ClipboardManager::class.java)?.setPrimaryClip(ClipData.newPlainText(label, text))
    }
    private fun phoneHasEsim(): Boolean {
        if (!packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY_EUICC)) return false
        return getSystemService(EuiccManager::class.java)?.isEnabled == true
    }

    /** Methods the page calls as window.GlobaleSIM.xxx() — same names as the main GlobaleSIM app. */
    inner class Bridge {
        private fun validCode(ac: String) = ac.startsWith("LPA:1$") && ac.length < 300

        @JavascriptInterface
        fun deviceInfo(): String = JSONObject()
            .put("esim", phoneHasEsim())
            .put("android", Build.VERSION.SDK_INT)
            .put("model", "${Build.MANUFACTURER} ${Build.MODEL}")
            .put("app", 1)
            .put("mobile", true)
            .put("voice", true)
            .put("voiceBg", true)
            .put("voiceXfer", true)
            .put("mic", hasMic())
            .put("net", netType())
            .toString()

        @JavascriptInterface fun netType(): String = this@MainActivity.netType()

        @JavascriptInterface
        fun copy(text: String) { copyText("GlobaleSIM", text.take(500)); toast("Copied") }

        /** Copy the activation code and open Android's eSIM screen (Add eSIM → enter code). */
        @JavascriptInterface
        fun installToPhone(ac: String) {
            if (!validCode(ac)) return toast("Invalid activation code")
            copyText("eSIM activation code", ac)
            runOnUiThread {
                val tries = listOf(
                    Intent(EuiccManager.ACTION_MANAGE_EMBEDDED_SUBSCRIPTIONS),
                    Intent("android.settings.MANAGE_ALL_SIM_PROFILES_SETTINGS"),
                    Intent(Settings.ACTION_NETWORK_OPERATOR_SETTINGS),
                    Intent(Settings.ACTION_WIRELESS_SETTINGS))
                val ok = tries.any { i -> try { startActivity(i); true } catch (e: Exception) { false } }
                Toast.makeText(this@MainActivity,
                    if (ok) "Code copied. Tap \"Add eSIM\" → \"Enter code manually\" and paste." else "Code copied. Open Settings → SIMs → Add eSIM and paste it.",
                    Toast.LENGTH_LONG).show()
            }
        }

        /** Data saving: open this phone's "mobile data per app" settings so video apps can be limited. */
        @JavascriptInterface
        fun openDataSettings() {
            runOnUiThread {
                val tries = listOf(Intent(Settings.ACTION_DATA_USAGE_SETTINGS), Intent(Settings.ACTION_DATA_ROAMING_SETTINGS), Intent(Settings.ACTION_WIRELESS_SETTINGS))
                if (!tries.any { i -> try { startActivity(i); true } catch (e: Exception) { false } }) toast("Open Settings → Network → Data usage")
            }
        }

        @JavascriptInterface
        fun voiceReady(): Boolean { if (hasMic()) return true; runOnUiThread { askMic() }; return false }
        @JavascriptInterface fun voiceLogin(json: String) = VoiceManager.login(json)
        @JavascriptInterface
        fun voiceBackground(): String = JSONObject()
            .put("notif", notifOk()).put("overlay", overlayOk()).put("battery", batteryOk()).put("fullscreen", fullScreenOk()).toString()
        @SuppressLint("BatteryLife")
        @JavascriptInterface
        fun voiceFixBackground() {
            runOnUiThread {
                try {
                    when {
                        !notifOk() -> requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), NOTIF_REQUEST)
                        !overlayOk() -> startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
                        !batteryOk() -> startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
                        !fullScreenOk() && Build.VERSION.SDK_INT >= 34 -> startActivity(Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT, Uri.parse("package:$packageName")))
                    }
                } catch (e: Exception) {
                    startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
                }
            }
        }
        @JavascriptInterface fun voiceLogout() = VoiceManager.logout()
        @JavascriptInterface fun voiceState(): String = VoiceManager.snapshot()
        @JavascriptInterface
        fun voiceCall(number: String) { if (!hasMic()) { runOnUiThread { askMic() }; return }; VoiceManager.call(number) }
        @JavascriptInterface
        fun voiceAnswer() { if (!hasMic()) { runOnUiThread { askMic() }; return }; VoiceManager.answer() }
        @JavascriptInterface fun voiceDecline() = VoiceManager.decline()
        @JavascriptInterface fun voiceHangup() = VoiceManager.hangup()
        @JavascriptInterface fun voiceHold(on: Boolean) = VoiceManager.hold(on)
        @JavascriptInterface fun voiceTransfer(number: String) = VoiceManager.transfer(number)
        @JavascriptInterface fun voiceConsult(number: String) = VoiceManager.consult(number)
        @JavascriptInterface fun voiceCompleteTransfer() = VoiceManager.completeTransfer()
        @JavascriptInterface fun voiceCancelConsult() = VoiceManager.cancelConsult()
        @JavascriptInterface fun voiceDtmf(d: String) = VoiceManager.dtmf(d)
        @JavascriptInterface fun voiceMute(on: Boolean) = VoiceManager.mute(on)
        @JavascriptInterface fun voiceSpeaker(on: Boolean) = VoiceManager.speaker(on)

        @JavascriptInterface
        fun openExternal(url: String) { val u = Uri.parse(url); if (isStore(u)) runOnUiThread { openOutside(u) } }
    }
}
