package tools.cleanplex.bridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.ComponentName
import android.content.Intent
import android.content.SharedPreferences
import android.media.AudioManager
import android.media.MediaMetadata
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.SystemClock

/**
 * Polls Plex's MediaSession and applies Cleanplex segments by seeking/muting.
 *
 * This works where Plex's remote-control API no longer does: it uses Android's
 * public MediaSession APIs, no Plex modification. It is only usable when the Plex
 * app exposes ACTION_SEEK_TO and notification access is granted.
 */
class BridgeService : Service() {

    companion object {
        private const val CHANNEL_ID = "bridge"
        // Plex for Android (mobile and TV builds share the package).
        private const val PLEX_PACKAGE = "com.plexapp.android"
    }

    private lateinit var thread: HandlerThread
    private lateinit var handler: Handler
    private lateinit var prefs: SharedPreferences
    private lateinit var audioManager: AudioManager

    private val planner = SegmentPlanner()
    private var segments: List<BridgeSegment> = emptyList()
    private var lastTitle = ""
    private var lastFetchAt = 0L
    private var skipUntilMs = 0L
    private var muteUntilMs = 0L
    private var restoreVolume = -1

    private val poll = object : Runnable {
        override fun run() {
            try {
                tick()
            } catch (error: Exception) {
                BridgeState.error = error.message ?: error.toString()
            }
            handler.postDelayed(this, 1_000)
        }
    }

    override fun onCreate() {
        super.onCreate()
        prefs = getSharedPreferences("bridge", MODE_PRIVATE)
        audioManager = getSystemService(AudioManager::class.java)
        thread = HandlerThread("cleanplex-bridge").also { it.start() }
        handler = Handler(thread.looper)
        createChannel()
        startForeground(1, notification("Watching for Plex playback"))
        BridgeState.running = true
        handler.post(poll)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_STICKY
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        restoreMute()
        thread.quitSafely()
        BridgeState.running = false
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun tick() {
        val manager = getSystemService(MediaSessionManager::class.java) ?: return
        val listener = ComponentName(this, PlexMediaListener::class.java)
        val sessions = try {
            manager.getActiveSessions(listener)
        } catch (error: SecurityException) {
            BridgeState.error = "Grant notification access for this app"
            return
        }

        val controller = sessions
            .filter { it.packageName == PLEX_PACKAGE }
            .maxByOrNull { it.playbackState?.state ?: PlaybackState.STATE_NONE }
            ?: run {
                BridgeState.sessionTitle = ""
                BridgeState.segmentCount = 0
                segments = emptyList()
                return
            }

        val playback = controller.playbackState ?: return
        BridgeState.seekSupported =
            (playback.actions and PlaybackState.ACTION_SEEK_TO) != 0L
        val title = controller.metadata?.getString(MediaMetadata.METADATA_KEY_TITLE).orEmpty()
        if (title != lastTitle) {
            lastTitle = title
            segments = emptyList()
            skipUntilMs = 0L
            lastFetchAt = 0L
            restoreMute()
        }
        BridgeState.sessionTitle = title
        val position = currentPosition(playback)
        BridgeState.positionMs = position
        refreshSegments(title)
        unmuteIfPast(position)

        if (playback.state != PlaybackState.STATE_PLAYING) return

        when (val plan = planner.plan(position, segments, skipUntilMs)) {
            is BridgePlan.Seek -> {
                if (BridgeState.seekSupported) {
                    controller.transportControls.seekTo(plan.targetMs)
                    skipUntilMs = plan.untilMs
                    BridgeState.lastAction =
                        "seek ${position / 1000}s to ${plan.targetMs / 1000}s"
                } else {
                    BridgeState.lastAction = "Plex does not expose SEEK_TO"
                }
            }

            is BridgePlan.Mute -> {
                if (muteUntilMs == 0L) {
                    restoreVolume = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)
                    audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, 0, 0)
                    muteUntilMs = plan.untilMs
                    BridgeState.lastAction = "muted until ${plan.untilMs / 1000}s"
                }
            }

            BridgePlan.None -> Unit
        }
    }

    private fun refreshSegments(title: String) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastFetchAt < 5_000 && segments.isNotEmpty()) return
        lastFetchAt = now

        val baseUrl = prefs.getString("url", "").orEmpty().trim()
        if (baseUrl.isEmpty()) {
            BridgeState.error = "Set the Cleanplex URL"
            return
        }
        BridgeState.error = ""
        val sessions = CleanplexApi.fetchSessions(baseUrl)
        val match = sessions.firstOrNull { it.title.equals(title, ignoreCase = true) }
            ?: sessions.firstOrNull { it.fullTitle.contains(title, ignoreCase = true) }
            ?: sessions.singleOrNull()
        segments = match?.segments ?: emptyList()
        BridgeState.segmentCount = segments.size
    }

    private fun currentPosition(playback: PlaybackState): Long {
        var position = playback.position
        if (playback.state == PlaybackState.STATE_PLAYING) {
            val elapsed = SystemClock.elapsedRealtime() - playback.lastPositionUpdateTime
            if (elapsed > 0) position += (elapsed * playback.playbackSpeed).toLong()
        }
        return position.coerceAtLeast(0L)
    }

    private fun unmuteIfPast(position: Long) {
        if (muteUntilMs > 0L && position > muteUntilMs) {
            restoreMute()
            BridgeState.lastAction = "unmuted"
        }
    }

    private fun restoreMute() {
        if (muteUntilMs > 0L && restoreVolume >= 0) {
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, restoreVolume, 0)
        }
        muteUntilMs = 0L
        restoreVolume = -1
    }

    private fun createChannel() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Bridge", NotificationManager.IMPORTANCE_LOW)
        )
    }

    private fun notification(text: String): Notification =
        Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Cleanplex Bridge")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setOngoing(true)
            .build()
}
