package com.housingar.inspect

import android.content.Context

/** Small persisted settings for the inspection screen. Read from any thread; written on the UI thread. */
class AppSettings(context: Context) {
  private val prefs = context.getSharedPreferences("room_inspect", Context.MODE_PRIVATE)

  /**
   * Base URL of the Mac segmentation service. The default works over USB after running
   * `adb reverse tcp:8000 tcp:8000` on the Mac (see scripts/phone_usb_setup.sh).
   */
  @Volatile var serviceUrl: String = prefs.getString(KEY_URL, DEFAULT_URL) ?: DEFAULT_URL
    set(value) {
      field = value.trim().trimEnd('/')
      prefs.edit().putString(KEY_URL, field).apply()
    }

  @Volatile var showPlanes: Boolean = prefs.getBoolean(KEY_PLANES, true)
    set(value) {
      field = value
      prefs.edit().putBoolean(KEY_PLANES, value).apply()
    }

  @Volatile var showPointCloud: Boolean = prefs.getBoolean(KEY_POINTS, true)
    set(value) {
      field = value
      prefs.edit().putBoolean(KEY_POINTS, value).apply()
    }

  @Volatile var showDepth: Boolean = prefs.getBoolean(KEY_DEPTH_VIS, false)
    set(value) {
      field = value
      prefs.edit().putBoolean(KEY_DEPTH_VIS, value).apply()
    }

  @Volatile var useOcclusion: Boolean = prefs.getBoolean(KEY_OCCLUSION, false)
    set(value) {
      field = value
      prefs.edit().putBoolean(KEY_OCCLUSION, value).apply()
    }

  companion object {
    const val DEFAULT_URL = "http://127.0.0.1:8000"
    private const val KEY_URL = "service_url"
    private const val KEY_PLANES = "show_planes"
    private const val KEY_POINTS = "show_points"
    private const val KEY_DEPTH_VIS = "show_depth"
    private const val KEY_OCCLUSION = "use_occlusion"
  }
}
