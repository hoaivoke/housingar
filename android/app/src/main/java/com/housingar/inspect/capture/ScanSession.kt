package com.housingar.inspect.capture

import android.util.Log
import com.google.ar.core.Anchor
import com.google.ar.core.Camera
import com.google.ar.core.Frame
import com.google.ar.core.TrackingState
import org.json.JSONObject
import java.io.File
import java.util.Collections
import java.util.concurrent.ExecutorService
import kotlin.math.abs
import kotlin.math.acos

/**
 * "Scan stuff": a walk-through recording of the user's belongings. The user taps each thing they
 * want ([addObject]); the tap's frame becomes that object's seed keyframe, and every later keyframe
 * that sees the object adds to its coverage. Packaged as housingar.scan.v2 (v1 when nothing was
 * tapped). Keyframes (image + newest depth + confidence + poses) are relative to a level scan
 * [anchor]. [onFrame] and [addObject] run on the GL thread; files are written on [io] in order.
 */
class ScanSession(
  val scanId: String,
  val sessionId: String,
  val anchor: Anchor,
  /** Anchor pose at the start of the scan (column-major anchor → world). */
  val anchorToWorld: FloatArray,
  val dir: File,
  private val io: ExecutorService,
  private val rawDepthSupported: Boolean,
  val label: String?,
) {
  val policy = ScanPolicy()
  private val frames: MutableList<CaptureFormat.Frame> = Collections.synchronizedList(mutableListOf())
  private val tapped = java.util.concurrent.CopyOnWriteArrayList<TappedObject>()
  val objects: List<TappedObject> get() = tapped
  private var nextNumber = 1
  /** Short-lived capture advice (blur, speed); otherwise guidance follows the tapped objects. */
  @Volatile private var advice: String? = null
  @Volatile private var adviceUntilNs = 0L
  private var nowNs = 0L

  val guidance: String
    get() {
      advice?.takeIf { nowNs < adviceUntilNs }?.let { return it }
      return coverageGuidance(tapped)
    }
  @Volatile var writeError: String? = null
    private set
  @Volatile var maxIntrinsicsDisagreement = 0f
    private set
  val frameCount get() = frames.size
  val distanceWalkedM get() = policy.distanceWalkedM
  private var lastDepthTimestamp = -1L
  private var prevRotation: FloatArray? = null
  private var prevTimestampNs = 0L
  private var angularSpeed = 0f

  init {
    dir.mkdirs()
  }

  fun onFrame(frame: Frame, camera: Camera) {
    val pose = camera.pose
    val q = pose.rotationQuaternion
    prevRotation?.let { p ->
      val dt = (frame.timestamp - prevTimestampNs) / 1e9f
      if (dt > 0f) {
        val dot = abs(p[0] * q[0] + p[1] * q[1] + p[2] * q[2] + p[3] * q[3]).coerceAtMost(1f)
        angularSpeed = 0.7f * angularSpeed + 0.3f * (Math.toDegrees(2.0 * acos(dot.toDouble())).toFloat() / dt)
      }
    }
    prevRotation = q.copyOf()
    prevTimestampNs = frame.timestamp
    nowNs = frame.timestamp
    if (anchor.trackingState != TrackingState.TRACKING) {
      advise("Lost the scan's reference point: move slowly and point at textured surfaces.")
      return
    }
    val z = pose.zAxis
    val candidate = ScanCandidate(frame.timestamp, camera.trackingState == TrackingState.TRACKING, pose.translation,
      floatArrayOf(-z[0], -z[1], -z[2]), angularSpeed)
    val decision = policy.evaluate(candidate)
    decision.guidance?.let { advise(it) }
    if (!decision.capture) return

    val kf = when (val r = KeyframeGrabber.grab(frame, camera, anchor, rawDepthSupported, lastDepthTimestamp)) {
      is KeyframeGrabber.Result.Ok -> r.keyframe
      is KeyframeGrabber.Result.Skip -> {
        r.guidance?.let { advise(it) }
        return
      }
    }
    if (!policy.acceptSharpness(kf.sharpness)) {
      advise("Hold steady: that frame was blurry.")
      return
    }
    policy.onCaptured(candidate, kf.sharpness)
    addKeyframe(kf)
  }

  sealed class AddResult {
    /** [newThing]: this tap started [obj]; otherwise it added a point to it. */
    class Ok(val obj: TappedObject, val keyframe: GrabbedKeyframe, val newThing: Boolean) : AddResult()
    class Failed(val message: String) : AddResult()
  }

  /** Taps go to the latest thing until [startNewThing]; the first tap of a scan always starts one. */
  @Volatile private var newThingNext = true

  /** The next tap starts another thing. */
  fun startNewThing() {
    newThingNext = true
  }

  val nextTapStartsNewThing get() = newThingNext || tapped.isEmpty()

  /**
   * GL thread: the user tapped a thing at [pointWorld] ([imagePoint] in this frame's CPU image).
   * This frame is kept as the object's seed keyframe, whatever the keyframe spacing rules say.
   */
  fun addObject(frame: Frame, camera: Camera, imagePoint: FloatArray, pointWorld: FloatArray): AddResult {
    if (anchor.trackingState != TrackingState.TRACKING) return AddResult.Failed("Tracking isn't stable: move slowly, then tap again.")
    val current = tapped.lastOrNull()?.takeIf { !newThingNext }
    if (current == null && tapped.size >= MAX_OBJECTS) return AddResult.Failed("That's $MAX_OBJECTS things already: finish this scan and start another.")
    if (current != null && current.tapCount >= MAX_TAPS) return AddResult.Failed("That's enough taps on thing ${current.number}. Press New thing for the next one.")
    if (frames.size >= policy.maxFrames) return AddResult.Failed("The scan is full. Press Finish scan.")
    val kf = when (val r = KeyframeGrabber.grab(frame, camera, anchor, rawDepthSupported, -1L)) {
      is KeyframeGrabber.Result.Ok -> r.keyframe
      is KeyframeGrabber.Result.Skip -> return AddResult.Failed(r.guidance ?: "Couldn't use that frame. Hold still and tap again.")
    }
    val worldToAnchor = FloatArray(16).also { anchor.pose.inverse().toMatrix(it, 0) }
    val pa = floatArrayOf(
      worldToAnchor[0] * pointWorld[0] + worldToAnchor[4] * pointWorld[1] + worldToAnchor[8] * pointWorld[2] + worldToAnchor[12],
      worldToAnchor[1] * pointWorld[0] + worldToAnchor[5] * pointWorld[1] + worldToAnchor[9] * pointWorld[2] + worldToAnchor[13],
      worldToAnchor[2] * pointWorld[0] + worldToAnchor[6] * pointWorld[1] + worldToAnchor[10] * pointWorld[2] + worldToAnchor[14],
    )
    val index = frames.size
    val obj = if (current == null) {
      TappedObject(nextNumber++, pointWorld.copyOf(), pa, index, imagePoint.copyOf()).also { tapped.add(it); newThingNext = false }
    } else {
      current.addTap(pointWorld, index, imagePoint) { p ->
        PointVisibility.project(p, kf.cameraToWorld, kf.imageK, kf.width, kf.height, kf.depthMm, kf.crop.width, kf.crop.height, kf.crop.toImage)
      }
      current
    }
    val pose = camera.pose
    val z = pose.zAxis
    policy.onCaptured(ScanCandidate(frame.timestamp, true, pose.translation, floatArrayOf(-z[0], -z[1], -z[2]), angularSpeed), kf.sharpness)
    addKeyframe(kf)
    obj.coverage.add(floatArrayOf(kf.cameraToWorld[12], kf.cameraToWorld[13], kf.cameraToWorld[14])) // the tap's own view always counts
    return AddResult.Ok(obj, kf, current == null)
  }

  /** What Undo tap did: removed a tap from [obj], or ([thingRemoved]) the whole thing. */
  class UndoResult(val obj: TappedObject, val thingRemoved: Boolean)

  /** Undo the latest tap (its keyframe stays as an ordinary frame). */
  fun undoTap(): UndoResult? {
    val last = tapped.lastOrNull() ?: return null
    if (last.undoTap()) return UndoResult(last, false)
    tapped.remove(last)
    newThingNext = true
    return UndoResult(last, true)
  }

  private fun addKeyframe(kf: GrabbedKeyframe) {
    maxIntrinsicsDisagreement = maxOf(maxIntrinsicsDisagreement, kf.intrinsicsDisagreement)
    lastDepthTimestamp = kf.depthTimestampNs
    val index = frames.size
    val name = "%04d".format(index)
    frames.add(kf.toFrame(index, name, 0f))
    val camPos = floatArrayOf(kf.cameraToWorld[12], kf.cameraToWorld[13], kf.cameraToWorld[14])
    for (o in tapped) {
      if (o.seedFrameIndex == index) continue
      // In view if any of its taps is (a chair's seat can be hidden while its back is seen).
      if (o.tapPoints.any { PointVisibility.visible(it, kf.cameraToWorld, kf.imageK, kf.width, kf.height, kf.depthMm, kf.crop.width, kf.crop.height, kf.crop.toImage) }) {
        o.coverage.add(camPos)
      }
    }
    io.execute {
      try {
        kf.write(dir, name, JPEG_QUALITY)
      } catch (e: Exception) {
        Log.e(TAG, "scan write failed", e)
        writeError = e.toString()
      }
    }
  }

  private fun advise(text: String) {
    advice = text
    adviceUntilNs = nowNs + 1_500_000_000L
  }

  /** [scanIdOverride] gives retries a fresh ID (the Mac rejects a reused scan_id with 409). */
  fun manifest(scanIdOverride: String = scanId): JSONObject {
    val snapshot = synchronized(frames) { frames.toList() }
    return CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest(scanIdOverride, sessionId, label, anchorToWorld, snapshot,
      tapped.map { it.toSeed() }))
  }

  companion object {
    private const val TAG = "ScanSession"
    /** Lower than single-object capture (92): scans have many more frames. */
    const val JPEG_QUALITY = 85
    const val MIN_FRAMES = 30
    const val MAX_OBJECTS = 30
    const val MAX_TAPS = 8

    /** What to tell the user next, given the tapped objects' coverage. */
    fun coverageGuidance(objects: List<TappedObject>): String {
      if (objects.isEmpty()) return "Tap each thing you want to keep, then walk around it."
      val worst = objects.minByOrNull { it.coverage.coverageDeg + it.coverage.views }!!
      return if (worst.coverage.ready) {
        "All ${objects.size} covered. Tap more things, or press Finish scan."
      } else {
        String.format(java.util.Locale.US, "Thing %d: seen from %.0f° so far. Walk around it to film its other sides.", worst.number, worst.coverage.coverageDeg)
      }
    }
  }
}
