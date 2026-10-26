package tools.cleanplex.bridge

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

data class BridgeSession(
    val title: String,
    val fullTitle: String,
    val mediaType: String,
    val segments: List<BridgeSegment>,
)

/** Minimal HTTP client for Cleanplex's bridge endpoint (no third-party deps). */
object CleanplexApi {
    fun fetchSessions(baseUrl: String): List<BridgeSession> {
        val endpoint = baseUrl.trimEnd('/') + "/api/sessions/bridge"
        val connection = (URL(endpoint).openConnection() as HttpURLConnection).apply {
            connectTimeout = 4_000
            readTimeout = 4_000
        }
        try {
            if (connection.responseCode != 200) {
                throw IllegalStateException("Cleanplex returned HTTP ${connection.responseCode}")
            }
            val body = connection.inputStream.bufferedReader().use { it.readText() }
            val sessions = JSONObject(body).optJSONArray("sessions") ?: return emptyList()
            return (0 until sessions.length()).mapNotNull { index ->
                val item = sessions.optJSONObject(index) ?: return@mapNotNull null
                val rawSegments = item.optJSONArray("segments")
                val segments = if (rawSegments == null) emptyList() else {
                    (0 until rawSegments.length()).mapNotNull { segmentIndex ->
                        val segment = rawSegments.optJSONObject(segmentIndex)
                            ?: return@mapNotNull null
                        BridgeSegment(
                            startMs = segment.optLong("start_ms"),
                            endMs = segment.optLong("end_ms"),
                            action = segment.optString("action", "skip"),
                        )
                    }
                }
                BridgeSession(
                    title = item.optString("title"),
                    fullTitle = item.optString("full_title"),
                    mediaType = item.optString("media_type"),
                    segments = segments,
                )
            }
        } finally {
            connection.disconnect()
        }
    }
}
