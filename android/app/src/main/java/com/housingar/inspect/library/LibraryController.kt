package com.housingar.inspect.library

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Typeface
import android.text.InputFilter
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import com.housingar.inspect.InspectActivity
import com.housingar.inspect.InspectRenderer
import com.housingar.inspect.JobCard
import com.housingar.inspect.MacServiceClient
import com.housingar.inspect.R
import com.housingar.inspect.mesh.GlbLoader
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * The full-screen sheet for "Objects found" (review a scan's proposals, name the ones to keep) and
 * "My stuff" (the library: place, rename, delete). The phone keeps an offline copy of every
 * library mesh so objects can be placed in a new apartment without reaching the Mac.
 * Public methods are called on the UI thread; network and file work runs on [worker].
 */
class LibraryController(
  private val activity: InspectActivity,
  private val client: MacServiceClient,
  private val jobs: JobCard,
) {
  private val worker = Executors.newSingleThreadExecutor()
  private val root = activity.getExternalFilesDir("library") ?: File(activity.filesDir, "library")
  val store = LibraryStore(root)
  private val thumbCache = File(activity.cacheDir, "proposal_thumbs")

  private val panel: View = activity.findViewById(R.id.sheet_panel)
  private val title: TextView = activity.findViewById(R.id.sheet_title)
  private val status: TextView = activity.findViewById(R.id.sheet_status)
  private val list: LinearLayout = activity.findViewById(R.id.sheet_list)
  private val buttons: LinearLayout = activity.findViewById(R.id.sheet_buttons)
  private val density = activity.resources.displayMetrics.density

  /** UI thread only. */
  private var items: List<LibraryItem> = store.load()
  private var online: Boolean? = null
  /** Which sheet is showing, so a background sync only redraws "My stuff". */
  private var showing: String? = null

  /** Called when the sheet opens or closes (the activity enables Back-to-close). */
  var onOpenChanged: (Boolean) -> Unit = {}

  val isOpen get() = panel.visibility == View.VISIBLE

  fun close() {
    showing = null
    panel.visibility = View.GONE
    onOpenChanged(false)
  }

  private fun open(heading: String, which: String) {
    showing = which
    title.text = heading
    list.removeAllViews()
    buttons.removeAllViews()
    panel.visibility = View.VISIBLE
    onOpenChanged(true)
  }

  // ------------------------------------------------------------------ scan proposals

  /** After a scan job finishes: load what the Mac found and let the user keep and name objects. */
  fun showProposals(scanId: String, path: String) {
    open("Objects found", "proposals")
    status.text = "Loading what the Mac found in the scan…"
    addButton("Close", GREY) { close() }
    worker.execute {
      try {
        val proposals = Proposal.listFrom(client.proposals(path))
        val thumbs = proposals.associate { p -> p.proposalId to p.thumbnailUrl?.let { loadThumb(it, File(thumbCache, "${safe(scanId)}_${safe(p.proposalId)}.img")) } }
        activity.runOnUiThread { if (showing == "proposals") renderProposals(scanId, proposals, thumbs) }
      } catch (e: Exception) {
        Log.e(TAG, "proposals failed", e)
        activity.runOnUiThread {
          if (showing != "proposals") return@runOnUiThread
          status.text = "Could not load the objects from the Mac: ${e.message?.take(140)}"
          buttons.removeAllViews()
          addButton("Retry", ACCENT) { showProposals(scanId, path) }
          addButton("Close", GREY) { close() }
        }
      }
    }
  }

  private class ProposalRow(val proposal: Proposal, val name: EditText, val keep: CheckBox)

  private fun renderProposals(scanId: String, proposals: List<Proposal>, thumbs: Map<String, Bitmap?>) {
    list.removeAllViews()
    buttons.removeAllViews()
    val usable = proposals.count { it.usable }
    status.text = when {
      proposals.isEmpty() -> "The Mac didn't find separate objects in this scan. Use Capture one for each object, or scan again more slowly, filming each object from all sides."
      else -> "Found ${proposals.size} object${if (proposals.size == 1) "" else "s"} ($usable with a mesh). Name the ones to keep; untick the rest."
    }
    val index = proposals.withIndex().associate { (i, p) -> p.proposalId to i + 1 }
    val rows = proposals.mapIndexed { i, p ->
      val row = rowLayout()
      row.addView(thumbView(thumbs[p.proposalId]))
      val col = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
      val name = EditText(activity).apply {
        // Tapped things come back as obj_01, obj_02, … in the order they were tapped.
        val tapNumber = Regex("^obj_(\\d+)$").find(p.proposalId)?.groupValues?.get(1)?.toIntOrNull()
        hint = if (tapNumber != null) "Name thing $tapNumber (e.g. desk lamp)" else "Name object ${i + 1} (e.g. desk lamp)"
        inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
        isSingleLine = true
        filters = arrayOf(InputFilter.LengthFilter(Placement.MAX_NAME_LENGTH))
        setTextColor(Color.WHITE)
        setHintTextColor(Color.GRAY)
        isEnabled = p.usable
      }
      col.addView(name)
      col.addView(detailText(buildString {
        append(dims(p.dimensionsM))
        if (p.views > 0) append(String.format(Locale.US, " · %d views, %.0f° around", p.views, p.coverageDeg))
        p.parentId?.let { parent -> append(" · on object ${index[parent] ?: parent}") }
        if (!p.usable) append("\nNo mesh: seen from too few sides. Use Capture one for it.")
        else if (p.lowQuality) append("\nLow quality: parts were not seen. Keep it, or capture it on its own.")
        p.warnings.take(2).forEach { append("\n").append(it) }
      }, if (!p.usable || p.lowQuality) WARN else LIGHT))
      row.addView(col, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
      val keep = CheckBox(activity).apply {
        text = "Keep"
        setTextColor(Color.WHITE)
        isChecked = p.usable && !p.lowQuality
        isEnabled = p.usable
      }
      row.addView(keep)
      list.addView(row)
      ProposalRow(p, name, keep)
    }
    if (rows.any { it.proposal.usable }) addButton("Add to My stuff", ACCENT) { addProposals(scanId, rows) }
    addButton("Capture one", ACCENT) {
      close()
      activity.armCaptureOne()
    }
    addButton("Close", GREY) { close() }
  }

  private fun addProposals(scanId: String, rows: List<ProposalRow>) {
    val chosen = rows.filter { it.keep.isChecked && it.proposal.usable }
    if (chosen.isEmpty()) {
      status.text = "Tick Keep on at least one object, or press Close."
      return
    }
    val unnamed = chosen.filter { it.name.text.isBlank() }
    if (unnamed.isNotEmpty()) {
      unnamed.forEach { it.name.error = "Name it, or untick Keep" }
      unnamed.first().name.requestFocus()
      status.text = "Give every kept object a name."
      return
    }
    val picks = chosen.map { it.proposal.proposalId to it.name.text.toString().trim() }
    close()
    jobs.show("Adding ${picks.size} object${if (picks.size == 1) "" else "s"} to My stuff…", 0f) {}
    worker.execute {
      val failed = mutableListOf<String>()
      picks.forEachIndexed { i, (proposalId, name) ->
        try {
          client.addToLibrary(name, JSONObject().put("scan_id", scanId).put("proposal_id", proposalId))
        } catch (e: Exception) {
          Log.e(TAG, "add $proposalId failed", e)
          failed += "$name (${e.message?.take(60)})"
        }
        jobs.show("Adding to My stuff: ${i + 1} / ${picks.size}…", (i + 1f) / picks.size) {}
      }
      if (failed.isEmpty()) {
        jobs.hide("Added ${picks.size} to My stuff. Downloading them to the phone…")
      } else {
        jobs.fail("Could not add: ${failed.joinToString()}", null)
      }
      activity.runOnUiThread { showLibrary() }
    }
  }

  // ------------------------------------------------------------------ My stuff

  /** Sync from the Mac (when reachable) and show the library. Callable from any thread. */
  fun syncAndShow() = activity.runOnUiThread { showLibrary() }

  fun showLibrary() {
    open("My stuff", "library")
    renderLibrary()
    status.text = statusLine(syncing = true)
    worker.execute {
      val result = sync()
      activity.runOnUiThread {
        items = result.first
        online = result.second == null
        if (showing != "library") return@runOnUiThread
        renderLibrary()
        status.text = statusLine(syncing = false, error = result.second)
      }
    }
  }

  private fun statusLine(syncing: Boolean, error: String? = null): String {
    val offline = items.count { store.isAvailableOffline(it) }
    val count = "${items.size} object${if (items.size == 1) "" else "s"}, $offline on this phone"
    return when {
      syncing -> "$count. Checking the Mac…"
      error != null -> "$count. Offline: the Mac isn't reachable, so rename and delete are unavailable. Placing works."
      items.isEmpty() -> "Nothing here yet. Press Scan stuff and film your things, or Capture one for a single object."
      else -> "$count. Up to date with the Mac."
    }
  }

  /** Worker thread. Returns the items to show, and an error if the Mac could not be reached. */
  private fun sync(): Pair<List<LibraryItem>, String?> = store.sync(client)

  private fun renderLibrary() {
    list.removeAllViews()
    buttons.removeAllViews()
    for (item in items) {
      val row = rowLayout()
      row.addView(thumbView(decodeThumb(store.thumbnailFile(item.itemId))))
      val col = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
      col.addView(TextView(activity).apply {
        text = item.name
        setTextColor(Color.WHITE)
        textSize = 16f
        setTypeface(typeface, Typeface.BOLD)
      })
      val offline = store.isAvailableOffline(item)
      col.addView(detailText(buildString {
        append(dims(item.dimensionsM))
        if (item.lowQuality) append(" · low quality")
        if (!offline) append(if (item.meshUrl == null) "\nNo mesh on the Mac." else "\nNot downloaded yet: open My stuff while the Mac is reachable.")
      }, if (!offline || item.lowQuality) WARN else LIGHT))
      val actions = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
      actions.addView(smallButton("Place", ACCENT, offline) { place(item) })
      actions.addView(smallButton("Rename", ACCENT, online == true) { rename(item) })
      actions.addView(smallButton("Delete", RED, online == true) { delete(item) })
      col.addView(actions)
      row.addView(col, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
      list.addView(row)
    }
    addButton("Scan stuff", ACCENT) {
      close()
      activity.scan.start()
    }
    addButton("Capture one", ACCENT) {
      close()
      activity.armCaptureOne()
    }
    addButton("Save layout", ACCENT) {
      close()
      saveLayout()
    }
    addButton("Close", GREY) { close() }
  }

  private fun place(item: LibraryItem) {
    close()
    activity.showBanner("Loading ${item.name}…")
    worker.execute {
      try {
        val mesh = GlbLoader.load(store.meshFile(item.itemId).readBytes())
        val bitmap = mesh.baseColorImage?.let { BitmapFactory.decodeByteArray(it, 0, it.size) }
        activity.placeItem(InspectRenderer.PlaceRequest(item.itemId, item.name, mesh, bitmap))
      } catch (e: Exception) {
        Log.e(TAG, "load ${item.itemId} failed", e)
        activity.showBanner("Could not load ${item.name}: ${e.message?.take(120)}", error = true)
      }
    }
  }

  private fun rename(item: LibraryItem) {
    activity.askName("Rename ${item.name}", "New name", item.name) { name ->
      status.text = "Renaming…"
      worker.execute {
        val error = try {
          client.renameLibraryItem(item.itemId, name)
          null
        } catch (e: Exception) {
          e.message ?: e.toString()
        }
        activity.runOnUiThread {
          if (error != null) activity.showBanner("Rename failed: ${error.take(120)}", error = true)
          if (showing == "library") showLibrary()
        }
      }
    }
  }

  private fun delete(item: LibraryItem) {
    AlertDialog.Builder(activity)
      .setTitle("Delete ${item.name}?")
      .setMessage("It is removed from My stuff on the Mac and on this phone. Objects already placed in the room stay until you clear them.")
      .setPositiveButton("Delete") { _, _ ->
        worker.execute {
          val error = try {
            client.deleteLibraryItem(item.itemId)
            store.delete(item.itemId)
            null
          } catch (e: Exception) {
            e.message ?: e.toString()
          }
          activity.runOnUiThread {
            if (error != null) activity.showBanner("Delete failed: ${error.take(120)}", error = true)
            if (showing == "library") showLibrary()
          }
        }
      }
      .setNegativeButton("Cancel", null)
      .show()
  }

  /** Records what is placed in this room: on the phone always, and on the Mac when reachable. */
  fun saveLayout() {
    activity.snapshotPlacements { placements ->
      if (placements.isEmpty()) {
        activity.showBanner("Place something from My stuff first, then save the layout.")
        return@snapshotPlacements
      }
      activity.askName("Name this layout", "e.g. new bedroom") { name ->
        val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val layoutId = "${activity.diagnostics.sessionId}_layout_$stamp"
        val local = Placement.layoutJson(layoutId, activity.diagnostics.sessionId, name, placements, withNames = true)
        val forMac = Placement.layoutJson(layoutId, activity.diagnostics.sessionId, name, placements)
        worker.execute {
          val dir = activity.getExternalFilesDir("layouts") ?: File(activity.filesDir, "layouts")
          dir.mkdirs()
          File(dir, "$layoutId.json").writeText(local.toString(2))
          val posted = client.postJson("/v1/layouts", forMac)
          activity.showBanner("Layout \"$name\" (${placements.size} object${if (placements.size == 1) "" else "s"}) saved on the phone" +
            if (posted) " and the Mac." else ". The Mac isn't reachable, so it's only on the phone.")
        }
      }
    }
  }

  // ------------------------------------------------------------------ helpers

  /** Worker thread: download a proposal thumbnail into the cache (best effort). */
  private fun loadThumb(url: String, file: File): Bitmap? = try {
    file.parentFile?.mkdirs()
    if (!file.isFile) client.download(url, file)
    decodeThumb(file)
  } catch (e: Exception) {
    Log.w(TAG, "thumbnail $url failed: $e")
    null
  }

  private fun decodeThumb(file: File): Bitmap? {
    if (!file.isFile) return null
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(file.path, bounds)
    var sample = 1
    while (bounds.outWidth / (sample * 2) >= THUMB_PX && bounds.outHeight / (sample * 2) >= THUMB_PX) sample *= 2
    return BitmapFactory.decodeFile(file.path, BitmapFactory.Options().apply { inSampleSize = sample })
  }

  private fun rowLayout() = LinearLayout(activity).apply {
    orientation = LinearLayout.HORIZONTAL
    gravity = Gravity.CENTER_VERTICAL
    val p = dp(6)
    setPadding(p, p, p, p)
    layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { bottomMargin = dp(6) }
    setBackgroundColor(ROW_BG)
  }

  private fun thumbView(bitmap: Bitmap?) = ImageView(activity).apply {
    layoutParams = LinearLayout.LayoutParams(dp(72), dp(72)).apply { marginEnd = dp(10) }
    scaleType = ImageView.ScaleType.CENTER_CROP
    setBackgroundColor(Color.BLACK)
    contentDescription = "Object photo"
    if (bitmap != null) setImageBitmap(bitmap)
  }

  private fun detailText(text: String, color: Int) = TextView(activity).apply {
    this.text = text
    setTextColor(color)
    textSize = 12f
  }

  private fun smallButton(label: String, color: Int, enabled: Boolean, onClick: () -> Unit) =
    Button(activity, null, android.R.attr.buttonBarButtonStyle).apply {
      text = label
      setTextColor(if (enabled) color else Color.DKGRAY)
      isEnabled = enabled
      minWidth = 0
      minimumWidth = 0
      setOnClickListener { onClick() }
    }

  private fun addButton(label: String, color: Int, onClick: () -> Unit) {
    buttons.addView(Button(activity, null, android.R.attr.buttonBarButtonStyle).apply {
      text = label
      setTextColor(color)
      setOnClickListener { onClick() }
    }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
  }

  private fun dp(v: Int) = (v * density).toInt()

  companion object {
    private const val TAG = "LibraryController"
    private const val THUMB_PX = 144
    private val ACCENT = Color.parseColor("#FF35C4D6")
    private val GREY = Color.parseColor("#FFBDBDBD")
    private val RED = Color.parseColor("#FFEF9A9A")
    private val WARN = Color.parseColor("#FFFFD54F")
    private val LIGHT = Color.parseColor("#FFE0E0E0")
    private val ROW_BG = Color.parseColor("#33FFFFFF")

    fun dims(d: FloatArray?): String =
      if (d == null || d.size < 3) "size unknown"
      else String.format(Locale.US, "%.0f × %.0f × %.0f cm", d[0] * 100, d[1] * 100, d[2] * 100)

    private fun safe(id: String) = id.replace(Regex("[^A-Za-z0-9_-]"), "_")
  }
}
