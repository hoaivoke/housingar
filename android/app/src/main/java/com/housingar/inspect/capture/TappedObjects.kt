package com.housingar.inspect.capture

import kotlin.math.atan2
import kotlin.math.floor
import kotlin.math.sqrt

/**
 * One thing the user tapped during "Scan stuff" (scan_v2 `objects[]`). It can be tapped several
 * times (seat, back, armrest…). The Mac's format takes prompt points in a single seed frame, so
 * the latest tap's keyframe becomes the seed and the earlier taps are re-projected into it from
 * their 3D points (those still in view there). The Mac follows the thing through the scan from
 * that seed (and [maskFile], when the phone got SAM's outline at tap time) and builds exactly
 * these things. Pure Kotlin so the rules can be unit tested.
 */
class TappedObject(
  /** 1-based number shown to the user. */
  val number: Int,
  /** First tap, world space: the centre for coverage and the ring. */
  val pointWorld: FloatArray,
  /** First tap in the scan anchor's frame (metres): the Mac keeps the depth connected to it. */
  val pointAnchor: FloatArray,
  seedFrameIndex: Int,
  /** First tap in the seed frame's image pixels (sensor orientation). */
  seedPoint: FloatArray,
) {
  /** The seed for one state of the taps: a keyframe and prompt points [x, y, 1] in it. */
  class Seed(val frameIndex: Int, val points: List<FloatArray>)

  val objectId = "obj_%02d".format(number)
  /** "seeds/<objectId>.png" once SAM's outline for the current seed has been saved. */
  @Volatile var maskFile: String? = null
  val coverage = ViewCoverage(pointWorld)
  private val taps = java.util.concurrent.CopyOnWriteArrayList<FloatArray>().apply { add(pointWorld.copyOf()) }
  private val seeds = java.util.concurrent.CopyOnWriteArrayList<Seed>().apply { add(Seed(seedFrameIndex, listOf(floatArrayOf(seedPoint[0], seedPoint[1], 1f)))) }

  val tapCount get() = taps.size
  /** Every tap's 3D point (world). */
  val tapPoints: List<FloatArray> get() = taps.toList()
  val seed: Seed get() = seeds.last()
  val seedFrameIndex get() = seed.frameIndex

  /**
   * Another tap on this thing at [pointWorld], made in keyframe [frameIndex] at [imagePoint].
   * [project] gives an earlier tap's pixel in that keyframe, or null if it isn't visible there.
   */
  fun addTap(pointWorld: FloatArray, frameIndex: Int, imagePoint: FloatArray, project: (FloatArray) -> FloatArray?) {
    val points = taps.mapNotNull { project(it)?.let { uv -> floatArrayOf(uv[0], uv[1], 1f) } } + listOf(floatArrayOf(imagePoint[0], imagePoint[1], 1f))
    taps.add(pointWorld.copyOf())
    seeds.add(Seed(frameIndex, points))
    maskFile = null
  }

  /** Undo the latest tap. Returns false when it was the only one (then remove the whole thing). */
  fun undoTap(): Boolean {
    if (taps.size <= 1) return false
    taps.removeAt(taps.size - 1)
    seeds.removeAt(seeds.size - 1)
    maskFile = null
    return true
  }

  fun toSeed() = CaptureFormat.ScanObjectSeed(objectId, seed.frameIndex, seed.points, maskFile, pointAnchor, coverage.views, coverage.coverageDeg)
}

/**
 * Which sides of a tapped object the scan has seen: one entry per keyframe that saw it. Coverage
 * follows the Mac's definition (docs/scan_v1.md): 360° minus the largest gap between the camera's
 * azimuths around the object.
 */
class ViewCoverage(val center: FloatArray, val sectors: Int = 24) {
  private val azimuths = ArrayList<Float>()
  private val sector = BooleanArray(sectors)

  @get:Synchronized val views get() = azimuths.size
  @Synchronized fun isCovered(i: Int) = sector[i]

  @Synchronized fun add(cameraPos: FloatArray) {
    val a = azimuthDeg(cameraPos)
    azimuths.add(a)
    sector[(a / (360f / sectors)).toInt().coerceIn(0, sectors - 1)] = true
  }

  val coverageDeg: Float
    @Synchronized get() {
      if (azimuths.size < 2) return 0f
      val sorted = azimuths.sorted()
      var gap = 360f - sorted.last() + sorted.first()
      for (i in 1 until sorted.size) gap = maxOf(gap, sorted[i] - sorted[i - 1])
      return 360f - gap
    }

  /** Enough for the Mac not to flag it low quality (8 views and 180°, its thresholds). */
  val ready get() = views >= MIN_VIEWS && coverageDeg >= MIN_COVERAGE_DEG

  fun azimuthDeg(cameraPos: FloatArray): Float {
    var a = Math.toDegrees(atan2((cameraPos[0] - center[0]).toDouble(), (cameraPos[2] - center[2]).toDouble())).toFloat()
    if (a < 0f) a += 360f
    return a
  }

  companion object {
    const val MIN_VIEWS = 8
    const val MIN_COVERAGE_DEG = 180f
    const val GOOD_COVERAGE_DEG = 270f
  }
}

/** Whether a keyframe saw a world point: in front, inside the image, at a sensible distance, and not hidden. */
object PointVisibility {
  /**
   * [cameraToWorld] is ARCore's physical camera pose (looks down −Z, +Y up in the sensor image);
   * [k] the CPU image intrinsics. With [depthMm] (cropped to the image, [toImage] = [sx, sy, tx, ty],
   * pixel centres u = tx + sx·(i + 0.5)), a point whose depth pixel measures clearly nearer
   * than the point is hidden behind something. "Clearly" is [occlusionToleranceM] = 35 cm: the
   * tapped point is on one face of the thing, and seen from behind, the thing itself is in front
   * of it; only something well in front (a wall, other furniture) means the thing isn't in view.
   */
  fun visible(
    pWorld: FloatArray,
    cameraToWorld: FloatArray,
    k: CaptureFormat.Intrinsics,
    width: Int,
    height: Int,
    depthMm: ShortArray? = null,
    depthW: Int = 0,
    depthH: Int = 0,
    toImage: FloatArray? = null,
    marginFraction: Float = 0.05f,
    minDistanceM: Float = 0.2f,
    maxDistanceM: Float = 4.0f,
    occlusionToleranceM: Float = 0.35f,
  ): Boolean = project(pWorld, cameraToWorld, k, width, height, depthMm, depthW, depthH, toImage, marginFraction, minDistanceM, maxDistanceM, occlusionToleranceM) != null

  /** Like [visible], but returns the point's pixel [u, v] in the image, or null when it isn't visible. */
  fun project(
    pWorld: FloatArray,
    cameraToWorld: FloatArray,
    k: CaptureFormat.Intrinsics,
    width: Int,
    height: Int,
    depthMm: ShortArray? = null,
    depthW: Int = 0,
    depthH: Int = 0,
    toImage: FloatArray? = null,
    marginFraction: Float = 0.05f,
    minDistanceM: Float = 0.2f,
    maxDistanceM: Float = 4.0f,
    occlusionToleranceM: Float = 0.35f,
  ): FloatArray? {
    val c = cameraToWorld
    // World → camera: Rᵀ (p − t).
    val d = floatArrayOf(pWorld[0] - c[12], pWorld[1] - c[13], pWorld[2] - c[14])
    val x = c[0] * d[0] + c[1] * d[1] + c[2] * d[2]
    val y = c[4] * d[0] + c[5] * d[1] + c[6] * d[2]
    val z = c[8] * d[0] + c[9] * d[1] + c[10] * d[2]
    val depth = -z
    if (depth < minDistanceM) return null
    if (sqrt(x * x + y * y + z * z) > maxDistanceM) return null
    val u = k.cx + k.fx * x / depth
    val v = k.cy - k.fy * y / depth
    val mx = width * marginFraction
    val my = height * marginFraction
    if (u < mx || u > width - mx || v < my || v > height - my) return null
    if (depthMm != null && toImage != null && depthW > 0 && depthH > 0) {
      val i = floor((u - toImage[2]) / toImage[0]).toInt()
      val j = floor((v - toImage[3]) / toImage[1]).toInt()
      if (i in 0 until depthW && j in 0 until depthH) {
        val measured = (depthMm[j * depthW + i].toInt() and 0xFFFF) / 1000f
        if (measured > 0f && measured < depth - occlusionToleranceM) return null
      }
    }
    return floatArrayOf(u, v)
  }
}
