package com.housingar.inspect

import android.annotation.SuppressLint
import android.graphics.Matrix
import android.util.Log
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.ImageView
import android.widget.TextView
import com.google.ar.core.Anchor
import com.housingar.inspect.geometry.ImageRotation
import com.housingar.inspect.geometry.SurfaceGeometry
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Locale
import java.util.concurrent.ExecutorService

/**
 * Everything captured on the GL thread at the moment of a "Select surface" tap. Geometry is later
 * built only from this data, never from the live camera pose.
 */
class SelectionCapture(
  val selectionId: Int,
  val frame: CapturedFrame,
  val anchor: Anchor,
  val anchorSource: String,
  val anchorPoseWorld: FloatArray,
  val worldToAnchor: FloatArray,
  val cameraToWorld: FloatArray,
  val depth: SurfaceGeometry.DepthImage?,
  val depthNote: String,
  val fallbackPlane: SurfaceGeometry.PlaneEstimate?,
)

/**
 * Milestone 2/3 flow: tap → frozen-frame review with point corrections → accept → anchored
 * geometry. UI methods run on the UI thread; network and geometry work on [worker].
 */
class SurfaceSelectionController(
  private val activity: InspectActivity,
  private val client: MacServiceClient,
  private val worker: ExecutorService,
) {
  private enum class State { IDLE, SEGMENTING, REVIEW, BUILDING }

  private val panel: View = activity.findViewById(R.id.review_panel)
  private val image: ImageView = activity.findViewById(R.id.review_image)
  private val status: TextView = activity.findViewById(R.id.review_status)
  private val addButton: Button = activity.findViewById(R.id.review_add)
  private val removeButton: Button = activity.findViewById(R.id.review_remove)
  private val nextButton: Button = activity.findViewById(R.id.review_next)
  private val undoButton: Button = activity.findViewById(R.id.review_undo)
  private val acceptButton: Button = activity.findViewById(R.id.review_accept)
  private val cancelButton: Button = activity.findViewById(R.id.review_cancel)
  private val createMeshButton: Button = activity.findViewById(R.id.review_create_mesh)

  // UI-thread state
  private var state = State.IDLE
  private var capture: SelectionCapture? = null
  private var jpeg: ByteArray? = null
  private val points = mutableListOf<FloatArray>() // x, y, label (1 include, 0 exclude)
  private var nextLabel = 1f
  private var response: JSONObject? = null
  private var maskIndex = 0
  private var requestSeq = 0
  private var lastRoundTripMs = 0L

  /** Notified on the UI thread when a selection review opens or closes. */
  var onActiveChanged: ((Boolean) -> Unit)? = null

  init {
    addButton.setOnClickListener { nextLabel = 1f; refreshButtons() }
    removeButton.setOnClickListener { nextLabel = 0f; refreshButtons() }
    undoButton.setOnClickListener {
      if (state == State.REVIEW && points.size > 1) {
        points.removeAt(points.size - 1)
        requestMask()
      }
    }
    nextButton.setOnClickListener {
      val masks = response?.optJSONArray("masks") ?: return@setOnClickListener
      if (state != State.REVIEW || masks.length() < 2) return@setOnClickListener
      maskIndex = (maskIndex + 1) % masks.length()
      showResult()
    }
    acceptButton.setOnClickListener { accept() }
    createMeshButton.setOnClickListener { createMesh() }
    cancelButton.setOnClickListener { cancel("Selection cancelled.") }
    image.setOnTouchListener(::onImageTouch)
  }

  val isActive get() = state != State.IDLE

  /** Called from the GL thread with a fresh capture. */
  fun begin(c: SelectionCapture) = activity.runOnUiThread {
    if (state != State.IDLE) cancel(null) // a new selection supersedes the old one
    capture = c
    jpeg = null
    response = null
    maskIndex = 0
    points.clear()
    points.add(floatArrayOf(c.frame.pointX, c.frame.pointY, 1f))
    nextLabel = 1f
    panel.visibility = View.VISIBLE
    state = State.SEGMENTING
    onActiveChanged?.invoke(true)
    status.text = "Segmenting on the Mac…"
    refreshButtons()
    val frame = c.frame
    worker.execute {
      val bytes = frame.toJpeg() // encoded once: corrections must resend identical bytes
      activity.runOnUiThread {
        if (capture?.selectionId != c.selectionId) return@runOnUiThread
        jpeg = bytes
        image.setImageBitmap(CameraImages.maskPreview(bytes, null, points, frame.sensorToDisplayRotation))
        requestMask()
      }
    }
  }

  fun cancel(message: String?) {
    val c = capture
    capture = null
    state = State.IDLE
    response = null
    panel.visibility = View.GONE
    onActiveChanged?.invoke(false)
    if (c != null) activity.releaseSelectionAnchor(c.anchor)
    if (message != null) activity.showBanner(message)
  }

  private fun requestMask() {
    val c = capture ?: return
    val bytes = jpeg ?: return
    state = State.SEGMENTING
    val seq = ++requestSeq
    val prompt = points.map { it.copyOf() }
    status.text = if (prompt.size == 1) "Segmenting on the Mac…" else "Updating selection with ${prompt.size} points…"
    refreshButtons()
    worker.execute {
      val started = System.nanoTime()
      val result = try {
        Result.success(client.segment(c.frame.frameId, bytes, prompt, c.frame.metadata()))
      } catch (e: Exception) {
        Result.failure(e)
      }
      val ms = (System.nanoTime() - started) / 1_000_000
      activity.runOnUiThread {
        // Discard results for superseded selections or stale requests.
        if (capture?.selectionId != c.selectionId || seq != requestSeq) return@runOnUiThread
        lastRoundTripMs = ms
        state = State.REVIEW
        result.onSuccess { json ->
          response = json
          maskIndex = 0
          showResult()
        }.onFailure { e ->
          Log.w(TAG, "segment failed", e)
          response = null
          status.text = "Mac service error: ${e.message?.take(160)}\nCheck the service URL in settings, then tap the image to retry."
          refreshButtons()
        }
      }
    }
  }

  private fun showResult() {
    val c = capture ?: return
    val bytes = jpeg ?: return
    val json = response ?: return
    val masks = json.optJSONArray("masks")
    if (masks == null || masks.length() == 0) {
      status.text = "The model returned no mask. Tap the surface again."
      refreshButtons()
      return
    }
    val m = masks.getJSONObject(maskIndex)
    image.setImageBitmap(CameraImages.maskPreview(bytes, m.getString("mask_png_b64"), points, c.frame.sensorToDisplayRotation))
    val score = m.optDouble("score")
    val area = m.optDouble("area_fraction")
    val cached = json.optBoolean("embedding_cached")
    val timing = json.optJSONObject("timing_ms")
    val warn = when {
      area <= 0.0 -> "\nEmpty mask: tap inside the surface."
      score < 0.5 -> "\nLow confidence: add or remove points before anchoring."
      area > 0.6 -> "\nMask covers most of the frame: add exclude points on other surfaces."
      else -> ""
    }
    status.text = String.format(
      Locale.US,
      "Mask %d/%d  score %.2f  area %.0f%%  %d ms round trip (model %.0f ms%s)  depth: %s%s\nTap the photo to %s a point.",
      maskIndex + 1, masks.length(), score, area * 100, lastRoundTripMs,
      (timing?.optDouble("embed") ?: 0.0) + (timing?.optDouble("predict") ?: 0.0), if (cached) ", cached" else "",
      c.depthNote, warn, if (nextLabel > 0.5f) "add an include" else "add an exclude",
    )
    refreshButtons()
  }

  @SuppressLint("ClickableViewAccessibility")
  private fun onImageTouch(v: View, e: MotionEvent): Boolean {
    if (e.action != MotionEvent.ACTION_UP) return true
    val c = capture ?: return true
    if (state == State.SEGMENTING || state == State.BUILDING) return true
    if (state == State.REVIEW && response == null) { // retry after an error
      requestMask()
      return true
    }
    val drawable = image.drawable ?: return true
    val inv = Matrix()
    if (!image.imageMatrix.invert(inv)) return true
    val pt = floatArrayOf(e.x, e.y)
    inv.mapPoints(pt)
    if (pt[0] < 0 || pt[1] < 0 || pt[0] > drawable.intrinsicWidth || pt[1] > drawable.intrinsicHeight) return true
    val orig = ImageRotation.fromRotated(pt[0], pt[1], c.frame.width, c.frame.height, c.frame.sensorToDisplayRotation)
    val x = orig[0].coerceIn(0f, c.frame.width - 1f)
    val y = orig[1].coerceIn(0f, c.frame.height - 1f)
    points.add(floatArrayOf(x, y, nextLabel))
    requestMask()
    v.performClick()
    return true
  }

  private fun accept() {
    val c = capture ?: return
    val json = response ?: return
    if (state != State.REVIEW) return
    val masks = json.optJSONArray("masks") ?: return
    if (masks.length() == 0) return
    val chosen = masks.getJSONObject(maskIndex)
    state = State.BUILDING
    status.text = "Fitting the surface from depth…"
    refreshButtons()
    val prompt = points.map { it.copyOf() }
    worker.execute {
      val outcome = try {
        val mask = SurfaceGeometry.Mask(c.frame.width, c.frame.height, CameraImages.decodeMask(chosen.getString("mask_png_b64"), c.frame.width, c.frame.height))
        val poly = chosen.optJSONArray("polygon") ?: JSONArray()
        val outline = (0 until poly.length()).map { i -> poly.getJSONArray(i).let { floatArrayOf(it.getDouble(0).toFloat(), it.getDouble(1).toFloat()) } }
        val k = c.frame.intrinsics
        SurfaceGeometry.build(
          SurfaceGeometry.Input(
            mask = mask,
            outline = outline,
            cpuIntrinsics = SurfaceGeometry.Intrinsics(k[0], k[1], k[2], k[3], c.frame.width, c.frame.height),
            depth = c.depth,
            cameraToWorld = c.cameraToWorld,
            worldToAnchor = c.worldToAnchor,
            fallbackPlane = c.fallbackPlane,
          )
        )
      } catch (e: Exception) {
        Log.w(TAG, "geometry failed", e)
        null
      }
      activity.runOnUiThread {
        if (capture?.selectionId != c.selectionId) return@runOnUiThread
        when (outcome) {
          is SurfaceGeometry.Outcome.Success -> {
            val s = outcome.surface
            activity.addAnchoredSurface(c.selectionId, c.anchor, s)
            saveGeometry(c, chosen, prompt, s)
            capture = null // anchor now owned by the renderer
            state = State.IDLE
            panel.visibility = View.GONE
            onActiveChanged?.invoke(false)
            activity.showBanner(String.format(Locale.US,
              "Surface %d anchored: %.2f m², fit %.1f cm rms from %s. Pan away and back to check it stays put.",
              c.selectionId, s.areaM2, s.stats.rmsM * 100, if (s.stats.source == "arcore_plane") "ARCore plane" else "${s.stats.inliers} depth points"))
          }
          is SurfaceGeometry.Outcome.Failed -> {
            state = State.REVIEW
            val st = outcome.stats
            status.text = outcome.reason.userMessage + (st?.let { "\n(${it.validDepthPixels} depth samples in mask, ${it.inliers} on the plane)" } ?: "") +
              "\nCorrect the mask, or Cancel and select again."
            refreshButtons()
          }
          null -> {
            state = State.REVIEW
            status.text = "Could not build geometry from this mask. Cancel and select again."
            refreshButtons()
          }
        }
      }
    }
  }

  /** Hands the selection (and its anchor) to the guided walk-around capture. */
  private fun createMesh() {
    val c = capture ?: return
    val json = response ?: return
    val bytes = jpeg ?: return
    if (state != State.REVIEW) return
    val masks = json.optJSONArray("masks") ?: return
    if (masks.length() == 0) return
    val chosen = masks.getJSONObject(maskIndex)
    state = State.BUILDING
    status.text = "Measuring the object from depth…"
    refreshButtons()
    activity.meshes.begin(c, bytes, chosen, points.map { it.copyOf() },
      onFailed = { message ->
        if (capture?.selectionId == c.selectionId) {
          state = State.REVIEW
          status.text = message
          refreshButtons()
        }
      },
      onStarted = {
        // The capture session now owns the anchor: close the review without releasing it.
        capture = null
        response = null
        state = State.IDLE
        panel.visibility = View.GONE
        onActiveChanged?.invoke(false)
      })
  }

  private fun refreshButtons() {
    val reviewing = state == State.REVIEW && response != null
    addButton.isEnabled = reviewing
    removeButton.isEnabled = reviewing
    addButton.alpha = if (nextLabel > 0.5f) 1f else 0.5f
    removeButton.alpha = if (nextLabel < 0.5f) 1f else 0.5f
    undoButton.isEnabled = reviewing && points.size > 1
    nextButton.isEnabled = reviewing && (response?.optJSONArray("masks")?.length() ?: 0) > 1
    acceptButton.isEnabled = reviewing && (response?.optJSONArray("masks")?.length() ?: 0) > 0
    createMeshButton.isEnabled = acceptButton.isEnabled && capture?.depth != null
    cancelButton.isEnabled = true
  }

  /** Observed geometry for review on the Mac (plan demo step 8). */
  private fun saveGeometry(c: SelectionCapture, mask: JSONObject, prompt: List<FloatArray>, s: SurfaceGeometry.Surface) {
    val doc = JSONObject()
      .put("kind", "m3_surface")
      .put("session_id", c.frame.sessionId)
      .put("selection_id", c.selectionId)
      .put("frame_id", c.frame.frameId)
      .put("created_ms", System.currentTimeMillis())
      .put("frame", c.frame.metadata())
      .put("prompt_points", JSONArray(prompt.map { JSONArray(it.map { v -> v.toDouble() }) }))
      .put("mask", JSONObject().put("score", mask.optDouble("score")).put("area_fraction", mask.optDouble("area_fraction")).put("polygon", mask.optJSONArray("polygon")))
      .put("anchor", JSONObject().put("source", c.anchorSource).put("pose_world_at_capture_colmajor", JSONArray(c.anchorPoseWorld.map { it.toDouble() })))
      .put("camera_to_world_colmajor", JSONArray(c.cameraToWorld.map { it.toDouble() }))
      .put("depth_note", c.depthNote)
      .put("plane_world", JSONObject()
        .put("point", JSONArray(s.planeWorld.point.map { it.toDouble() }))
        .put("normal", JSONArray(s.planeWorld.normal.map { it.toDouble() })))
      .put("vertices_anchor_m", JSONArray(s.vertices.map { it.toDouble() }))
      .put("vertices_world_at_capture_m", JSONArray(s.verticesWorldAtCapture.map { it.toDouble() }))
      .put("triangles", JSONArray(s.triangles.toList()))
      .put("area_m2", s.areaM2.toDouble())
      .put("stats", JSONObject()
        .put("source", s.stats.source)
        .put("depth_pixels_in_mask", s.stats.depthPixelsInMask)
        .put("valid_depth_pixels", s.stats.validDepthPixels)
        .put("inliers", s.stats.inliers)
        .put("rms_m", s.stats.rmsM.toDouble())
        .put("median_depth_m", s.stats.medianDepthM.toDouble())
        .put("outline_vertices", s.stats.outlineVertices)
        .put("dropped_outline_vertices", s.stats.droppedOutlineVertices)
        .put("triangulation", s.stats.triangulation))
    worker.execute {
      val dir = activity.getExternalFilesDir("geometry") ?: activity.filesDir
      File(dir, "surface_${c.frame.sessionId}_${c.selectionId}.json").writeText(doc.toString(2))
      client.postJson("/v1/geometry", doc) // best effort; endpoint may not exist yet
    }
  }

  companion object {
    private const val TAG = "SurfaceSelection"
  }
}
