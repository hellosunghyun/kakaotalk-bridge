// Kakao notification reply and media share follow the pinned Iris Replier (GPL-3.0).
// See NOTICE.md. No upstream conflated queue, mutable config, or body logging.
// Media staging matches Iris IMAGE_DIR and MediaPayload.buildSendIntent (2026-10-24):
// ACTION_SEND / ACTION_SEND_MULTIPLE, key_id, key_type, key_from_direct_share.
package party.qwer.iris

import android.app.RemoteInput
import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.IBinder
import android.util.Xml
import org.xmlpull.v1.XmlPullParser
import java.io.File

object CollectorSender {
    private const val SERVICE = "com.kakao.talk.notification.NotificationActionService"
    private const val SHARE = "com.kakao.talk.activity.RecentExcludeIntentFilterActivity"
    const val STAGE = "/sdcard/Android/data/com.kakao.talk/files"
    private const val MAX_BYTES = 8L * 1024 * 1024
    private val KINDS = setOf("image", "video", "audio", "file")
    private val LOCKED = arrayOf(
        "mDreamingLockscreen=true", "isKeyguardShowing=true", "mShowingLockscreen=true"
    )
    private val UNLOCKED = arrayOf(
        "mDreamingLockscreen=false", "isKeyguardShowing=false", "mShowingLockscreen=false"
    )

    fun prepare(chat: Long, text: String, threadId: Long? = null): Intent {
        require(chat > 0 && text.isNotBlank() && text.length <= 4000 && !text.contains('\u0000'))
        if (threadId != null) require(threadId > 0)
        val file = File("/data/user/0/com.kakao.talk/shared_prefs/KakaoTalk.hw.perferences.xml")
        require(file.length() in 1L..1048576L)
        val referer = file.inputStream().use { input ->
            val parser = Xml.newPullParser()
            parser.setInput(input, "UTF-8")
            var value: String? = null
            while (parser.next() != XmlPullParser.END_DOCUMENT) {
                if (parser.eventType == XmlPullParser.START_TAG && parser.name == "string"
                    && parser.getAttributeValue(null, "name") == "NotificationReferer") {
                    value = parser.nextText()
                    break
                }
            }
            require(!value.isNullOrBlank())
            value
        }
        return Intent("com.kakao.talk.notification.REPLY_MESSAGE").apply {
            component = ComponentName("com.kakao.talk", SERVICE)
            putExtra("noti_referer", referer)
            putExtra("chat_id", chat)
            // threadId is KakaoTalk's notification thread, not a type-26 quote.
            putExtra("is_chat_thread_notification", threadId != null)
            if (threadId != null) putExtra("thread_id", threadId)
            RemoteInput.addResultsToIntent(arrayOf(RemoteInput.Builder("reply_message").build()),
                this, Bundle().apply { putCharSequence("reply_message", text) })
        }
    }

    /** A file this process already staged under KakaoTalk's external files dir. */
    fun stagedFile(name: String, kind: String): File {
        require(kind in KINDS)
        require(name.length in 4..80 && name.startsWith("kb-") && !name.contains(".."))
        require(!name.contains('/') && !name.contains('\\') && !name.contains('\u0000'))
        require(name.none { it.code < 32 })
        val root = File(STAGE).canonicalFile
        val file = File(root, name).canonicalFile
        require(file.parentFile == root && file.isFile && file.length() in 1..MAX_BYTES)
        return file
    }

    fun prepareMedia(chat: Long, files: List<Pair<File, String>>): Intent {
        require(chat > 0 && files.isNotEmpty() && files.size <= 10)
        val kind = files.first().second
        require(files.all { it.second == kind && it.second in KINDS })
        val uris = ArrayList<Uri>(files.size)
        for ((file, _) in files) uris.add(Uri.fromFile(file))
        val mime = when (kind) {
            "image" -> "image/*"
            "video" -> "video/*"
            "audio" -> "audio/*"
            // `*/*` is forwarded to the main screen and never sent on 26.9.1;
            // `application/octet-stream` is sent as a file (type 18) to `key_id`.
            else -> "application/octet-stream"
        }
        val action = if (uris.size == 1) Intent.ACTION_SEND else Intent.ACTION_SEND_MULTIPLE
        return Intent(action).apply {
            // Explicit: on 26.9.1 `*/*` and `text/plain` match two KakaoTalk activities,
            // which opens the system chooser instead of sending.
            component = ComponentName("com.kakao.talk", SHARE)
            type = mime
            if (uris.size == 1) putExtra(Intent.EXTRA_STREAM, uris[0])
            else putParcelableArrayListExtra(Intent.EXTRA_STREAM, uris)
            putExtra("key_id", chat)
            putExtra("key_type", 1)
            putExtra("key_from_direct_share", true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        }
    }

    /**
     * KakaoTalk 26.9+ drops a share that arrives while the screen is off.
     * Wake only. A lock screen is a hard failure; this does not dismiss it.
     */
    fun ensureAwake() {
        val window = exec("dumpsys", "window")
        if (LOCKED.any { it in window }) throw IllegalStateException("screen_locked")
        if (UNLOCKED.none { it in window }) throw IllegalStateException("screen_state_unreadable")
        val power = exec("dumpsys", "power")
        if ("Display Power: state=OFF" in power || "mWakefulness=Asleep" in power || "mWakefulness=Dozing" in power) {
            exec("input", "keyevent", "224")
            Thread.sleep(400)
        }
    }

    fun submit(intent: Intent) {
        // Android 14 redroid: inspect the returned component instead of discarding errors.
        val binder = Class.forName("android.os.ServiceManager")
            .getMethod("getService", String::class.java).invoke(null, "activity")
        val manager = Class.forName("android.app.IActivityManager\$Stub")
            .getMethod("asInterface", IBinder::class.java).invoke(null, binder)
        val method = Class.forName("android.app.IActivityManager").getMethod("startService",
            Class.forName("android.app.IApplicationThread"), Intent::class.java,
            String::class.java, Boolean::class.javaPrimitiveType, String::class.java,
            String::class.java, Int::class.javaPrimitiveType)
        val component = method.invoke(manager, null, intent, null, false,
            "com.android.shell", null, 0) as? ComponentName
        require(component?.packageName == "com.kakao.talk" && component.className == SERVICE)
    }

    /**
     * A resolved activity-start binder call. Resolved before any IPC so a missing
     * method is a clean `failed` (`kakao_activity_unavailable`), never `unknown`.
     */
    class ActivityStarter internal constructor(
        private val target: Any,
        private val method: java.lang.reflect.Method,
        private val feature: Boolean,
        private val user: Boolean,
    ) {
        val signature: String get() = "${method.declaringClass.simpleName}.${method.name}/${method.parameterTypes.size}"

        /** Returns the framework result code (negative = rejected). */
        fun start(intent: Intent): Int {
            val args = ArrayList<Any?>(12)
            args.add(null)                       // IApplicationThread caller
            args.add("com.android.shell")        // callingPackage
            if (feature) args.add(null)          // callingFeatureId
            args.addAll(listOf(intent, intent.type, null, null, 0, 0, null, null))
            if (user) args.add(0)                // userId (USER_SYSTEM)
            val code = try {
                method.invoke(target, *args.toTypedArray())
            } catch (error: java.lang.reflect.InvocationTargetException) {
                throw error.targetException ?: error
            }
            return (code as? Int) ?: throw IllegalStateException("kakao_activity_rejected")
        }
    }

    /**
     * Android 14 (redroid) exposes no 12-argument `IActivityManager.startActivity`;
     * the 12-argument form is `startActivityAsUser(..., int userId)`. Signatures vary by
     * release, so match by name and parameter shape on IActivityManager, then
     * IActivityTaskManager.
     */
    fun activityStarter(): ActivityStarter {
        val thread = Class.forName("android.app.IApplicationThread")
        val profiler = Class.forName("android.app.ProfilerInfo")
        val int = Int::class.javaPrimitiveType
        val tail = listOf(Intent::class.java, String::class.java, IBinder::class.java,
            String::class.java, int, int, profiler, Bundle::class.java)
        fun shape(feature: Boolean, user: Boolean): List<Class<*>?> {
            val head = mutableListOf<Class<*>?>(thread, String::class.java)
            if (feature) head.add(String::class.java)
            head.addAll(tail)
            if (user) head.add(int)
            return head
        }
        val services = listOf(
            "activity" to "android.app.IActivityManager",
            "activity_task" to "android.app.IActivityTaskManager",
        )
        val names = listOf(
            "startActivityAsUserWithFeature" to true, "startActivityAsUser" to true,
            "startActivityWithFeature" to false, "startActivity" to false,
        )
        for ((name, user) in names) {
            for ((service, iface) in services) {
                val target = runCatching { binderInterface(service, iface) }.getOrNull() ?: continue
                val type = Class.forName(iface)
                for (feature in listOf(true, false)) {
                    val wanted = shape(feature, user)
                    val method = type.methods.firstOrNull {
                        it.name == name && it.parameterTypes.toList() == wanted
                    } ?: continue
                    return ActivityStarter(target, method, feature, user)
                }
            }
        }
        throw IllegalStateException("kakao_activity_unavailable")
    }

    private fun binderInterface(service: String, iface: String): Any {
        val binder = Class.forName("android.os.ServiceManager")
            .getMethod("getService", String::class.java).invoke(null, service)
            ?: throw IllegalStateException("kakao_activity_unavailable")
        return Class.forName("$iface\$Stub")
            .getMethod("asInterface", IBinder::class.java).invoke(null, binder)
            ?: throw IllegalStateException("kakao_activity_unavailable")
    }

    private fun exec(vararg args: String): String {
        val process = ProcessBuilder(*args).redirectErrorStream(true).start()
        val text = process.inputStream.bufferedReader().use { it.readText() }
        process.waitFor()
        return text
    }
}
