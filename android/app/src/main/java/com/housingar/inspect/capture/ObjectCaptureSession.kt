package com.housingar.inspect.capture

import android.media.Image
import android.util.Base64
import android.util.Log
import com.google.ar.core.Camera
import com.google.ar.core.Frame
import com.google.ar.core.TrackingState
import com.housingar.inspect.SelectionCapture
import org.json.JSONObject
import java.io.File
import java.util.Collections
import java.util.concurrent.ExecutorService
import kotlin.math.abs
import kotlin.math.acos

/**
 * One guided walk-around capture ("Create mesh"). [onFrame] runs on the GL thread; file writes go
 * to [io] (a single-thread executor, so they complete in order before packaging).
 */
class ObjectCaptureSession(
  val captureId: String,
  val selection: SelectionCapture,
  private val seedJpeg: ByteArray,
  private val seedMaskPngBase64: String,
  private val seedPoints: List<FloatArray>,
  val extent: SeedExtent,
  val dir: File,
  private val io: ExecutorService,
  private val rawDepthSupported: Boolean,
  val label: String?,
) {
  val coverage = OrbitCoverage(extent.centerWorld)
  val policy = KeyframePolicy(coverage)
  private val frames: MutableList<CaptureFormat.Frame> = Collections.synchronizedList(mutableListOf())
  @Volatile var guidance: String = "Walk slowly around the object."
    private set
  @Volatile var writeError: String? = null
    private set
  val frameCount get() = frames.size

  private var lastDepthTimestamp = -1L
  private var prevRotation: FloatArray? = null
  private var prevTimestampNs = 0L
  private var angularSpeed = 0f

  init {
    File(dir, "frames").mkdirs()
    File(dir, "depth").mkdirs()
    File(dir, "confidence").mkdirs()
  }

  /** Frame 0: the user-approved selection frame, its mask and its (smoothed) depth. */
  fun writeSeed() {
    val c = selection
    val depth = c.depth ?: throw IllegalStateException("seed frame has no depth")
    val f = c.frame
    val cameraToAnchor = multiply(c.worldToAnchor, c.cameraToWorld)
    val luma = f.nv21.copyOfRange(0, f.width * f.height)
    val sharp = KeyframePolicy.laplacianVariance(luma, f.width, f.height)
    val camPos = floatArrayOf(c.cameraToWorld[12], c.cameraToWorld[13], c.cameraToWorld[14])
    policy.onSeed(camPos, f.timestampNs, sharp)
    val imageK = CaptureFormat.Intrinsics(f.intrinsics[0], f.intrinsics[1], f.intrinsics[2], f.intrinsics[3])
    val crop = CaptureFormat.cropDepthToImage(depth.width, depth.height, depth.cpuRegion, f.width, f.height, imageK)
      ?: throw IllegalStateException("seed depth does not overlap the camera image")
    frames.add(CaptureFormat.Frame(
      index = 0, timestampNs = f.timestampNs, image = "frames/0000.jpg", width = f.width, height = f.height,
      intrinsics = imageK, cameraToAnchor = cameraToAnchor, cameraToWorld = c.cameraToWorld,
      depth = CaptureFormat.Depth("depth/0000.bin", crop.width, crop.height, f.depthTimestampNs ?: f.timestampNs, crop.intrinsics, crop.toImage),
      confidenceFile = null, sharpness = sharp, orbitDeg = coverage.azimuthDeg(camPos),
    ))
    val seedDepth = crop.crop(depth.millimeters, depth.width)
    io.execute {
      guard {
        File(dir, "frames/0000.jpg").writeBytes(seedJpeg)
        File(dir, "depth/0000.bin").writeBytes(CaptureFormat.depthBytes(seedDepth))
        File(dir, "seed_mask.png").writeBytes(Base64.decode(seedMaskPngBase64, Base64.DEFAULT))
      }
    }
  }

  /** GL thread, once per frame while capturing. [viewProjection] is the current display VP matrix. */
  fun onFrame(frame: Frame, camera: Camera, viewProjection: FloatArray) {
    val pose = camera.pose
    val q = pose.rotationQuaternion
    prevRotation?.let { p ->
      val dt = (frame.timestamp - prevTimestampNs) / 1e9f
      if (dt > 0f) {
        val dot = abs(p[0] * q[0] + p[1] * q[1] + p[2] * q[2] + p[3] * q[3]).coerceAtMost(1f)
        val deg = Math.toDegrees(2.0 * acos(dot.toDouble())).toFloat()
        angularSpeed = 0.7f * angularSpeed + 0.3f * (deg / dt)
      }
    }
    prevRotation = q.copyOf()
    prevTimestampNs = frame.timestamp

    val anchor = selection.anchor
    if (anchor.trackingState != TrackingState.TRACKING) {
      guidance = "Lost the object's anchor: point back at the object and move slowly."
      return
    }
    val camPos = pose.translation
    val candidate = FrameCandidate(frame.timestamp, camera.trackingState == TrackingState.TRACKING, camPos,
      projectNdc(viewProjection, extent.centerWorld), angularSpeed)
    val decision = policy.evaluate(candidate)
    decision.guidance?.let { guidance = it }
    if (!decision.capture) return

    val kf = when (val r = KeyframeGrabber.grab(frame, camera, anchor, rawDepthSupported, lastDepthTimestamp)) {
      is KeyframeGrabber.Result.Ok -> r.keyframe
      is KeyframeGrabber.Result.Skip -> {
        r.guidance?.let { guidance = it }
        return
      }
    }
    if (!policy.acceptSharpness(kf.sharpness)) {
      guidance = "Hold steady: that frame was blurry."
      return
    }
    maxIntrinsicsDisagreement = maxOf(maxIntrinsicsDisagreement, kf.intrinsicsDisagreement)
    policy.onCaptured(candidate, kf.sharpness)
    lastDepthTimestamp = kf.depthTimestampNs
    val index = frames.size
    val name = "%04d".format(index)
    frames.add(kf.toFrame(index, name, coverage.azimuthDeg(camPos)))
    io.execute { guard { kf.write(dir, name, 92) } }
  }

  /**
   * Largest relative disagreement seen between the texture-derived depth intrinsics (Google's
   * raw-depth sample) and the ones derived from the CPU image through the mapping. Reported, not
   * hidden: a large value means the depth/image alignment assumption is wrong on this device.
   */
  @Volatile var maxIntrinsicsDisagreement = 0f
    private set

  /** [captureIdOverride] gives retries a fresh ID (the Mac rejects a reused capture_id with 409). */
  fun manifest(captureIdOverride: String = captureId): JSONObject {
    val anchorToWorld = selection.anchorPoseWorld
    val snapshot = synchronized(frames) { frames.toList() }
    return CaptureFormat.manifestJson(CaptureFormat.Manifest(
      captureId = captureIdOverride, sessionId = selection.frame.sessionId, label = label, anchorToWorld = anchorToWorld,
      seedFrameIndex = 0, seedPoints = seedPoints, seedMask = "seed_mask.png",
      bboxMin = extent.bboxMinAnchor, bboxMax = extent.bboxMaxAnchor, frames = snapshot,
    ))
  }

  private inline fun guard(block: () -> Unit) {
    try {
      block()
    } catch (e: Exception) {
      Log.e(TAG, "capture write failed", e)
      writeError = e.toString()
    }
  }

  companion object {
    private const val TAG = "ObjectCapture"

    fun projectNdc(vp: FloatArray, p: FloatArray): FloatArray? {
      val x = vp[0] * p[0] + vp[4] * p[1] + vp[8] * p[2] + vp[12]
      val y = vp[1] * p[0] + vp[5] * p[1] + vp[9] * p[2] + vp[13]
      val w = vp[3] * p[0] + vp[7] * p[1] + vp[11] * p[2] + vp[15]
      if (w <= 1e-6f) return null
      return floatArrayOf(x / w, y / w)
    }

    /** Column-major a × b. */
    fun multiply(a: FloatArray, b: FloatArray): FloatArray = FloatArray(16).also { android.opengl.Matrix.multiplyMM(it, 0, a, 0, b, 0) }
  }
}
