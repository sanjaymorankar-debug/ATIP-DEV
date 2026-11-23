package com.atip.mobile

import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import androidx.core.app.NotificationCompat
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.time.LocalDate
import java.time.ZonedDateTime
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * The 09:00 IST pre-open checklist notification (22 Nov 2026), Monday to Friday.
 *
 * An inexact AlarmManager window (PreOpenRules: 09:00:30-09:10:30 IST) wakes this receiver; it sets the next
 * weekday's alarm first, then asks the server GET {server}/api/preopen?compact=1 -- the address set in the app
 * (with its sub-path, e.g. https://host/atip), the web view's cookies (the sign-in) and its "ATIPApp/<version>"
 * user-agent marker.
 *   ready, with "notify"        -> the server's title and body ("ATIP pre-open: Bullish", ...)
 *   "trading_day": false        -> nothing (an NSE holiday)
 *   not ready, or no answer     -> asked once more 2 minutes later; then "ATIP pre-open checklist: tap to open
 *                                  (data not ready yet)"
 * Tapping it opens /preopen in the Home tab (MainActivity.EXTRA_PATH).
 *
 * The same receiver sets the alarm again after a reboot, an app update, or a time-zone or clock change (the
 * alarm is an absolute IST time, so a new phone time zone does not move it). It holds no credentials; it only
 * forwards the cookie the web view already has.
 */
class PreOpenReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val ctx = context.applicationContext
        when (intent.action) {
            ACTION_FIRE -> fire(ctx, retry = false)
            ACTION_RETRY -> fire(ctx, retry = true)
            else -> schedule(ctx)          // BOOT_COMPLETED, MY_PACKAGE_REPLACED, TIMEZONE_CHANGED, TIME_SET
        }
    }

    private fun fire(ctx: Context, retry: Boolean) {
        val today = LocalDate.now(PreOpenRules.IST)
        if (!retry) {
            prefs(ctx).edit().putString(KEY_LAST, today.toString()).apply()
            schedule(ctx)                  // the next weekday's first, whatever happens below
        }
        val nm = ctx.getSystemService(NotificationManager::class.java)
        // no network call when nothing could be shown (Android 13+: notifications not allowed)
        if (!PreOpenRules.isWeekday(today) || !nm.areNotificationsEnabled()) return
        val pending = goAsync()
        val done = AtomicBoolean(false)
        val finish = { outcome: PreOpenRules.Outcome? ->
            if (done.compareAndSet(false, true)) {
                try { act(ctx, nm, outcome, retry) } finally { pending.finish() }
            }
        }
        // a broadcast should finish within about 10 s: after 9 s the server counts as not answering
        Handler(Looper.getMainLooper()).postDelayed({ finish(null) }, 9_000)
        thread(name = "atip-preopen") {
            finish(try { fetch(ctx, today) } catch (e: Exception) { null })
        }
    }

    private fun act(ctx: Context, nm: NotificationManager, outcome: PreOpenRules.Outcome?, retry: Boolean) {
        when (outcome) {
            PreOpenRules.Outcome.Skip -> Unit
            is PreOpenRules.Outcome.Post -> post(ctx, nm, outcome.title, outcome.body)
            else -> if (retry) post(ctx, nm, PreOpenRules.GENERIC_TITLE, PreOpenRules.GENERIC_BODY)
                    else alarms(ctx).setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP,
                             System.currentTimeMillis() + PreOpenRules.RETRY.toMillis(), alarm(ctx, ACTION_RETRY))
        }
    }

    /** The reply's meaning; null when the server answers non-200 (sign-in needed, an older server) or not JSON. */
    private fun fetch(ctx: Context, today: LocalDate): PreOpenRules.Outcome? {
        val url = MainActivity.serverOf(ctx) + "/api/preopen?compact=1"
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = 4000; c.readTimeout = 4000
        c.setRequestProperty("Accept", "application/json")
        // the web view's marker, so the server tells the app apart as it does for the pages
        c.setRequestProperty("User-Agent", (System.getProperty("http.agent") ?: "Android") +
                                           " ATIPApp/" + BuildConfig.VERSION_NAME)
        val cookie = try { CookieManager.getInstance().getCookie(url) } catch (e: Exception) { null }
        if (!cookie.isNullOrBlank()) c.setRequestProperty("Cookie", cookie)
        return try {
            if (c.responseCode != 200) null
            else PreOpenRules.decide(JSONObject(c.inputStream.bufferedReader().use { it.readText() }), today)
        } finally {
            c.disconnect()
        }
    }

    private fun post(ctx: Context, nm: NotificationManager, title: String, body: String) {
        nm.createNotificationChannel(NotificationChannel(CHANNEL, ctx.getString(R.string.preopen_channel),
                                                         NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = ctx.getString(R.string.preopen_channel_text)
        })
        val open = PendingIntent.getActivity(ctx, 0, MainActivity.openIntent(ctx, PAGE),
                                             PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        nm.notify(NOTIFICATION_ID, NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_notify_preopen)
            .setColor(ctx.getColor(R.color.ax_brand))
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build())
    }

    companion object {
        private const val ACTION_FIRE = "com.atip.mobile.PREOPEN"
        private const val ACTION_RETRY = "com.atip.mobile.PREOPEN_RETRY"
        private const val CHANNEL = "preopen"
        private const val NOTIFICATION_ID = 900
        private const val KEY_LAST = "preopen_last"     // IST date of the last 09:00 alarm
        /** The full checklist on the server (a web screen, opened in the Home tab). */
        const val PAGE = "/preopen"

        /** Sets (or moves) the one daily alarm to the next window; safe to call any time. */
        fun schedule(ctx: Context) {
            val start = PreOpenRules.nextStart(ZonedDateTime.now(PreOpenRules.IST), prefs(ctx).getString(KEY_LAST, null))
            alarms(ctx).setWindow(AlarmManager.RTC_WAKEUP, start.toInstant().toEpochMilli(),
                                  PreOpenRules.WINDOW.toMillis(), alarm(ctx, ACTION_FIRE))
        }

        private fun alarms(ctx: Context) = ctx.getSystemService(AlarmManager::class.java)

        private fun alarm(ctx: Context, action: String): PendingIntent =
            PendingIntent.getBroadcast(ctx, if (action == ACTION_FIRE) 1 else 2,
                                       Intent(ctx, PreOpenReceiver::class.java).setAction(action),
                                       PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)

        private fun prefs(ctx: Context) = ctx.getSharedPreferences("atip", Context.MODE_PRIVATE)
    }
}
