package com.housingar.inspect

import android.util.Base64
import android.util.Log
import android.view.View
import android.widget.Button
import androidx.appcompat.app.AlertDialog
import com.housingar.inspect.capture.CaptureFormat
import com.housingar.inspect.capture.ScanSession
import com.housingar.inspect.capture.TappedObject
import org.json.JSONObject
import java.io.File
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit

/**
 * "Scan stuff": film your belongings and tap each thing you want to keep → walk around each one
 * until it's covered → package (scan_v2: frames + the tapped objects) → upload → poll the Mac job
 * → name the objects it built (LibraryController.showProposals).
 * UI methods run on the UI thread; file work on [worker]; outline previews on [net], so a slow or
 * unreachable Mac never holds up saving frames.
 */
class ScanController(
  private val activity: InspectActivity,
  private val client: MacServiceClient,
  private val jobs: JobCard,
) {
  private val worker = Executors.newSingleThreadExecutor()
  private val net = Executors.newSingleThreadExecutor()
  private val previews = java.util.concurrent.CopyOnWriteArrayList<Future<*>>()
  private val scanBar: View = activity.findViewById(R.id.scan_bar)
  private val finishButton: Button = activity.findViewById(R.id.scan_finish)
  private val mainBar: View = activity.findViewById(R.id.main_bar)
  private var session: ScanSession? = null
  @Volatile private var cancelled = false
  @Volatile private var jobId: String? = null

  init {
    finishButton.setOnClickListener { finish() }
    activity.findViewById<Button>(R.id.scan_cancel).setOnClickListener { cancel() }
    activity.findViewById<Button>(R.id.scan_undo).setOnClickListener { undo() }
    activity.findViewById<Button>(R.id.scan_new_thing).setOnClickListener { newThing() }
  }

  val isScanning get() = session != null

  fun start() {
    if (session != null) return
    if (jobs.busy) {
      activity.showBanner("Wait for the current Mac job to finish first.")
      return
    }
    val sessionId = activity.diagnostics.sessionId
    val scanId = "${sessionId}_scan_${System.currentTimeMillis() % 100_000_000}"
    val root = activity.getExternalFilesDir("scans") ?: activity.filesDir
    activity.startScan({ anchor, anchorToWorld ->
      ScanSession(scanId, sessionId, anchor, anchorToWorld, File(root, scanId), worker, activity.diagnostics.rawDepthSupported, null)
    }) { s ->
      if (s == null) {
        activity.showBanner("Tracking isn't ready. Move the phone slowly sideways, then press Scan stuff again.")
        return@startScan
      }
      session = s
      mainBar.visibility = View.GONE
      scanBar.visibility = View.VISIBLE
      previews.clear()
      updateProgress(0, 0f)
      activity.showBanner("Scanning. Tap a thing you want to keep (a few spots on it), walk around it until its ring is green, then press New thing for the next.")
    }
  }

  /** From the renderer a few times per second. */
  fun updateProgress(frames: Int, @Suppress("UNUSED_PARAMETER") walkedM: Float) = activity.runOnUiThread {
    val objects = session?.objects.orEmpty()
    val ready = objects.count { it.coverage.ready }
    finishButton.text = if (objects.isEmpty()) "Tap a thing" else
      String.format(Locale.US, "Finish (%d/%d covered)", ready, objects.size)
    finishButton.isEnabled = frames >= ScanSession.MIN_FRAMES && objects.isNotEmpty()
  }

  /**
   * UI thread: the renderer kept a tap — a new thing, or another spot on the current one. When the
   * Mac is reachable the thing's outline (from all its taps, in this seed frame) is shown straight
   * away and saved as its seed mask; otherwise the Mac outlines it after upload.
   */
  fun onObjectAdded(obj: TappedObject, frame: CapturedFrame, newThing: Boolean) {
    val s = session ?: return
    val seed = obj.seed
    activity.showBanner(if (newThing) "Thing ${obj.number} added. Tap other parts of it too, then walk around it until its ring is green."
      else "Thing ${obj.number}: ${obj.tapCount} taps. Tap more parts, or press New thing for the next one.")
    previews += net.submit {
      try {
        val jpeg = frame.toJpeg()
        val resp = client.segment(frame.frameId, jpeg, seed.points, frame.metadata())
        val b64 = resp.getJSONArray("masks").getJSONObject(0).getString("mask_png_b64")
        // Only if this is still the thing's latest seed (not undone or tapped again meanwhile).
        if (obj !in s.objects || obj.seed !== seed) return@submit
        val name = "seeds/${obj.objectId}.png"
        File(s.dir, "seeds").mkdirs()
        File(s.dir, name).writeBytes(Base64.decode(b64, Base64.DEFAULT))
        obj.maskFile = name
        val preview = CameraImages.maskPreview(jpeg, b64, seed.points, frame.sensorToDisplayRotation)
        activity.showPreview(preview, "Thing ${obj.number}: is this the whole thing? If not, tap the parts it missed (or Undo tap).")
      } catch (e: Exception) {
        Log.i(TAG, "outline preview unavailable: $e")
        if (newThing) activity.showBanner("Thing ${obj.number} added. (The Mac isn't connected, so it will outline it after upload. Tap a few parts of it to be safe.)")
      }
    }
  }

  private fun undo() {
    if (session == null) return
    activity.undoScanObject { r ->
      when {
        r == null -> activity.showBanner("Nothing to undo: tap a thing to add it.")
        r.thingRemoved -> activity.showBanner("Thing ${r.obj.number} removed.")
        else -> activity.showBanner("Removed the last tap on thing ${r.obj.number} (${r.obj.tapCount} left).")
      }
    }
  }

  private fun newThing() {
    val s = session ?: return
    if (s.objects.isEmpty()) {
      activity.showBanner("Tap the first thing you want to keep.")
      return
    }
    activity.startNewScanThing()
    activity.showBanner("Now tap the next thing.")
  }

  private fun cancel() {
    if (session == null) return
    activity.stopScan { stopped ->
      session = null
      worker.execute { stopped.dir.deleteRecursively() }
      scanBar.visibility = View.GONE
      mainBar.visibility = View.VISIBLE
      activity.showBanner("Scan cancelled.")
    }
  }

  private fun finish() {
    val s = session ?: return
    val thin = s.objects.filter { !it.coverage.ready }
    if (thin.isEmpty()) {
      stopAndUpload()
      return
    }
    AlertDialog.Builder(activity)
      .setTitle("Some things aren't filmed from enough sides")
      .setMessage(thin.joinToString("\n") { String.format(Locale.US, "Thing %d: %.0f° around, %d views", it.number, it.coverage.coverageDeg, it.coverage.views) } +
        "\n\nTheir 3D models will have holes or missing sides. Walk around them until their rings are mostly green (at least halfway round).")
      .setPositiveButton("Keep filming", null)
      .setNegativeButton("Finish anyway") { _, _ -> stopAndUpload() }
      .show()
  }

  private fun stopAndUpload() {
    if (session == null) return
    activity.stopScan { stopped ->
      session = null
      scanBar.visibility = View.GONE
      mainBar.visibility = View.VISIBLE
      cancelled = false
      jobs.show("Packaging ${stopped.frameCount} frames…", 0f) { cancelJob() }
      worker.execute { packageAndUpload(stopped) }
    }
  }

  // ---------------------------------------------------------------- worker thread

  private fun packageAndUpload(s: ScanSession) {
    if (cancelled) return
    // Let outline previews still in flight finish (they add seed masks), but don't wait long.
    val deadline = System.currentTimeMillis() + 8000
    for (f in previews) {
      try {
        f.get(maxOf(1L, deadline - System.currentTimeMillis()), TimeUnit.MILLISECONDS)
      } catch (e: Exception) {
        f.cancel(true)
      }
    }
    previews.clear()
    val zip = File(s.dir.parentFile, "${s.scanId}.zip")
    val manifest = try {
      s.writeError?.let { throw IllegalStateException("saving frames failed: $it") }
      // Only the seed masks of things still in the scan go into the archive.
      val keep = s.objects.mapNotNull { it.maskFile }.toSet()
      File(s.dir, "seeds").listFiles()?.forEach { if ("seeds/${it.name}" !in keep) it.delete() }
      val m = s.manifest()
      File(s.dir, "manifest.json").writeText(m.toString(2))
      zip.outputStream().buffered().use { CaptureFormat.writeZip(s.dir, it) }
      m
    } catch (e: Exception) {
      Log.e(TAG, "packaging failed", e)
      jobs.fail("Could not package the scan: ${e.message}", null)
      return
    }
    upload(s, manifest, zip, 1)
  }

  private fun upload(s: ScanSession, firstManifest: JSONObject, zip: File, attempt: Int) {
    val manifest = if (attempt == 1) firstManifest else s.manifest("${s.scanId}_r$attempt")
    jobId = null
    try {
      var lastUi = 0L
      val resp = client.uploadScan(manifest, zip) { sent, total ->
        if (cancelled) throw CancelledUpload()
        val now = System.currentTimeMillis()
        if (now - lastUi > 200) {
          lastUi = now
          jobs.show(String.format(Locale.US, "Uploading the scan: %.0f / %.0f MB…", sent / 1e6, total / 1e6), sent.toFloat() / total) { cancelJob() }
        }
      }
      val id = resp.getString("job_id")
      jobId = id
      poll(s, resp.optString("scan_id", manifest.getString("scan_id")), id, zip, firstManifest, attempt)
    } catch (e: Exception) {
      if (cancelled) return // cancelJob() hides the card
      Log.e(TAG, "upload failed", e)
      jobs.fail(String.format(Locale.US, "Scan upload failed (%.0f MB): %s\nThe scan is saved on the phone (%s).",
        zip.length() / 1e6, e.message?.take(140), s.scanId), { retry { upload(s, firstManifest, zip, attempt + 1) } })
    }
  }

  private fun retry(action: () -> Unit) {
    cancelled = false
    worker.execute(action)
  }

  private fun poll(s: ScanSession, scanId: String, id: String, zip: File, firstManifest: JSONObject, attempt: Int) {
    var failures = 0
    var lastProgress = 0f
    while (!cancelled) {
      val job = try {
        client.job(id).also { failures = 0 }
      } catch (e: MacServiceClient.ServiceException) {
        // The Mac answered but doesn't know this job (e.g. its data was cleared).
        jobs.fail("The Mac no longer has this scan's job (${e.message?.take(100)}). The scan is saved on the phone (${s.scanId}).",
          { retry { upload(s, firstManifest, zip, attempt + 1) } })
        return
      } catch (e: Exception) {
        // Unplugged or off Wi‑Fi: the Mac keeps working, so keep waiting rather than giving up.
        if (++failures >= 3 && !cancelled) {
          jobs.show("Can't reach the Mac right now. It keeps working on the scan: plug the phone back in and run " +
            "adb reverse tcp:8000 tcp:8000 (or reconnect Wi‑Fi), and this carries on by itself.", lastProgress) { stopWaiting() }
        }
        Thread.sleep(3000)
        continue
      }
      val state = job.optString("state")
      val msg = job.optString("message").ifBlank { JobCard.STAGES[state] ?: state }
      when (state) {
        "done" -> {
          jobs.hide(null)
          val path = job.optJSONObject("outputs")?.optString("proposals")?.ifBlank { null } ?: "/v1/scans/$scanId/proposals"
          activity.runOnUiThread { activity.library.showProposals(scanId, path) }
          return
        }
        "failed" -> {
          jobs.fail("The Mac could not process the scan: ${job.optString("error").ifBlank { msg }}\nThe scan is saved on the phone (${s.scanId}).",
            { retry { upload(s, firstManifest, zip, attempt + 1) } })
          return
        }
        "cancelled" -> {
          jobs.hide("Scan processing cancelled.")
          return
        }
        else -> {
          lastProgress = job.optDouble("progress", 0.0).toFloat()
          if (!cancelled) jobs.show("Building your things on the Mac (this takes several minutes; you can unplug and come back): $msg", lastProgress) { cancelJob() }
        }
      }
      Thread.sleep(2000)
    }
  }

  /** Cancel packaging, upload or Mac processing. Runs after the worker's current step stops. */
  private fun cancelJob() {
    cancelled = true
    jobs.hide("Cancelled. The scan stays saved on the phone.")
    worker.execute {
      jobId?.let { client.cancelJob(it) }
      jobId = null
    }
  }

  /** Stop following a job the phone can't reach (the Mac may still finish it). */
  private fun stopWaiting() {
    cancelled = true
    jobs.hide("Stopped waiting for the Mac. The scan is saved on the phone.")
  }

  private class CancelledUpload : java.io.IOException("cancelled")

  companion object {
    private const val TAG = "ScanController"
  }
}
