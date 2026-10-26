package tools.cleanplex.bridge

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

class MainActivity : Activity() {

    private lateinit var urlField: EditText
    private lateinit var status: TextView
    private val handler = Handler(Looper.getMainLooper())

    private val refresh = object : Runnable {
        override fun run() {
            render()
            handler.postDelayed(this, 1_000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("bridge", MODE_PRIVATE)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
        }

        val heading = TextView(this).apply {
            text = "Cleanplex Bridge"
            textSize = 24f
        }
        val help = TextView(this).apply {
            text = "1. Set the Cleanplex URL\n" +
                "2. Grant notification access (Settings > Apps > Special access)\n" +
                "3. Start the bridge, then play in the Plex app"
            textSize = 13f
        }
        urlField = EditText(this).apply {
            setText(prefs.getString("url", "http://10.0.24.2:7979"))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            hint = "http://your-cleanplex:7979"
        }
        val access = Button(this).apply {
            text = "Grant notification access"
            setOnClickListener {
                startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
            }
        }
        val start = Button(this).apply {
            text = "Start bridge"
            setOnClickListener {
                prefs.edit().putString("url", urlField.text.toString().trim()).apply()
                startForegroundService(Intent(this@MainActivity, BridgeService::class.java))
            }
        }
        val stop = Button(this).apply {
            text = "Stop bridge"
            setOnClickListener {
                stopService(Intent(this@MainActivity, BridgeService::class.java))
            }
        }
        status = TextView(this).apply {
            textSize = 14f
            setPadding(0, 24, 0, 0)
        }

        listOf(heading, help, urlField, access, start, stop, status).forEach { root.addView(it) }
        setContentView(root)

        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        handler.post(refresh)
    }

    override fun onDestroy() {
        handler.removeCallbacks(refresh)
        super.onDestroy()
    }

    private fun render() {
        val enabled = Settings.Secure
            .getString(contentResolver, "enabled_notification_listeners")
            .orEmpty()
            .contains(packageName)
        status.text = buildString {
            appendLine("Notification access: ${if (enabled) "granted" else "NOT granted"}")
            appendLine("Service: ${if (BridgeState.running) "running" else "stopped"}")
            appendLine("Plex title: ${BridgeState.sessionTitle.ifBlank { "—" }}")
            appendLine("Position: ${BridgeState.positionMs / 1000}s")
            appendLine("Seek supported: ${BridgeState.seekSupported}")
            appendLine("Segments: ${BridgeState.segmentCount}")
            appendLine("Last action: ${BridgeState.lastAction.ifBlank { "—" }}")
            if (BridgeState.error.isNotBlank()) append("Error: ${BridgeState.error}")
        }
    }
}
