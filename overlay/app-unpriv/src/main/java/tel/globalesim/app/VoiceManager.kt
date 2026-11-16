package tel.globalesim.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import org.linphone.core.Account
import org.linphone.core.AudioDevice
import org.linphone.core.Call
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
    }

    fun logout() = onMain {
        app.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove("login").apply()
        core?.let { c ->
            c.currentCall?.terminate()
            c.accountList.forEach { c.removeAccount(it) }
            c.clearAllAuthInfo()
        }
        regState = "none"; emit(snapshot())
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

    fun call(number: String) = onMain {
        val c = core ?: return@onMain
        var d = number.filter { it.isDigit() }
        if (d.length == 10) d = "1$d"
        if (d.isEmpty()) return@onMain
        val addr = Factory.instance().createAddress("sip:$d@$domainFor") ?: return@onMain
        val params = c.createCallParams(null) ?: return@onMain
        params.mediaEncryption = MediaEncryption.SRTP
        params.isVideoEnabled = false
        c.inviteAddressWithParams(addr, params)
    }

    fun answer() = onMain { core?.currentCall?.accept() ?: core?.calls?.firstOrNull()?.accept() }
    fun decline() = onMain { (core?.currentCall ?: core?.calls?.firstOrNull())?.decline(Reason.Declined) }
    fun hangup() = onMain { (core?.currentCall ?: core?.calls?.firstOrNull())?.terminate() }
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
        val call = c?.currentCall ?: c?.calls?.firstOrNull()
        if (call != null) {
            o.put("call", JSONObject()
                .put("state", call.state?.name ?: "")
                .put("dir", if (call.dir == Call.Dir.Incoming) "in" else "out")
                .put("peer", call.remoteAddress.username ?: "")
                .put("duration", call.duration)
                .put("muted", c?.isMicEnabled == false)
                .put("speaker", call.outputAudioDevice?.type == AudioDevice.Type.Speaker))
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
            emit(snapshot())
        }

        override fun onCallStateChanged(core: Core, call: Call, state: Call.State?, message: String) {
            if (state == Call.State.IncomingReceived) {
                // bring the app up so the customer can answer
                runCatching {
                    val i = android.content.Intent(app, StoreActivity::class.java)
                        .setData(android.net.Uri.parse("globalesim://phone"))
                        .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    app.startActivity(i)
                }
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
