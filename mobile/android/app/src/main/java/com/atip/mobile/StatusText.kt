package com.atip.mobile

import org.json.JSONObject

/**
 * The banner's words and colours for one status reply: the same wording, colours and rules as the web banner
 * (dashboard/global_asia.py). UI fixes v2 (H10, 2026-11-03): a normal run -- RUNNING, kill switch off, not LIVE, no
 * manual override -- shows only the environment pill and a small "● Running" pill; the full-width strip is for what
 * is not normal (stopped, resuming, halted, LIVE money, a manual override, not reachable).
 * Uses no Android classes (org.json only), so it can be checked on a plain JVM.
 */
object StatusText {
    /**
     * What the banner shows. [strip] true: the full-width strip, [text] in [fg] on [bg]. [strip] false: the normal
     * state, only the pills; [text], [bg] and [fg] are then the small run pill's. [env] null: not known (older
     * server, or not reachable), so the environment pill is hidden.
     */
    data class Shown(val text: String, val bg: String, val fg: String, val env: String?, val strip: Boolean = true)

    /** Environment pill colours (design system); the pill always shows the word, never colour alone. */
    val ENV_COLOURS = mapOf("PAPER" to "#0E7490", "SANDBOX" to "#6941C6", "LIVE" to "#B42318")

    /** Before the first reply. */
    val CHECKING = Shown("Checking ATIP status…", "#334155", "#FFFFFF", null)

    /** Works for both replies: /api/v2/shell/state has env / halted / role, an older /api/status may not. */
    fun render(js: JSONObject): Shown {
        val status = str(js, "status") ?: ""
        val banner = str(js, "banner") ?: ""
        val env = str(js, "env")?.uppercase()?.takeIf { it in ENV_COLOURS }
        val halted = js.optBoolean("halted", false)
        val manual = js.optJSONObject("override")?.let { str(it, "mode") ?: "override" }   // null: no override
        val viewer = str(js, "role") == "VIEWER"
        if (status == "RUNNING" && !halted && env != "LIVE" && manual == null)
            return Shown("● Running" + (if (viewer) " · view only" else ""), "#064E3B", "#D1FAE5", env, strip = false)

        // the strip always carries a symbol (● running, ⏸ stopped, ↻ resuming, ⛔ halted), never colour alone
        val state = when (status) {
            "STOPPED" -> listOf("⏸ $banner", "read-only, no orders")
            "RESUMING" -> listOf("↻ $banner")
            "RUNNING" -> listOfNotNull("● $banner", "stops ${(str(js, "next_stop") ?: "").takeLast(5)} IST",
                                       if (js.optBoolean("live_feeds", true)) null else "no live feeds today")
            else -> listOf(banner.ifBlank { "ATIP status unknown" })
        }
        val liveRun = env == "LIVE" && status == "RUNNING"
        val parts = when {
            halted -> listOf("⛔ Trading halted (kill switch on)" +
                    (str(js, "halt_reason")?.takeIf { it.isNotBlank() }?.let { ": $it" } ?: "")) +
                (if (status == "RUNNING") listOf("no order of any kind is placed until you resume it on Trade › Command Center")
                 else state)
            liveRun -> listOf("● LIVE: orders use real money") + state
            else -> state
        }.toMutableList()
        if (manual != null) parts += "manual $manual"
        if (viewer) parts += "view only"
        if (js.has("last_updated"))
            parts += "last updated " + (str(js, "last_updated")?.takeIf { it.isNotBlank() } ?: "—")
        val (bg, fg) = when {
            halted || liveRun -> "#7F1D1D" to "#FEE2E2"
            status == "STOPPED" -> "#7C2D12" to "#FFEDD5"
            status == "RESUMING" -> "#1E3A8A" to "#DBEAFE"
            status == "RUNNING" -> "#064E3B" to "#D1FAE5"
            else -> "#334155" to "#FFFFFF"
        }
        return Shown(parts.joinToString(" · "), bg, fg, env)
    }

    /** A string field, or null when it is missing or JSON null (optString would give the text "null"). */
    private fun str(js: JSONObject, key: String): String? = if (js.isNull(key)) null else js.optString(key)
}
