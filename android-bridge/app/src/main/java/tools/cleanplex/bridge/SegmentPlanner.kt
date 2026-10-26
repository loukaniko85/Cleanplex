package tools.cleanplex.bridge

/** One Cleanplex segment relevant to the playing title. */
data class BridgeSegment(
    val startMs: Long,
    val endMs: Long,
    val action: String,
)

sealed class BridgePlan {
    data object None : BridgePlan()
    data class Seek(val targetMs: Long, val untilMs: Long) : BridgePlan()
    data class Mute(val untilMs: Long) : BridgePlan()
}

/**
 * Decides when to act, mirroring Cleanplex's server-side filter engine:
 * widen the segment by the pre/post buffers, trigger within the look-ahead,
 * and seek PAST the widened end (never to the start).
 *
 * Kept free of Android types so it can be unit-tested on the JVM.
 */
class SegmentPlanner(
    private val preBufferMs: Long = 3_000,
    private val postBufferMs: Long = 3_000,
    private val lookaheadMs: Long = 5_000,
) {
    fun plan(positionMs: Long, segments: List<BridgeSegment>, skipUntilMs: Long): BridgePlan {
        if (positionMs < skipUntilMs) return BridgePlan.None

        for (segment in segments) {
            val start = maxOf(0L, segment.startMs - preBufferMs)
            val end = segment.endMs + postBufferMs
            val inWindow = positionMs >= start - lookaheadMs && positionMs < end
            if (!inWindow) continue
            return if (segment.action == "mute") BridgePlan.Mute(end) else BridgePlan.Seek(end, end)
        }
        return BridgePlan.None
    }
}
