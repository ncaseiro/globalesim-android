package tel.globalesim.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Person
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.Ringtone
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator

/**
 * GlobaleSIM Phone in the background.
 *
 * Keeps the calling engine (VoiceManager / Linphone) alive and signed in while the app is
 * closed, so incoming calls ring like a normal phone: ringtone + vibration (following the
 * phone's ring / vibrate / silent switch), a full-screen call screen over the lock screen,
 * and Answer / Decline buttons in the notification. Missed calls leave a notification.
 *
 * Runs as a foreground service with a quiet "Ready for calls" notification (Android requires
 * one). During a call it also holds the microphone so audio keeps working if the customer
 * leaves the app. Started when the customer signs in to their line, and again after reboot.
 */
class VoiceService : Service() {

    companion object {
        @Volatile var instance: VoiceService? = null

        fun start(ctx: Context) {
            if (!VoiceManager.hasLogin(ctx)) return
            val i = Intent(ctx, VoiceService::class.java)
            runCatching { if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i) }
        }

        fun stop(ctx: Context) { runCatching { ctx.stopService(Intent(ctx, VoiceService::class.java)) } }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        CallNotif.channels(this)
        foreground(false)
        VoiceManager.init(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!VoiceManager.hasLogin(this)) { stopSelf(); return START_NOT_STICKY }
        foreground(VoiceManager.inCall())
        return START_STICKY
    }

    override fun onDestroy() {
        instance = null
        Ringer.stop()
        super.onDestroy()
    }

    /** (Re)post the foreground notification; [mic] = a call is up, keep the microphone. */
    fun foreground(mic: Boolean) {
        val n = CallNotif.line(this, VoiceManager.regText())
        if (Build.VERSION.SDK_INT >= 34) {
            val base = ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            val withMic = base or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            // The microphone type is only allowed while the app is on screen; fall back quietly.
            if (!(mic && runCatching { startForeground(CallNotif.ID_LINE, n, withMic) }.isSuccess))
                runCatching { startForeground(CallNotif.ID_LINE, n, base) }
        } else if (Build.VERSION.SDK_INT >= 29) {
            runCatching { startForeground(CallNotif.ID_LINE, n, if (mic) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0) }
                .onFailure { runCatching { startForeground(CallNotif.ID_LINE, n) } }
        } else {
            runCatching { startForeground(CallNotif.ID_LINE, n) }
        }
    }

    /** Line status changed (online / connecting / offline): refresh the quiet notification. */
    fun refresh() {
        runCatching { getSystemService(NotificationManager::class.java)?.notify(CallNotif.ID_LINE, CallNotif.line(this, VoiceManager.regText())) }
    }
}

/** Restart the line after the phone reboots or the app is updated. */
class VoiceBootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_MY_PACKAGE_REPLACED ||
            intent.action == "android.intent.action.LOCKED_BOOT_COMPLETED") VoiceService.start(ctx)
    }
}

/** "Decline" button on the incoming-call notification. */
class CallActionReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action == CallNotif.ACTION_DECLINE) {
            Ringer.stop()
            CallNotif.cancelIncoming(ctx)
            VoiceManager.init(ctx)
            VoiceManager.decline()
        }
    }
}

/**
 * Ringtone + vibration for incoming calls, following the ring / vibrate / silent switch.
 * The calling engine puts the phone in "call" audio mode as soon as a call arrives, which sends
 * sound to the small earpiece (a faint ring). We switch to ringtone mode (loudspeaker, ring volume)
 * and keep re-asserting it while ringing.
 */
object Ringer {
    private var player: MediaPlayer? = null
    private var tone: Ringtone? = null
    private var vib: Vibrator? = null
    private var focus: AudioFocusRequest? = null
    private var ringing = false
    private val main = Handler(Looper.getMainLooper())
    private lateinit var am: AudioManager

    private val attrs: AudioAttributes = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
        .build()

    private val keepRingMode = object : Runnable {
        override fun run() {
            if (!ringing) return
            runCatching { if (am.mode != AudioManager.MODE_RINGTONE) am.mode = AudioManager.MODE_RINGTONE }
            main.postDelayed(this, 700)
        }
    }

    @Suppress("DEPRECATION")
    fun start(ctx: Context) {
        stop()
        val app = ctx.applicationContext
        am = app.getSystemService(AudioManager::class.java) ?: return
        ringing = true
        val mode = am.ringerMode
        if (mode == AudioManager.RINGER_MODE_NORMAL) {
            runCatching { am.mode = AudioManager.MODE_RINGTONE }
            if (Build.VERSION.SDK_INT >= 26) runCatching {
                focus = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT).setAudioAttributes(attrs).build()
                am.requestAudioFocus(focus!!)
            }
            val uri: Uri = RingtoneManager.getActualDefaultRingtoneUri(app, RingtoneManager.TYPE_RINGTONE)
                ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            val ok = runCatching {
                player = MediaPlayer().apply {
                    setAudioAttributes(attrs)
                    setDataSource(app, uri)
                    isLooping = true
                    prepare()
                    start()
                }
            }.isSuccess
            if (!ok) runCatching {
                player = null
                tone = RingtoneManager.getRingtone(app, uri)?.also { t ->
                    t.audioAttributes = attrs
                    if (Build.VERSION.SDK_INT >= 28) t.isLooping = true
                    t.play()
                }
            }
            main.postDelayed(keepRingMode, 300)
        }
        if (mode != AudioManager.RINGER_MODE_SILENT) runCatching {
            val v = app.getSystemService(Vibrator::class.java)
            if (v != null && v.hasVibrator()) {
                val pattern = longArrayOf(0, 1000, 800)          // buzz… pause… repeat
                if (Build.VERSION.SDK_INT >= 26) v.vibrate(VibrationEffect.createWaveform(pattern, 0), attrs)
                else v.vibrate(pattern, 0)
                vib = v
            }
        }
    }

    fun stop() {
        val was = ringing
        ringing = false
        main.removeCallbacks(keepRingMode)
        runCatching { player?.stop() }; runCatching { player?.release() }; player = null
        runCatching { tone?.stop() }; tone = null
        runCatching { vib?.cancel() }; vib = null
        if (Build.VERSION.SDK_INT >= 26) runCatching { focus?.let { am.abandonAudioFocusRequest(it) } }
        focus = null
        // hand the audio back: the call engine sets "call" mode itself if the call is answered
        if (was) runCatching { if (am.mode == AudioManager.MODE_RINGTONE) am.mode = AudioManager.MODE_NORMAL }
    }
}

/** All GlobaleSIM Phone notifications. */
object CallNotif {
    const val CH_LINE = "gs_phone_line"
    const val CH_CALL = "gs_phone_incoming"
    const val CH_MISSED = "gs_phone_missed"
    const val CH_TEXT = "gs_phone_texts"
    const val ID_LINE = 4101
    const val ID_CALL = 4102
    const val ID_MISSED = 4103
    const val ACTION_DECLINE = "tel.globalesim.app.DECLINE_CALL"
    const val EXTRA_INCOMING = "gs_incoming"
    const val EXTRA_ANSWER = "gs_answer"

    private const val FLAGS = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE

    fun channels(ctx: Context) {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(NotificationChannel(CH_LINE, "Phone line", NotificationManager.IMPORTANCE_LOW).apply {
            description = "Keeps your GlobaleSIM number ready to receive calls"
            setShowBadge(false)
        })
        nm.createNotificationChannel(NotificationChannel(CH_CALL, "Incoming calls", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Rings when someone calls your GlobaleSIM number"
            setSound(null, null)          // the ringtone is played by Ringer (it loops until answered)
            enableVibration(false)        // same for vibration
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
        })
        nm.createNotificationChannel(NotificationChannel(CH_MISSED, "Missed calls", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "Calls you didn't answer"
        })
        nm.createNotificationChannel(NotificationChannel(CH_TEXT, "Text messages", NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Texts sent to your GlobaleSIM number"
            enableVibration(true)
        })
    }

    @Suppress("DEPRECATION")
    private fun builder(ctx: Context, ch: String): Notification.Builder =
        if (Build.VERSION.SDK_INT >= 26) Notification.Builder(ctx, ch) else Notification.Builder(ctx)

    fun phoneIntent(ctx: Context): Intent =
        Intent(ctx, MainActivity::class.java)
            .setData(Uri.parse("globalesim://phone"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)

    fun fmt(num: String): String {
        var d = num.filter { it.isDigit() }
        if (d.length == 11 && d.startsWith("1")) d = d.substring(1)
        return if (d.length == 10) "(${d.substring(0, 3)}) ${d.substring(3, 6)}-${d.substring(6)}" else num.ifEmpty { "Unknown" }
    }

    /** The quiet always-on notification of the foreground service. */
    fun line(ctx: Context, status: String): Notification =
        builder(ctx, CH_LINE)
            .setSmallIcon(android.R.drawable.sym_action_call)
            .setContentTitle("GlobaleSIM Phone")
            .setContentText(status)
            .setOngoing(true)
            .setShowWhen(false)
            .setContentIntent(PendingIntent.getActivity(ctx, 40, phoneIntent(ctx), FLAGS))
            .build()

    fun incoming(ctx: Context, peer: String) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        val who = fmt(peer)
        val full = PendingIntent.getActivity(ctx, 41, phoneIntent(ctx).putExtra(EXTRA_INCOMING, true), FLAGS)
        val answer = PendingIntent.getActivity(ctx, 42, phoneIntent(ctx).putExtra(EXTRA_INCOMING, true).putExtra(EXTRA_ANSWER, true), FLAGS)
        val decline = PendingIntent.getBroadcast(ctx, 43, Intent(ctx, CallActionReceiver::class.java).setAction(ACTION_DECLINE), FLAGS)
        fun base() = builder(ctx, CH_CALL)
            .setSmallIcon(android.R.drawable.sym_call_incoming)
            .setContentTitle("Incoming call")
            .setContentText("$who · GlobaleSIM Phone")
            .setCategory(Notification.CATEGORY_CALL)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setOngoing(true)
            .setAutoCancel(false)
            .setContentIntent(full)
            .setFullScreenIntent(full, true)
            .setColor(0xFFFFB547.toInt())
            .also { if (Build.VERSION.SDK_INT < 26) @Suppress("DEPRECATION") it.setPriority(Notification.PRIORITY_MAX) }
        val styled = if (Build.VERSION.SDK_INT >= 31) runCatching {
            val person = Person.Builder().setName(who).setImportant(true).build()
            base().setStyle(Notification.CallStyle.forIncomingCall(person, decline, answer)).build()
        }.getOrNull() else null
        val n = styled ?: base()
            .addAction(Notification.Action.Builder(null as Icon?, "Decline", decline).build())
            .addAction(Notification.Action.Builder(null as Icon?, "Answer", answer).build())
            .build()
        runCatching { nm.notify(ID_CALL, n) }.onFailure {
            // e.g. a phone that rejects the call style: plain buttons instead
            runCatching {
                nm.notify(ID_CALL, base()
                    .addAction(Notification.Action.Builder(null as Icon?, "Decline", decline).build())
                    .addAction(Notification.Action.Builder(null as Icon?, "Answer", answer).build())
                    .build())
            }
        }
    }

    fun cancelIncoming(ctx: Context) {
        runCatching { ctx.getSystemService(NotificationManager::class.java)?.cancel(ID_CALL) }
    }

    /** One notification per sender (a new text from the same person replaces the old one). */
    fun text(ctx: Context, peer: String, body: String) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        val open = PendingIntent.getActivity(ctx, 45, phoneIntent(ctx).putExtra("gs_texts", true), FLAGS)
        runCatching {
            nm.notify(5000 + (peer.hashCode() and 0xFFF), builder(ctx, CH_TEXT)
                .setSmallIcon(android.R.drawable.sym_action_chat)
                .setContentTitle("Text from ${fmt(peer)}")
                .setContentText(body)
                .setStyle(Notification.BigTextStyle().bigText(body))
                .setCategory(Notification.CATEGORY_MESSAGE)
                .setAutoCancel(true)
                .setContentIntent(open)
                .also { if (Build.VERSION.SDK_INT < 26) @Suppress("DEPRECATION") it.setPriority(Notification.PRIORITY_HIGH) }
                .build())
        }
    }

    fun missed(ctx: Context, peer: String) {
        val nm = ctx.getSystemService(NotificationManager::class.java) ?: return
        runCatching {
            nm.notify(ID_MISSED, builder(ctx, CH_MISSED)
                .setSmallIcon(android.R.drawable.sym_call_missed)
                .setContentTitle("Missed call")
                .setContentText(fmt(peer))
                .setCategory(Notification.CATEGORY_MISSED_CALL)
                .setAutoCancel(true)
                .setContentIntent(PendingIntent.getActivity(ctx, 44, phoneIntent(ctx), FLAGS))
                .build())
        }
    }
}
