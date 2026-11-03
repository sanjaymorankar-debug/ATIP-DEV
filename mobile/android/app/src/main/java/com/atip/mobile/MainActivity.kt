package com.atip.mobile

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
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
import android.webkit.JsResult
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
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
import androidx.core.view.WindowInsetsControllerCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.bottomnavigation.BottomNavigationView
import com.google.android.material.navigation.NavigationBarView
import org.json.JSONException
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * ATIP for Android (2026-10-26; tabs and banner 2026-11-02; UI fixes v2 2026-11-03): a native shell around ATIP's
 * own mobile screens.
 *
 *   Home       /home       Today: is ATIP safe, running, making money
 *   Watchlist  /watchlist  the symbols you follow; a symbol's chart and market depth (/stock/<SYMBOL>) open in the tab
 *   Trade      /command    Command Center (UXR-01): halt switch, risk budget, pending confirmations, order ticket
 *   Sessions   /sessions   the 10 trading sessions S1-S10: gross P&L vs actual P&L after charges and estimated tax
 *   More       /more       every other section: Markets, Research, Reports, Accounting, Strategy Lab, Wealth, Settings
 *
 * The web view adds "ATIPApp/<version>" to its user agent, so the page shell hides its own left rail / bottom
 * tab bar and its run pills: the tabs and the banner here are native. Pull down to reload the page on screen, tap
 * the open tab again to go back to its page; Back goes back inside the tab, then leaves the app. ATIP's own links
 * (/stock/<SYMBOL> included) open inside the tab; a link tapped to another site opens in the phone's browser.
 *
 * The banner on top is native: it polls GET /api/v2/shell/state every 60 s (GET /api/status on an older
 * server). Normal (RUNNING, kill switch off, not LIVE, no manual override): only the environment pill and a small
 * "● Running" pill. Anything else gets the full-width strip: STOPPED (21:00-06:00 IST), RESUMING, the kill switch,
 * LIVE money, a manual override, or ATIP not reachable -- so the state is visible even when a web screen fails to
 * load. Long-press the banner to change the server address. Colours follow the web tokens, light or dark.
 * The app holds no credentials and places no orders itself; every write goes through the web pages,
 * which carry the dashboard token and are refused (HTTP 423) while ATIP is stopped.
 */
class MainActivity : AppCompatActivity() {
    private class Tab(val path: String, val label: Int, val icon: Int)
    private val tabs = listOf(Tab("/home", R.string.tab_home, R.drawable.ic_tab_home),
                              Tab("/watchlist", R.string.tab_watchlist, R.drawable.ic_tab_watchlist),
                              Tab("/command", R.string.tab_trade, R.drawable.ic_tab_trade),
                              Tab("/sessions", R.string.tab_sessions, R.drawable.ic_tab_sessions),
                              Tab("/more", R.string.tab_more, R.drawable.ic_tab_more))
    private lateinit var root: LinearLayout
    private lateinit var web: WebView
    private lateinit var banner: LinearLayout
    private lateinit var bannerText: TextView
    private lateinit var envBadge: TextView
    private lateinit var runPill: TextView
    private lateinit var bannerRule: View
    private lateinit var navRule: View
    private lateinit var nav: BottomNavigationView
    private lateinit var refresh: SwipeRefreshLayout
    private val main = Handler(Looper.getMainLooper())
    private var current = 0
    private var newTab = false                  // the next finished page starts the tab's Back history afresh
    private var shown: StatusText.Shown? = null // the banner on screen, drawn again when light / dark switches

    // semibold: weight 600 from Android 9, the "medium" face before that
    private val semibold: Typeface = if (Build.VERSION.SDK_INT >= 28) Typeface.create(Typeface.DEFAULT, 600, false)
                                     else Typeface.create("sans-serif-medium", Typeface.NORMAL)

    private val server: String
        get() = getSharedPreferences("atip", MODE_PRIVATE).getString("server", BuildConfig.DEFAULT_SERVER)!!
            .trimEnd('/')

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    /** A web token (values/tokens.xml; values-night/tokens.xml in the dark theme). */
    private fun tok(id: Int) = getColor(id)

    /** A rounded pill background (the environment pill and the run pill). */
    private fun pill(colour: String) = GradientDrawable().apply {
        cornerRadius = dp(12).toFloat(); setColor(Color.parseColor(colour))
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        envBadge = TextView(this).apply {                         // a rounded pill; coloured in show()
            textSize = 12f; typeface = semibold; setTextColor(Color.WHITE); visibility = View.GONE
            setPadding(dp(8), dp(2), dp(8), dp(2))
        }
        runPill = TextView(this).apply {                          // the normal state's "● Running" (H10)
            textSize = 12f; typeface = semibold; visibility = View.GONE
            setPadding(dp(8), dp(2), dp(8), dp(2))
        }
        bannerText = TextView(this).apply {                       // the full-width strip's words
            textSize = 13f; typeface = semibold; maxLines = 3; ellipsize = TextUtils.TruncateAt.END
        }
        banner = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(16), dp(6), dp(16), dp(6)); minimumHeight = dp(44)       // >= 44dp touch target
            addView(envBadge, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { marginEnd = dp(8) })
            addView(runPill, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT))
            addView(bannerText, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            setOnLongClickListener { askServer(); true }
            contentDescription = "ATIP operating status"
        }
        bannerRule = View(this)                                   // under the compact row, as under the web top bar
        navRule = View(this)                                      // above the tabs, as above the web tab bar
        nav = BottomNavigationView(this).apply {
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
                /** ATIP's own pages -- /stock/<SYMBOL> (chart and market depth), a section opened from More -- open
                 *  in this tab, as do redirects, frames and script navigations; only a link the user taps to
                 *  another site opens in the phone's browser (inside the tab if there is none). */
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val u = request.url
                    val scheme = u.scheme?.lowercase()
                    if ((scheme != "http" && scheme != "https") || !request.isForMainFrame || request.isRedirect ||
                        !request.hasGesture() || isAtip(u, view.url)) return false
                    return try { startActivity(Intent(Intent.ACTION_VIEW, u)); true }
                           catch (e: ActivityNotFoundException) { false }
                }
                override fun onPageFinished(view: WebView, url: String) {
                    refresh.isRefreshing = false
                    if (newTab) { newTab = false; view.clearHistory() }     // Back stays inside the open tab
                }
            }
            // alert() and confirm() from the pages (the order ticket asks before it sends): native dialogs.
            // <dialog> elements (the bottom-sheet ticket) are drawn by the page itself.
            webChromeClient = object : WebChromeClient() {
                override fun onJsAlert(view: WebView, url: String?, message: String?, result: JsResult): Boolean {
                    AlertDialog.Builder(this@MainActivity).setMessage(message)
                        .setPositiveButton(android.R.string.ok) { _, _ -> result.confirm() }
                        .setOnCancelListener { result.confirm() }.show()
                    return true
                }
                override fun onJsConfirm(view: WebView, url: String?, message: String?, result: JsResult): Boolean {
                    AlertDialog.Builder(this@MainActivity).setMessage(message)
                        .setPositiveButton(android.R.string.ok) { _, _ -> result.confirm() }
                        .setNegativeButton(android.R.string.cancel) { _, _ -> result.cancel() }
                        .setOnCancelListener { result.cancel() }.show()
                    return true
                }
            }
        }
        refresh = SwipeRefreshLayout(this).apply {
            addView(web)
            // reloads the page on screen (a section opened from More stays open); nothing loaded yet: the tab's page
            setOnRefreshListener { if (web.url == null) load() else web.reload(); pollStatus() }
        }
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(banner, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(bannerRule, LinearLayout.LayoutParams(MATCH_PARENT, dp(1)))
            addView(refresh, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
            addView(navRule, LinearLayout.LayoutParams(MATCH_PARENT, dp(1)))
            addView(nav, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        }
        setContentView(root)
        show(StatusText.CHECKING)
        applyColours()
        // Android 15 draws targetSdk-35 apps edge to edge: keep the banner below the status bar; the bottom bar
        // pads itself above the navigation bar. While the keyboard is up (the order ticket's fields) the screen ends
        // above it, as adjustResize does on older versions -- Android 15 leaves that to the app. Older versions pass
        // no bar or keyboard insets here (the window does it), so nothing changes there.
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            val keyboard = ime.bottom > bars.bottom
            v.setPadding(bars.left, bars.top, bars.right, if (keyboard) ime.bottom else 0)
            WindowInsetsCompat.Builder(insets)
                .setInsets(WindowInsetsCompat.Type.systemBars(), Insets.of(0, 0, 0, if (keyboard) 0 else bars.bottom))
                .setInsets(WindowInsetsCompat.Type.ime(), Insets.NONE).build()
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

    /** Light / dark switched: the manifest keeps uiMode here, so the page on screen is not reloaded. */
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        applyColours()
    }

    /** The native parts in the web tokens of the phone's light / dark setting. */
    @Suppress("DEPRECATION")                    // the bar colours: ignored from Android 15, needed before it
    private fun applyColours() {
        val surface = tok(R.color.ax_surface)
        root.setBackgroundColor(surface)                          // behind the status bar when drawn edge to edge
        bannerRule.setBackgroundColor(tok(R.color.ax_border))
        navRule.setBackgroundColor(tok(R.color.ax_border))
        nav.setBackgroundColor(surface)
        val items = getColorStateList(R.color.ax_tab_item)        // the brand token for the open tab, muted for the rest
        nav.setItemIconTintList(items)
        nav.setItemTextColor(items)
        nav.setItemActiveIndicatorColor(ColorStateList.valueOf(tok(R.color.ax_brand_soft)))
        web.setBackgroundColor(tok(R.color.ax_bg))                // no white flash before a dark page paints
        refresh.setColorSchemeColors(tok(R.color.ax_brand))
        refresh.setProgressBackgroundColorSchemeColor(surface)
        val light = resources.getBoolean(R.bool.ax_light_bars)
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = light; isAppearanceLightNavigationBars = light
        }
        if (Build.VERSION.SDK_INT < 35) { window.statusBarColor = surface; window.navigationBarColor = surface }
        shown?.let { show(it) }
    }

    /** Opens the current tab's own page. */
    private fun load() {
        newTab = true
        web.loadUrl(server + tabs[current].path)
    }

    /** A link on the ATIP server: the host of the address set in the app, or of the page on screen. */
    private fun isAtip(u: Uri, page: String?): Boolean {
        val host = u.host ?: return false
        return host.equals(Uri.parse(server).host, ignoreCase = true) ||
            (page != null && host.equals(Uri.parse(page).host, ignoreCase = true))
    }

    private fun schedulePoll() {
        pollStatus()
        main.postDelayed({ schedulePoll() }, 60_000)
    }

    private fun pollStatus() = thread {
        val next = try {
            // the page shell's state (2026-11-02); a server older than that answers non-200: use /api/status
            val js = getJson("$server/api/v2/shell/state") ?: getJson("$server/api/status")
                ?: error("no status reply")
            StatusText.render(js)
        } catch (e: Exception) {
            StatusText.Shown("ATIP not reachable at $server — long-press to change", "#78350F", "#FDE68A", null)
        }
        main.post { show(next) }
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

    /** Normal (H10): the pills on the surface colour; anything else: the full-width strip in the state's colours. */
    private fun show(s: StatusText.Shown) {
        shown = s
        val env = s.env
        if (env == null) {
            envBadge.visibility = View.GONE                      // not known (older server, or not reachable)
        } else {
            envBadge.text = env
            envBadge.typeface = if (env == "LIVE") Typeface.DEFAULT_BOLD else semibold
            envBadge.background = pill(StatusText.ENV_COLOURS.getValue(env))
            envBadge.visibility = View.VISIBLE
        }
        if (s.strip) {
            bannerText.text = s.text
            bannerText.setTextColor(Color.parseColor(s.fg))
            bannerText.visibility = View.VISIBLE
            runPill.visibility = View.GONE
            banner.setBackgroundColor(Color.parseColor(s.bg))
            bannerRule.visibility = View.GONE
        } else {
            runPill.text = s.text
            runPill.setTextColor(Color.parseColor(s.fg))
            runPill.background = pill(s.bg)
            runPill.visibility = View.VISIBLE
            bannerText.visibility = View.GONE
            banner.setBackgroundColor(tok(R.color.ax_surface))
            bannerRule.visibility = View.VISIBLE
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
