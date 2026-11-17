package tel.globalesim.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import org.linphone.core.Account
import org.linphone.core.AudioDevice
import org.linphone.core.Call
import org.linphone.core.ChatMessage
import org.linphone.core.ChatRoom
import org.linphone.core.Core
import org.linphone.core.CoreListenerStub
import org.linphone.core.Factory
import org.linphone.core.MediaEncryption
import org.linphone.core.Reason
import org.linphone.core.RegistrationState
import org.linphone.core.TransportType

/**
 * GlobaleSIM Phone — the native calling engine (Linphone SDK).
 *
 * One Core per app process. Signs in to voice.globalesim.tel over TLS with the customer's
 * hidden SIP login (handed over by the store page after the customer signs in), places and
 * receives calls with encrypted audio (SRTP), and reports every change to the store page
 * through [listener] as small JSON events. All Core calls run on the main thread.
 */
object VoiceManager {
    private const val PREFS = "gs_voice"
    private val main = Handler(Looper.getMainLooper())
    private var core: Core? = null
    private lateinit var app: Context

    /** Receives JSON events: {"type":"reg"|"call", ...}. Set by StoreActivity. */
    var listener: ((String) -> Unit)? = null

    private var regState = "none"
    private var regMessage = ""

    /** True while the store screen is on top (StoreActivity onResume/onPause). */
    @Volatile var uiVisible = false
    private var answeredIncoming = false
    private var declinedIncoming = false

    fun hasLogin(ctx: Context): Boolean =
        ctx.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("login", null) != null

    fun inCall(): Boolean = (core?.callsNb ?: 0) > 0

    /** Short status for the "phone line" notification. */
    fun regText(): String = when (regState) {
        "ok" -> "Ready for calls"
        "connecting" -> "Connecting…"
        "failed" -> "Offline — tap to reconnect"
        else -> "Starting…"
    }

    fun init(ctx: Context) = onMain {
        if (core != null) return@onMain
        app = ctx.applicationContext
        val factory = Factory.instance()
        factory.setDebugMode(false, "GlobaleSIM")
        val c = factory.createCore(null, null, app)
        c.isVideoCaptureEnabled = false
        c.isVideoDisplayEnabled = false
        c.mediaEncryption = MediaEncryption.SRTP
        c.isMediaEncryptionMandatory = true
        c.isKeepAliveEnabled = true          // keeps the connection open so calls reach a closed app
        c.isNativeRingingEnabled = false     // Ringer plays the phone's own ringtone + vibration
        c.ring = null
        c.addListener(coreListener)
        c.start()
        core = c
        val p = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val saved = p.getString("login", null)
        if (saved != null) runCatching { applyLogin(JSONObject(saved)) }
    }

    /** {"sip_user":..,"sip_pass":..,"sip_server":"voice.globalesim.tel","did":"1940..."} */
    fun login(json: String) = onMain {
        val o = JSONObject(json)
        app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString("login", o.toString()).apply()
        applyLogin(o)
        VoiceService.start(app)
    }

    fun logout() = onMain {
        app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove("login").apply()
        core?.let { c ->
            c.currentCall?.terminate()
            c.accountList.forEach { c.removeAccount(it) }
            c.clearAllAuthInfo()
        }
        regState = "none"; emit(snapshot())
        Ringer.stop(); CallNotif.cancelIncoming(app); VoiceService.stop(app)
    }

    private fun applyLogin(o: JSONObject) {
        val c = core ?: return
        val user = o.getString("sip_user"); val pass = o.getString("sip_pass")
        val domain = o.optString("sip_server", "voice.globalesim.tel")
        val factory = Factory.instance()
        c.accountList.forEach { c.removeAccount(it) }
        c.clearAllAuthInfo()
        c.addAuthInfo(factory.createAuthInfo(user, null, pass, null, null, domain, null))
        val params = c.createAccountParams()
        params.identityAddress = factory.createAddress("sip:$user@$domain")
        val server = factory.createAddress("sip:$domain:5061")
        server?.transport = TransportType.Tls
        params.serverAddress = server
        params.isRegisterEnabled = true
        params.expires = 600
        val acc = c.createAccount(params)
        c.addAccount(acc)
        c.defaultAccount = acc
        domainFor = domain
    }
    private var domainFor = "voice.globalesim.tel"

    private fun target(number: String): org.linphone.core.Address? {
        var d = number.filter { it.isDigit() }
        if (d.length == 10) d = "1$d"
        if (d.isEmpty()) return null
        return Factory.instance().createAddress("sip:$d@$domainFor")
    }

    fun call(number: String) = onMain {
        val c = core ?: return@onMain
        val addr = target(number) ?: return@onMain
        val params = c.createCallParams(null) ?: return@onMain
        params.mediaEncryption = MediaEncryption.SRTP
        params.isVideoEnabled = false
        c.inviteAddressWithParams(addr, params)
    }

    fun answer() = onMain {
        Ringer.stop(); CallNotif.cancelIncoming(app)
        val c = core ?: return@onMain
        (c.calls.firstOrNull { it.state == Call.State.IncomingReceived || it.state == Call.State.IncomingEarlyMedia } ?: c.currentCall)?.accept()
    }
    fun decline() = onMain {
        Ringer.stop(); CallNotif.cancelIncoming(app)
        declinedIncoming = true
        (core?.currentCall ?: core?.calls?.firstOrNull())?.decline(Reason.Declined)
    }
    fun hangup() = onMain { (core?.currentCall ?: core?.calls?.firstOrNull())?.terminate() }

    // ---- business call handling: hold (caller hears music), transfer now, talk first then transfer ----
    private fun held(): Call? = core?.calls?.firstOrNull { it.state == Call.State.Paused || it.state == Call.State.Pausing }
    private fun active(): Call? = core?.currentCall ?: core?.calls?.firstOrNull { it.state == Call.State.StreamsRunning || it.state == Call.State.Connected }

    fun hold(on: Boolean) = onMain {
        if (on) active()?.pause() else if ((core?.callsNb ?: 0) == 1) held()?.resume()
        emit(snapshot())
    }
    /** Transfer now: the other person is sent straight to [number] (extension or phone number). */
    fun transfer(number: String) = onMain {
        val call = active() ?: held() ?: return@onMain
        val addr = target(number) ?: return@onMain
        call.transferTo(addr)
    }
    /** Talk first: put the caller on hold and call [number]. */
    fun consult(number: String) = onMain {
        val c = core ?: return@onMain
        val first = active() ?: held() ?: return@onMain
        if (first.state != Call.State.Paused) first.pause()
        val addr = target(number) ?: return@onMain
        val params = c.createCallParams(null) ?: return@onMain
        params.mediaEncryption = MediaEncryption.SRTP
        params.isVideoEnabled = false
        c.inviteAddressWithParams(addr, params)
    }
    /** Connect the held caller with the person you're talking to now, and drop out. */
    fun completeTransfer() = onMain {
        val c = core ?: return@onMain
        val first = held() ?: return@onMain
        val second = c.calls.firstOrNull { it != first } ?: return@onMain
        first.transferToAnother(second)
    }
    /** Give up the transfer: hang up the second call, back to the held caller. */
    fun cancelConsult() = onMain {
        val c = core ?: return@onMain
        val first = held() ?: return@onMain
        c.calls.firstOrNull { it != first }?.terminate()
        main.postDelayed({ if (first.state == Call.State.Paused) first.resume() }, 400)
    }
    fun dtmf(digit: String) = onMain { digit.firstOrNull()?.let { core?.currentCall?.sendDtmf(it) } }
    fun mute(on: Boolean) = onMain { core?.isMicEnabled = !on; emit(snapshot()) }

    fun speaker(on: Boolean) = onMain {
        val c = core ?: return@onMain
        val call = c.currentCall ?: return@onMain
        val want = if (on) AudioDevice.Type.Speaker else AudioDevice.Type.Earpiece
        c.audioDevices.firstOrNull { it.type == want }?.let { call.outputAudioDevice = it }
        emit(snapshot())
    }

    /** Snapshot for the page: registration + current call. */
    fun snapshot(): String {
        val o = JSONObject().put("type", "state").put("reg", regState).put("regMessage", regMessage)
        val c = core
        val calls: Array<Call> = c?.calls ?: arrayOf()
        val pausedOne = calls.firstOrNull { it.state == Call.State.Paused || it.state == Call.State.Pausing }
        // with two calls (talk first, then transfer) the one in progress is shown, the other is "on hold"
        val call = (if (calls.size > 1) calls.firstOrNull { it != pausedOne } else null) ?: c?.currentCall ?: calls.firstOrNull()
        if (call != null) {
            o.put("call", JSONObject()
                .put("state", call.state?.name ?: "")
                .put("dir", if (call.dir == Call.Dir.Incoming) "in" else "out")
                .put("peer", call.remoteAddress.username ?: "")
                .put("duration", call.duration)
                .put("held", call.state == Call.State.Paused || call.state == Call.State.Pausing)
                .put("muted", c?.isMicEnabled == false)
                .put("speaker", call.outputAudioDevice?.type == AudioDevice.Type.Speaker))
            if (calls.size > 1 && pausedOne != null && pausedOne != call)
                o.put("held_call", JSONObject().put("peer", pausedOne.remoteAddress.username ?: "").put("duration", pausedOne.duration))
        }
        return o.toString()
    }

    private val coreListener = object : CoreListenerStub() {
        override fun onAccountRegistrationStateChanged(core: Core, account: Account, state: RegistrationState?, message: String) {
            regState = when (state) {
                RegistrationState.Ok -> "ok"
                RegistrationState.Progress -> "connecting"
                RegistrationState.Failed -> "failed"
                RegistrationState.Cleared, RegistrationState.None -> "none"
                else -> state?.name?.lowercase() ?: "none"
            }
            regMessage = message
            VoiceService.instance?.refresh()
            emit(snapshot())
        }

        /** Incoming text (receive-only line): notify, and tell the page to refresh its Messages list. */
        override fun onMessageReceived(core: Core, chatRoom: ChatRoom, message: ChatMessage) {
            val from = message.fromAddress.username ?: ""
            val text = message.utf8Text ?: ""
            if (text.isNotEmpty()) CallNotif.text(app, from, text)
            emit(JSONObject().put("type", "text").put("from", from).toString())
        }

        override fun onCallStateChanged(core: Core, call: Call, state: Call.State?, message: String) {
            val peer = call.remoteAddress.username ?: ""
            when (state) {
                Call.State.IncomingReceived -> {
                    answeredIncoming = false; declinedIncoming = false
                    // ring + vibrate like a normal phone, and show the call over the lock screen
                    Ringer.start(app)
                    CallNotif.incoming(app, peer)
                    // pop the call screen up over whatever is open (needs "Display over other apps",
                    // or the app already on screen); otherwise the notification / lock-screen call shows
                    if (uiVisible || android.provider.Settings.canDrawOverlays(app)) runCatching {
                        app.startActivity(CallNotif.phoneIntent(app).putExtra(CallNotif.EXTRA_INCOMING, true))
                    }
                }
                Call.State.Connected, Call.State.StreamsRunning -> {
                    Ringer.stop(); CallNotif.cancelIncoming(app)
                    if (call.dir == Call.Dir.Incoming) answeredIncoming = true
                    VoiceService.instance?.foreground(true)
                }
                Call.State.OutgoingInit -> VoiceService.instance?.foreground(true)
                Call.State.End, Call.State.Error, Call.State.Released -> {
                    Ringer.stop(); CallNotif.cancelIncoming(app)
                    if (state != Call.State.Released && call.dir == Call.Dir.Incoming && !answeredIncoming && !declinedIncoming)
                        CallNotif.missed(app, peer)
                    if (state == Call.State.Released) { answeredIncoming = false; declinedIncoming = false }
                    if (core.callsNb == 0) VoiceService.instance?.foreground(false)
                }
                else -> {}
            }
            if (state == Call.State.StreamsRunning && core.currentCall?.outputAudioDevice == null) {
                core.audioDevices.firstOrNull { it.type == AudioDevice.Type.Earpiece }?.let { call.outputAudioDevice = it }
            }
            val o = JSONObject(snapshot()).put("type", "call").put("event", state?.name ?: "").put("message", message)
            if (state == Call.State.End || state == Call.State.Error || state == Call.State.Released) {
                o.put("ended", true).put("reason", call.reason?.name ?: "")
            }
            emit(o.toString())
        }
    }

    private fun emit(json: String) { listener?.invoke(json) }
    private fun onMain(block: () -> Unit) { if (Looper.myLooper() == Looper.getMainLooper()) block() else main.post(block) }
}
