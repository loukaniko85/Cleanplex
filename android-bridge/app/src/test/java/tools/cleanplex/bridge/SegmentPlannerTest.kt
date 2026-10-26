package tools.cleanplex.bridge

import org.junit.Assert.assertEquals
import org.junit.Test

/** Mirrors the server-side filter semantics: widen, look ahead, seek past the end. */
class SegmentPlannerTest {

    private val planner = SegmentPlanner()

    private fun skip(startMs: Long, endMs: Long) = BridgeSegment(startMs, endMs, "skip")

    @Test
    fun `seeks past the widened end, never to the start`() {
        val plan = planner.plan(
            positionMs = 23_000,
            segments = listOf(skip(30_000, 40_000)),
            skipUntilMs = 0,
        )

        assertEquals(BridgePlan.Seek(43_000, 43_000), plan)
    }

    @Test
    fun `does not retrigger at the widened end`() {
        val plan = planner.plan(
            positionMs = 43_000,
            segments = listOf(skip(30_000, 40_000)),
            skipUntilMs = 0,
        )

        assertEquals(BridgePlan.None, plan)
    }

    @Test
    fun `respects the lookahead window`() {
        val plan = planner.plan(
            positionMs = 21_000,
            segments = listOf(skip(30_000, 40_000)),
            skipUntilMs = 0,
        )

        assertEquals(BridgePlan.None, plan)
    }

    @Test
    fun `mutes until the widened end`() {
        val plan = planner.plan(
            positionMs = 31_000,
            segments = listOf(BridgeSegment(30_000, 40_000, "mute")),
            skipUntilMs = 0,
        )

        assertEquals(BridgePlan.Mute(43_000), plan)
    }

    @Test
    fun `suppresses a segment that was already handled`() {
        val plan = planner.plan(
            positionMs = 35_000,
            segments = listOf(skip(30_000, 40_000)),
            skipUntilMs = 50_000,
        )

        assertEquals(BridgePlan.None, plan)
    }

    @Test
    fun `clamps the pre-buffer at zero`() {
        val plan = planner.plan(
            positionMs = 0,
            segments = listOf(skip(1_000, 5_000)),
            skipUntilMs = 0,
        )

        assertEquals(BridgePlan.Seek(8_000, 8_000), plan)
    }
}
