package tools.cleanplex.bridge

import android.service.notification.NotificationListenerService

/** Exists only to grant the app Notification access, which is what allows reading
 *  another app's MediaSession on modern Android. */
class PlexMediaListener : NotificationListenerService() {
    override fun onListenerConnected() {
        BridgeState.error = ""
    }

    override fun onListenerDisconnected() {
        BridgeState.error = "Notification access was revoked"
    }
}
