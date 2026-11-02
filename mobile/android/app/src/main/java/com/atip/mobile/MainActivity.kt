package com.atip.mobile

import android.annotation.SuppressLint
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.TextUtils
import android.view.Gravity
import android.view.Menu
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.bottomnavigation.BottomNavigationView
import com.google.android.material.navigation.NavigationBarView
import org.json.JSONException
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * ATIP for Android (2026-10-26; tabs and banner 2026-11-02): a native shell around ATIP's own mobile screens.
 *
 *   Home      /home      Today: is ATIP safe, running, making money
 *   Trade     /command   Command Center (UXR-01): halt switch, risk budget, pending confirmations
 *   Sessions  /sessions  the 10 trading sessions S1-S10: gross P&L vs actual P&L after charges and estimated tax
 *   Reports   /reports   periodic reports and the archive
 *   More      /more      every other section: Markets, Research, Accounting, Strategy Lab, Wealth, Settings
 *
 * The web view adds "ATIPApp/<version>" to its user agent, so the page shell hides its own left rail / bottom
 * tab bar: the tabs here are native. Pull down to reload the page on screen, tap the open tab again to go back
 * to its page; Back goes back inside the tab, then leaves the app.
 *
 * The banner on top is native: it polls GET /api/v2/shell/state every 60 s (GET /api/status on an older
 * server) and shows the environment badge (PAPER / SANDBOX / LIVE; hidden when the server does not say) next
 * to STOPPED (21:00-06:00 IST, "ATIP stopped — resumes 06:00 IST"), RESUMING or RUNNING, "Kill switch ON"
 * while trading is halted, and the last-updated time, so the state is visible even when a web screen fails
 * to load. Long-press the banner to change the server address.
 * The app holds no credentials and places no orders itself; every write goes through the web pages,
 * which carry the dashboard token and are refused (HTTP 423) while ATIP is stopped.
 */
class MainActivity : AppCompatActivity() {
    private class Tab(val path: String, val label: Int, val icon: Int)
    private val tabs = listOf(Tab("/home", R.string.tab_home, R.drawable.ic_tab_home),
                              Tab("/command", R.string.tab_trade, R.drawable.ic_tab_trade),
                              Tab("/sessions", R.string.tab_sessions, R.drawable.ic_tab_sessions),
                              Tab("/reports", R.string.tab_reports, R.drawable.ic_tab_reports),
                              Tab("/more", R.string.tab_more, R.drawable.ic_tab_more))
    private lateinit var web: WebView
    private lateinit var banner: LinearLayout
    private lateinit var bannerText: TextView
    private lateinit var envBadge: TextView
    private lateinit var refresh: SwipeRefreshLayout
    private val main = Handler(Looper.getMainLooper())
    private var current = 0
    private var newTab = false                  // the next finished page starts the tab's Back history afresh

    // semibold: weight 600 from Android 9, the "medium" face before that
    private val semibold: Typeface = if (Build.VERSION.SDK_INT >= 28) Typeface.create(Typeface.DEFAULT, 600, false)
                                     else Typeface.create("sans-serif-medium", Typeface.NORMAL)

    private val server: String
        get() = getSharedPreferences("atip", MODE_PRIVATE).getString("server", BuildConfig.DEFAULT_SERVER)!!
            .trimEnd('/')

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        envBadge = TextView(this).apply {                         // a rounded pill; coloured in show()
            textSize = 12f; typeface = semibold; setTextColor(Color.WHITE); visibility = View.GONE
            setPadding(dp(8), dp(2), dp(8), dp(2))
        }
        bannerText = TextView(this).apply {
            textSize = 13f; typeface = semibold; maxLines = 3; ellipsize = TextUtils.TruncateAt.END
            setTextColor(Color.WHITE); text = "Checking ATIP status…"
        }
        banner = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(16), dp(6), dp(16), dp(6)); minimumHeight = dp(44)       // >= 44dp touch target
            setBackgroundColor(Color.parseColor("#334155"))
            addView(envBadge, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { marginEnd = dp(8) })
            addView(bannerText, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            setOnLongClickListener { askServer(); true }
            contentDescription = "ATIP operating status"
        }
        val nav = BottomNavigationView(this).apply {
            labelVisibilityMode = NavigationBarView.LABEL_VISIBILITY_LABELED
            // item ids 1-5 (0 is Menu.NONE, "no id")
            tabs.forEachIndexed { i, t -> menu.add(Menu.NONE, i + 1, i, t.label).setIcon(t.icon) }
            setOnItemSelectedListener { current = it.itemId - 1; load(); true }
            setOnItemReselectedListener { load() }                // the open tab tapped again: back to its page
        }
        web = WebView(this).apply {
            settings.javaScriptEnabled = true          // the ATIP pages render from their own APIs
            settings.domStorageEnabled = true          // /m keeps its last-good copy for offline use
            // "ATIPApp/" makes the page shell hide its own left rail / bottom tab bar: the tabs here are native
            settings.userAgentString = settings.userAgentString + " ATIPApp/" + BuildConfig.VERSION_NAME
            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView, url: String) {
                    refresh.isRefreshing = false
                    if (newTab) { newTab = false; view.clearHistory() }     // Back stays inside the open tab
                }
            }
        }
        refresh = SwipeRefreshLayout(this).apply {
            addView(web)
            // reloads the page on screen (a section opened from More stays open); nothing loaded yet: the tab's page
            setOnRefreshListener { if (web.url == null) load() else web.reload(); pollStatus() }
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#0F172A"))     // behind the status bar when drawn edge to edge
            addView(banner, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(refresh, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
            addView(nav, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        }
        setContentView(root)
        // Android 15 draws targetSdk-35 apps edge to edge: keep the banner below the status bar; the bottom bar
        // pads itself above the navigation bar. Older versions pass no bar insets here, so nothing changes there.
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.setPadding(bars.left, bars.top, bars.right, 0)
            WindowInsetsCompat.Builder(insets)
                .setInsets(WindowInsetsCompat.Type.systemBars(), Insets.of(0, 0, 0, bars.bottom)).build()
        }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) { web.goBack(); return }
                isEnabled = false; onBackPressedDispatcher.onBackPressed(); isEnabled = true   // leave, as before
            }
        })
        load()
        schedulePoll()
    }

    /** Opens the current tab's own page. */
    private fun load() {
        newTab = true
        web.loadUrl(server + tabs[current].path)
    }

    private fun schedulePoll() {
        pollStatus()
        main.postDelayed({ schedulePoll() }, 60_000)
    }

    private fun pollStatus() = thread {
        val shown = try {
            // the page shell's state (2026-11-02); a server older than that answers non-200: use /api/status
            val js = getJson("$server/api/v2/shell/state") ?: getJson("$server/api/status")
                ?: error("no status reply")
            StatusText.render(js)
        } catch (e: Exception) {
            StatusText.Shown("ATIP not reachable at $server — long-press to change", "#78350F", "#FDE68A", null)
        }
        main.post { show(shown) }
    }

    /** One GET as a JSON object; null when the server answers non-200 or not JSON. Throws when it is unreachable. */
    private fun getJson(url: String): JSONObject? {
        val c = URL(url).openConnection() as HttpURLConnection
        c.connectTimeout = 8000; c.readTimeout = 8000
        return try {
            if (c.responseCode != 200) null
            else JSONObject(c.inputStream.bufferedReader().use { it.readText() })
        } catch (e: JSONException) {
            null
        } finally {
            c.disconnect()
        }
    }

    private fun show(s: StatusText.Shown) {
        bannerText.text = s.text
        bannerText.setTextColor(Color.parseColor(s.fg))
        banner.setBackgroundColor(Color.parseColor(s.bg))
        val env = s.env
        if (env == null) {
            envBadge.visibility = View.GONE                      // not known (older server, or not reachable)
        } else {
            envBadge.text = env
            envBadge.typeface = if (env == "LIVE") Typeface.DEFAULT_BOLD else semibold
            envBadge.background = GradientDrawable().apply {
                cornerRadius = dp(12).toFloat(); setColor(Color.parseColor(StatusText.ENV_COLOURS.getValue(env)))
            }
            envBadge.visibility = View.VISIBLE
        }
        banner.contentDescription = "ATIP status: " + (env?.let { "$it environment. " } ?: "") + s.text
    }

    private fun askServer() {
        val input = EditText(this).apply { setText(server) }
        AlertDialog.Builder(this).setTitle(R.string.server_title).setView(input)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                getSharedPreferences("atip", MODE_PRIVATE).edit().putString("server", input.text.toString().trim()).apply()
                load(); pollStatus()
            }.setNegativeButton(android.R.string.cancel, null).show()
    }

    override fun onDestroy() { main.removeCallbacksAndMessages(null); super.onDestroy() }
}

/** The banner's words and colours for one status reply (same wording and colours as the web banner). */
object StatusText {
    /** What the banner shows: the line, its background and text colours, and the environment (null: not known). */
    data class Shown(val text: String, val bg: String, val fg: String, val env: String?)

    /** Environment badge colours (design system); the badge always shows the word, never colour alone. */
    val ENV_COLOURS = mapOf("PAPER" to "#0E7490", "SANDBOX" to "#6941C6", "LIVE" to "#B42318")

    /** Works for both replies: /api/v2/shell/state has env / halted / role, an older /api/status may not. */
    fun render(js: JSONObject): Shown {
        val status = str(js, "status") ?: ""
        val banner = str(js, "banner") ?: ""
        // the state always carries a symbol (● running, ⏸ stopped, ↻ resuming), never colour alone
        val parts = when (status) {
            "STOPPED" -> mutableListOf("⏸ $banner", "read-only, no orders")
            "RESUMING" -> mutableListOf("↻ $banner")
            "RUNNING" -> mutableListOf("● $banner", "stops ${(str(js, "next_stop") ?: "").takeLast(5)} IST")
                .apply { if (!js.optBoolean("live_feeds", true)) add("no live feeds today") }
            else -> mutableListOf(banner.ifBlank { "ATIP status unknown" })
        }
        if (js.optBoolean("halted", false))
            parts.add(1, "Kill switch ON" + (str(js, "halt_reason")?.takeIf { it.isNotBlank() }?.let { " — $it" } ?: ""))
        if (str(js, "role") == "VIEWER") parts.add("view only")
        if (js.has("last_updated"))
            parts.add("last updated " + (str(js, "last_updated")?.takeIf { it.isNotBlank() } ?: "—"))
        val (bg, fg) = when (status) {
            "STOPPED" -> "#7C2D12" to "#FFEDD5"
            "RESUMING" -> "#1E3A8A" to "#DBEAFE"
            "RUNNING" -> "#064E3B" to "#D1FAE5"
            else -> "#334155" to "#FFFFFF"
        }
        val env = str(js, "env")?.uppercase()?.takeIf { it in ENV_COLOURS }
        return Shown(parts.joinToString(" · "), bg, fg, env)
    }

    /** A string field, or null when it is missing or JSON null (optString would give the text "null"). */
    private fun str(js: JSONObject, key: String): String? = if (js.isNull(key)) null else js.optString(key)
}
