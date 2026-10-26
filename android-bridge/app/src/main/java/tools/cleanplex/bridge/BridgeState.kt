package tools.cleanplex.bridge

/** Mutable status shown by the setup activity. Written from the polling thread. */
object BridgeState {
    @Volatile var running: Boolean = false
    @Volatile var sessionTitle: String = ""
    @Volatile var positionMs: Long = 0
    @Volatile var seekSupported: Boolean = false
    @Volatile var segmentCount: Int = 0
    @Volatile var lastAction: String = ""
    @Volatile var error: String = ""
}
