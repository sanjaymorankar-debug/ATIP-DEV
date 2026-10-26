package com.atip.mobile

import android.annotation.SuppressLint
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.google.android.material.tabs.TabLayout
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * ATIP for Android (2026-10-26): a native shell around ATIP's own mobile screens.
 *
 *   Today          /m            market health, signals, portfolio, pipeline, alerts
 *   Command        /command      Command Center (UXR-01): halt switch, sessions, pending confirmations
 *   Global & Asia  /global-asia  Japan, South Korea, Taiwan, Pakistan signals + global markets (Task 3)
 *
 * The banner on top is native: it polls GET /api/status every 60 s and shows STOPPED (21:00-06:00 IST,
 * "ATIP stopped — resumes 06:00 IST"), RESUMING or RUNNING with the last-updated time, so the state is
 * visible even when a web screen fails to load. Long-press the banner to change the server address.
 * The app holds no credentials and places no orders itself; every write goes through the web pages,
 * which carry the dashboard token and are refused (HTTP 423) while ATIP is stopped.
 */
class MainActivity : AppCompatActivity() {
    private val tabs = listOf("/m" to R.string.tab_today, "/command" to R.string.tab_command,
                              "/global-asia" to R.string.tab_asia)
    private lateinit var web: WebView
    private lateinit var banner: TextView
    private lateinit var refresh: SwipeRefreshLayout
    private val main = Handler(Looper.getMainLooper())
    private var current = 0

    private val server: String
        get() = getSharedPreferences("atip", MODE_PRIVATE).getString("server", BuildConfig.DEFAULT_SERVER)!!
            .trimEnd('/')

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        banner = TextView(this).apply {
            setPadding(32, 20, 32, 20); textSize = 13f; gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(Color.parseColor("#334155")); setTextColor(Color.WHITE)
            text = "Checking ATIP status…"; minHeight = 132       // >= 44dp touch target
            setOnLongClickListener { askServer(); true }
            contentDescription = "ATIP operating status"
        }
        val tabBar = TabLayout(this).apply {
            tabs.forEach { (_, label) -> addTab(newTab().setText(label)) }
            addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
                override fun onTabSelected(t: TabLayout.Tab) { current = t.position; load() }
                override fun onTabUnselected(t: TabLayout.Tab) {}
                override fun onTabReselected(t: TabLayout.Tab) { load() }
            })
        }
        web = WebView(this).apply {
            settings.javaScriptEnabled = true          // the ATIP pages render from their own APIs
            settings.domStorageEnabled = true          // /m keeps its last-good copy for offline use
            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView, url: String) { refresh.isRefreshing = false }
            }
        }
        refresh = SwipeRefreshLayout(this).apply { addView(web); setOnRefreshListener { load(); pollStatus() } }
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(banner, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(tabBar, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            addView(refresh, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        })
        load()
        schedulePoll()
    }

    private fun load() = web.loadUrl(server + tabs[current].first)

    private fun schedulePoll() {
        pollStatus()
        main.postDelayed({ schedulePoll() }, 60_000)
    }

    private fun pollStatus() = thread {
        val shown = try {
            val c = URL("$server/api/status").openConnection() as HttpURLConnection
            c.connectTimeout = 8000; c.readTimeout = 8000
            val js = JSONObject(c.inputStream.bufferedReader().use { it.readText() })
            StatusText.render(js.optString("status"), js.optString("banner"), js.optString("last_updated"),
                              js.optString("next_stop"), js.optBoolean("live_feeds", true))
        } catch (e: Exception) {
            Triple("ATIP not reachable at $server — long-press to change", "#78350F", "#FDE68A")
        }
        main.post {
            banner.text = shown.first
            banner.setBackgroundColor(Color.parseColor(shown.second))
            banner.setTextColor(Color.parseColor(shown.third))
        }
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

/** The banner's words and colours for one /api/status reply (same colours as the web banner). */
object StatusText {
    fun render(status: String, banner: String, lastUpdated: String, nextStop: String, liveFeeds: Boolean)
            : Triple<String, String, String> {
        val lu = " · last updated " + lastUpdated.ifBlank { "—" }
        return when (status) {
            "STOPPED" -> Triple("⏸ $banner · read-only, no orders$lu", "#7C2D12", "#FFEDD5")
            "RESUMING" -> Triple("↻ $banner$lu", "#1E3A8A", "#DBEAFE")
            else -> Triple("● $banner · stops ${nextStop.takeLast(5)} IST" +
                           (if (liveFeeds) "" else " · no live feeds today") + lu, "#064E3B", "#D1FAE5")
        }
    }
}
