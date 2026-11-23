package com.atip.mobile

import org.json.JSONObject
import java.time.DayOfWeek
import java.time.Duration
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime

/**
 * The 09:00 IST pre-open checklist notification (22 Nov 2026): when it comes and what it says. Uses no Android
 * classes (java.time and org.json only), so it can be checked on a plain JVM, as StatusText.
 *
 * All times are IST (Asia/Kolkata), whatever the phone's own time zone. Weekdays only; an NSE holiday is skipped
 * by the server's reply ("trading_day": false), not here.
 */
object PreOpenRules {
    val IST: ZoneId = ZoneId.of("Asia/Kolkata")

    /** The window opens 30 s after the server's 09:00 IST pre-open run starts. */
    val START: LocalTime = LocalTime.of(9, 0, 30)

    /** Inexact, so no exact-alarm permission is needed. Android 12+ stretches a shorter inexact window to 10
     *  minutes anyway, so the notification comes between 09:00:30 and 09:10:30 IST (later only if the phone has
     *  lain untouched in Doze, which defers inexact alarms to its next maintenance window). */
    val WINDOW: Duration = Duration.ofMinutes(10)

    /** The 09:00 run lands about 40 s after 09:00: a reply not ready yet is asked again once, this much later. */
    val RETRY: Duration = Duration.ofMinutes(2)

    /** Shown when the checklist is not ready after the retry, or ATIP cannot be reached (or wants a sign-in). */
    const val GENERIC_TITLE = "ATIP pre-open checklist"
    const val GENERIC_BODY = "Tap to open (data not ready yet)"

    fun isWeekday(d: LocalDate) = d.dayOfWeek != DayOfWeek.SATURDAY && d.dayOfWeek != DayOfWeek.SUNDAY

    /**
     * The start of the next window: today's while it is still open and today has had no notification yet
     * ([lastFired], the IST date "yyyy-MM-dd" of the last one), else the next weekday's. A start already past
     * means "now": the phone came back on, or the app was opened, inside today's window before it fired.
     */
    fun nextStart(now: ZonedDateTime, lastFired: String?): ZonedDateTime {
        val ist = now.withZoneSameInstant(IST)
        var day = ist.toLocalDate()
        while (true) {
            val start = ZonedDateTime.of(day, START, IST)
            if (isWeekday(day) && day.toString() != lastFired && ist.isBefore(start.plus(WINDOW))) return start
            day = day.plusDays(1)
        }
    }

    /** What one reply of GET /api/preopen?compact=1 means for the notification. */
    sealed class Outcome {
        /** Not a trading day (an NSE holiday): no notification. */
        object Skip : Outcome()
        /** Today's checklist: the server's own title and body. */
        data class Post(val title: String, val body: String) : Outcome()
        /** Not ready (before the 09:00 run, no notify text, or another day's checklist): ask again, then generic. */
        object NotReady : Outcome()
    }

    /** [today] is the IST date; a reply whose "date" is another day is not today's checklist. */
    fun decide(js: JSONObject, today: LocalDate): Outcome {
        if (!js.optBoolean("trading_day", true)) return Outcome.Skip
        if (!js.optBoolean("ready", false)) return Outcome.NotReady
        val date = str(js, "date")
        if (date != null && date != today.toString()) return Outcome.NotReady
        val note = js.optJSONObject("notify") ?: return Outcome.NotReady
        val title = str(note, "title")?.takeIf { it.isNotBlank() } ?: return Outcome.NotReady
        return Outcome.Post(title, str(note, "body") ?: "")
    }

    /** A string field, or null when it is missing or JSON null (optString would give the text "null"). */
    private fun str(js: JSONObject, key: String): String? = if (js.isNull(key)) null else js.optString(key)
}
