package com.housingar.inspect.room

import android.graphics.Color
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import com.housingar.inspect.InspectActivity
import com.housingar.inspect.R
import java.io.File
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.atomic.AtomicInteger

/** Listing preview has its own download destination; it never changes the scanning server. */
class RoomArController(private val activity: InspectActivity, private val scene: RoomArScene) {
  private val worker = Executors.newSingleThreadExecutor()
  private val generation = AtomicInteger()
  private var request: Future<*>? = null
  private var link: RoomLink? = null
  private var fullPlan: RoomPlan? = null
  private var destroyed = false
  @Volatile var active = false
    private set
  private val panel = LinearLayout(activity).apply {
    orientation = LinearLayout.VERTICAL
    setPadding(dp(12), dp(8), dp(12), dp(8))
    setBackgroundColor(Color.parseColor("#ED142434"))
    visibility = View.GONE
  }
  private val title = label(17f)
  private val details = label(13f)
  private val guide = label(14f)
  private val controls = LinearLayout(activity)
  private val back = object : OnBackPressedCallback(false) {
    override fun handleOnBackPressed() = close()
  }

  init {
    val heading = LinearLayout(activity)
    heading.addView(title, LinearLayout.LayoutParams(0, -2, 1f))
    heading.addView(button("Close") { close() })
    panel.addView(heading); panel.addView(details); panel.addView(guide)
    for ((text, action) in listOf<Pair<String, () -> Unit>>(
      "Turn ↶" to { scene.rotate(15f) }, "Turn ↷" to { scene.rotate(-15f) },
      "Move" to { scene.move() }, "Rooms" to { chooseRoom() },
    )) controls.addView(button(text, action), LinearLayout.LayoutParams(0, -2, 1f))
    panel.addView(controls)
    activity.findViewById<LinearLayout>(R.id.bottom_stack).addView(panel)
    activity.onBackPressedDispatcher.addCallback(activity, back)
  }

  fun open(raw: String) {
    val next = try { RoomLink.parse(raw) } catch (_: Exception) {
      activity.showBanner("This room link is invalid.", error = true); return
    }
    if (!activity.canOpenListingRoom()) {
      activity.showBanner("Finish or cancel the current scan, capture or review before opening a listing room.")
      return
    }
    val known = runCatching { RoomLink.origin(activity.settings.serviceUrl) }.getOrNull()
    if (next.service == known) load(next)
    else AlertDialog.Builder(activity)
      .setTitle("Open this room?")
      .setMessage("Download this room and its furniture from ${next.service}? This does not change the Mac used for scanning.")
      .setNegativeButton("Cancel", null)
      .setPositiveButton("Open room") { _, _ -> load(next) }.show()
  }

  private fun load(next: RoomLink) {
    request?.cancel(true)
    val epoch = generation.incrementAndGet()
    link = next; fullPlan = null
    active = true; back.isEnabled = true; panel.visibility = View.VISIBLE
    activity.prepareListingRoom()
    title.text = "Listing room · AR"
    details.text = "Loading the saved plan and My stuff…"
    guide.text = "Keep the phone pointed toward the floor."
    controls.isEnabled = false
    for (i in 0 until controls.childCount) controls.getChildAt(i).isEnabled = false
    scene.close()
    request = worker.submit {
      try {
        val repository = RoomRepository(File(activity.cacheDir, "room_ar/${RoomRepository.cacheName(next.service)}"), RoomHttp(next.service))
        val result = repository.load(next.listingId, next.roomId) { message ->
          activity.runOnUiThread { if (current(epoch)) details.text = message }
        }
        activity.runOnUiThread {
          if (!current(epoch)) { result.releaseBitmaps(); return@runOnUiThread }
          fullPlan = result.fullPlan
          title.text = if (next.roomId == null) result.title else result.plan.rooms.single().name
          val b = result.plan.bounds
          details.text = String.format(Locale.US, "%s · %.2f × %.2f m · %.2f m high · full size%s",
            if (result.plan.estimated) "Estimated plan" else "Your saved plan", b.x1-b.x0, b.z1-b.z0, result.plan.height,
            if (result.warnings.isEmpty()) "" else "\n${result.warnings.joinToString(". ")}")
          for (i in 0 until controls.childCount) controls.getChildAt(i).isEnabled = true
          scene.open(result)
        }
      } catch (e: Exception) {
        activity.runOnUiThread {
          if (current(epoch)) {
            details.text = "Couldn't load this room. Check the Mac connection, then open View in AR again."
            guide.text = e.message?.take(160) ?: "Room download failed"
          }
        }
      }
    }
  }

  fun guidance(message: String) { if (active && !destroyed) guide.text = message }
  fun close() {
    generation.incrementAndGet(); request?.cancel(true); request = null
    active = false; back.isEnabled = false; panel.visibility = View.GONE
    scene.close(); link = null; fullPlan = null
    activity.finishListingRoom()
  }
  fun destroy() { destroyed = true; generation.incrementAndGet(); request?.cancel(true); worker.shutdownNow(); scene.close() }
  private fun current(epoch: Int) = !destroyed && active && generation.get() == epoch
  private fun chooseRoom() {
    val plan = fullPlan ?: return
    val current = link ?: return
    val names = arrayOf("Whole apartment", *plan.rooms.map { it.name }.toTypedArray())
    AlertDialog.Builder(activity).setTitle("Preview at full size").setItems(names) { _, index ->
      load(current.copy(roomId = if (index == 0) null else plan.rooms[index-1].id))
    }.setNegativeButton("Cancel", null).show()
  }
  private fun dp(value: Int) = (value * activity.resources.displayMetrics.density).toInt()
  private fun label(size: Float) = TextView(activity).apply { textSize = size; setTextColor(Color.WHITE) }
  private fun button(text: String, action: () -> Unit) = Button(activity).apply {
    this.text = text; isAllCaps = false; minWidth = 0; minimumWidth = 0; textSize = 12f
    setOnClickListener { action() }
  }
}
