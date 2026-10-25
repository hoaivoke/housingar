package com.housingar.inspect

import android.util.Log
import android.view.View
import android.widget.Button
import com.housingar.inspect.capture.ObjectCaptureSession
import com.housingar.inspect.capture.CaptureFormat
import com.housingar.inspect.capture.SeedExtent
import com.housingar.inspect.geometry.SurfaceGeometry
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Locale
import java.util.concurrent.Executors

/**
 * "Capture one object" (for things the scan missed): guided walk-around → package (capture_v1) →
 * upload → poll the Mac job → add the result to My stuff under a name the user gives it.
 * UI methods run on the UI thread; all file and network work runs on [worker].
 */
class MeshController(private val activity: InspectActivity, private val client: MacServiceClient, private val jobs: JobCard) {
  private val worker = Executors.newSingleThreadExecutor()

  private val captureBar: View = activity.findViewById(R.id.capture_bar)
  private val finishButton: Button = activity.findViewById(R.id.capture_finish)
  private val captureCancel: Button = activity.findViewById(R.id.capture_cancel)
  private val mainBar: View = activity.findViewById(R.id.main_bar)

  private var session: ObjectCaptureSession? = null
  @Volatile private var jobId: String? = null
  @Volatile private var cancelled = false
  /** Capture whose job is in flight; owns the selection anchor until the job ends. */
  @Volatile private var inFlight: ObjectCaptureSession? = null

  init {
    finishButton.setOnClickListener { finishCapture() }
    captureCancel.setOnClickListener { cancelCapture() }
  }

  val isCapturing get() = session != null

  /**
   * From the review screen. Computes the object's rough 3D extent from the seed frame; on success
   * takes ownership of the selection anchor and starts the guided capture.
   */
  fun begin(
    c: SelectionCapture,
    jpeg: ByteArray,
    mask: JSONObject,
    points: List<FloatArray>,
    onFailed: (String) -> Unit,
    onStarted: () -> Unit,
  ) {
    val depth = c.depth
    if (depth == null) {
      onFailed("No depth for this frame (${c.depthNote}). Move sideways slowly until depth appears, then select again.")
      return
    }
    worker.execute {
      val extent = try {
        val bits = CameraImages.decodeMask(mask.getString("mask_png_b64"), c.frame.width, c.frame.height)
        SeedExtent.compute(SurfaceGeometry.Mask(c.frame.width, c.frame.height, bits), depth, c.cameraToWorld, c.worldToAnchor, c.anchorPoseWorld)
      } catch (e: Exception) {
        Log.w(TAG, "seed extent failed", e)
        null
      }
      activity.runOnUiThread {
        if (extent == null) {
          onFailed("Not enough depth on the object to place it in 3D. Move sideways slowly, then select again.")
          return@runOnUiThread
        }
        val id = "${c.frame.sessionId}_obj${c.selectionId}_${System.currentTimeMillis() % 100_000_000}"
        val dir = File(activity.getExternalFilesDir("captures") ?: activity.filesDir, id)
        val s = ObjectCaptureSession(id, c, jpeg, mask.getString("mask_png_b64"), points, extent, dir, worker,
          activity.diagnostics.rawDepthSupported, label = null)
        worker.execute { s.writeSeed() }
        session = s
        onStarted()
        mainBar.visibility = View.GONE
        captureBar.visibility = View.VISIBLE
        updateCaptureProgress(1, 0, 0)
        activity.startObjectCapture(s)
        activity.showBanner(String.format(Locale.US, "Walk slowly around the object (about %.1f m away). The ring fills in as you cover each angle.",
          extent.ringRadiusM + 0.6f))
      }
    }
  }

  /** From the renderer (any thread), a few times per second. */
  fun updateCaptureProgress(frames: Int, covered: Int, sectors: Int) = activity.runOnUiThread {
    finishButton.text = if (sectors > 0) "Finish ($frames frames, $covered/$sectors)" else "Finish ($frames frames)"
    finishButton.isEnabled = frames >= MIN_FRAMES
  }

  private fun cancelCapture() {
    if (session == null) return
    activity.stopObjectCapture { stopped ->
      session = null
      activity.releaseSelectionAnchor(stopped.selection.anchor)
      worker.execute { stopped.dir.deleteRecursively() }
      captureBar.visibility = View.GONE
      mainBar.visibility = View.VISIBLE
      activity.showBanner("Capture cancelled.")
    }
  }

  private fun finishCapture() {
    if (session == null) return
    activity.stopObjectCapture { stopped ->
      session = null
      inFlight = stopped
      captureBar.visibility = View.GONE
      mainBar.visibility = View.VISIBLE
      cancelled = false
      showJob("Packaging ${stopped.frameCount} frames…", 0f)
      worker.execute { packageAndUpload(stopped) }
    }
  }

  // ---------------------------------------------------------------- worker thread

  private fun packageAndUpload(s: ObjectCaptureSession) {
    if (cancelled) return
    val zip = File(s.dir.parentFile, "${s.captureId}.zip")
    val manifest: JSONObject
    try {
      s.writeError?.let { throw IllegalStateException("saving frames failed: $it") }
      manifest = s.manifest()
      File(s.dir, "manifest.json").writeText(manifest.toString(2))
      zip.outputStream().buffered().use { CaptureFormat.writeZip(s.dir, it) }
    } catch (e: Exception) {
      Log.e(TAG, "packaging failed", e)
      fail("Could not package the capture: ${e.message}", s)
      return
    }
    upload(s, manifest, zip, attempt = 1)
  }

  private fun upload(s: ObjectCaptureSession, firstManifest: JSONObject, zip: File, attempt: Int) {
    val mb = zip.length() / 1_000_000.0
    // The Mac treats capture_id as unique (409 on reuse), so retries get a suffixed ID.
    val manifest = if (attempt == 1) firstManifest else s.manifest("${s.captureId}_r$attempt")
    jobId = null
    try {
      var lastUi = 0L
      val resp = client.uploadCapture(manifest, zip) { sent, total ->
        if (cancelled) throw java.io.IOException("cancelled")
        val now = System.currentTimeMillis()
        if (now - lastUi > 200) {
          lastUi = now
          showJob(String.format(Locale.US, "Uploading %.1f / %.1f MB to the Mac…", sent / 1e6, total / 1e6), sent.toFloat() / total)
        }
      }
      val id = resp.getString("job_id")
      jobId = id
      poll(s, id)
    } catch (e: Exception) {
      if (cancelled) return // cancelJob() hides the card and releases the anchor
      Log.e(TAG, "upload failed", e)
      fail(String.format(Locale.US, "Upload failed (%.1f MB): %s", mb, e.message?.take(160)), s) { retry { upload(s, firstManifest, zip, attempt + 1) } }
    }
  }

  private fun poll(s: ObjectCaptureSession, id: String) {
    var failures = 0
    var lastProgress = 0f
    while (!cancelled) {
      val job = try {
        client.job(id).also { failures = 0 }
      } catch (e: MacServiceClient.ServiceException) {
        fail("The Mac no longer has this capture's job: ${e.message?.take(100)}", s)
        return
      } catch (e: Exception) {
        // Unplugged or off Wi‑Fi: the Mac keeps working, so keep waiting rather than giving up.
        if (++failures >= 3 && !cancelled) {
          jobs.show("Can't reach the Mac right now. It keeps building the mesh: plug the phone back in and run " +
            "adb reverse tcp:8000 tcp:8000 (or reconnect Wi‑Fi), and this carries on by itself.", lastProgress) { cancelJob() }
        }
        Thread.sleep(3000)
        continue
      }
      val state = job.optString("state")
      val progress = job.optDouble("progress", 0.0).toFloat()
      val msg = job.optString("message").ifBlank { JobCard.STAGES[state] ?: state }
      when (state) {
        "done" -> {
          addToLibrary(s, id)
          return
        }
        "failed" -> {
          fail("The Mac could not build the mesh: ${job.optString("error").ifBlank { msg }}", s)
          return
        }
        "cancelled" -> {
          inFlight = null
          activity.releaseSelectionAnchor(s.selection.anchor)
          jobs.hide("Mesh job cancelled.")
          return
        }
        else -> {
          lastProgress = progress
          if (!cancelled) jobs.show("Building the mesh on the Mac (you can unplug and come back): $msg", progress) { cancelJob() }
        }
      }
      Thread.sleep(1500)
    }
  }

  /** Job done: the selection anchor is no longer needed; name the object and add it to My stuff. */
  private fun addToLibrary(s: ObjectCaptureSession, jobId: String) {
    inFlight = null
    activity.releaseSelectionAnchor(s.selection.anchor)
    activity.runOnUiThread {
      jobs.hide(null)
      activity.askName("Name this object", "e.g. desk lamp",
        onCancel = { activity.showBanner("Not added to My stuff. Capture it again whenever you like.") }) { name ->
        jobs.show("Adding $name to My stuff…", 1f) {}
        worker.execute {
          try {
            client.addToLibrary(name, JSONObject().put("job_id", jobId))
            jobs.hide("$name added to My stuff.")
            activity.library.syncAndShow()
          } catch (e: Exception) {
            Log.e(TAG, "add to library failed", e)
            jobs.fail("Could not add $name to My stuff: ${e.message?.take(140)}", { retry { addToLibrary(s, jobId) } })
          }
        }
      }
    }
  }

  /** Cancel packaging, upload or Mac processing. Runs after the worker's current step stops. */
  private fun cancelJob() {
    cancelled = true
    hideJob("Cancelled. The capture stays saved on the phone.")
    worker.execute {
      jobId?.let { client.cancelJob(it) }
      jobId = null
      inFlight?.let { activity.releaseSelectionAnchor(it.selection.anchor) }
      inFlight = null
    }
  }

  private fun retry(action: () -> Unit) {
    cancelled = false
    worker.execute(action)
  }

  private fun showJob(text: String, progress: Float) = jobs.show(text, progress) { cancelJob() }

  private fun hideJob(message: String) = jobs.hide(message)

  private fun fail(message: String, s: ObjectCaptureSession, retryAction: (() -> Unit)? = null) =
    jobs.fail(message + "\nThe capture is saved on the phone (${s.captureId}).", retryAction) {
      inFlight?.let { activity.releaseSelectionAnchor(it.selection.anchor) }
      inFlight = null
    }

  companion object {
    private const val TAG = "MeshController"
    const val MIN_FRAMES = 12

    /** Absolute URL, service-absolute path (the Mac returns /v1/jobs/<id>/files/…), or job-relative path. */
    fun resolveOutputUrl(raw: String, jobId: String): String = when {
      raw.startsWith("http://") || raw.startsWith("https://") || raw.startsWith("/") -> raw
      raw.startsWith("v1/") -> "/$raw"
      else -> "/v1/jobs/$jobId/$raw"
    }

    /** Depth-first search for a key anywhere in a JSON document. */
    fun findValue(json: Any?, key: String): Any? {
      when (json) {
        is JSONObject -> {
          if (json.has(key) && !json.isNull(key)) return json.get(key)
          for (k in json.keys()) findValue(json.get(k), key)?.let { return it }
        }
        is JSONArray -> for (i in 0 until json.length()) findValue(json.get(i), key)?.let { return it }
      }
      return null
    }

    /**
     * The matrix stored under [key], returned column-major. A flat 16-element array is taken as
     * column-major (the agreed convention); 4 nested arrays are taken as rows (numpy's tolist()).
     */
    fun findMatrix(json: JSONObject, key: String): FloatArray? {
      val v = findValue(json, key) as? JSONArray ?: return null
      val out = FloatArray(16)
      if (v.length() == 16) {
        for (i in 0 until 16) out[i] = v.getDouble(i).toFloat()
      } else if (v.length() == 4 && (0 until 4).all { (v.get(it) as? JSONArray)?.length() == 4 }) {
        for (r in 0 until 4) for (c in 0 until 4) out[c * 4 + r] = v.getJSONArray(r).getDouble(c).toFloat()
      } else {
        return null
      }
      return if (out.all { it.isFinite() }) out else null
    }
  }
}
