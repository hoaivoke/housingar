package com.housingar.inspect.capture

import com.housingar.inspect.geometry.SurfaceGeometry
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt

/**
 * Rough 3D extent of a selected object from the seed frame's depth: a box in the anchor frame for
 * the Mac, plus the world-space centre/ring used to guide the walk-around.
 *
 * The Mac treats every reliable depth sample inside this box as part of the object (it fills
 * missed mask edges from it), so the box must NOT reach into the supporting surface or the
 * background. Hence:
 * - the bottom stops [SUPPORT_CLEARANCE_M] above the support surface seen around the object;
 * - the unseen depth (one view only shows the front) is added only on the side away from the
 *   camera, and stops short of background surfaces that the seed frame sees behind the object;
 * - padding on the other sides is small.
 */
class SeedExtent(
  val bboxMinAnchor: FloatArray,
  val bboxMaxAnchor: FloatArray,
  val centerWorld: FloatArray,
  /** World height of the bottom of the object (where the guide ring is drawn). */
  val bottomWorldY: Float,
  val ringRadiusM: Float,
  val pointCount: Int,
  /** Support surface height along the anchor's vertical axis, if one was seen. */
  val supportAnchor: Float?,
) {
  companion object {
    const val MIN_POINTS = 30
    const val PAD_M = 0.03f
    const val SUPPORT_CLEARANCE_M = 0.012f
    const val BACKGROUND_CLEARANCE_M = 0.015f
    const val EDGE_GAP_M = 0.03f
    const val MIN_SURFACE_SAMPLES = 5
    const val WALL_SEARCH_ABOVE_M = 0.30f

    /** Returns null when the mask has too little depth to place the object in 3D. */
    fun compute(
      mask: SurfaceGeometry.Mask,
      depth: SurfaceGeometry.DepthImage,
      cameraToWorld: FloatArray,
      worldToAnchor: FloatArray,
      anchorToWorld: FloatArray,
    ): SeedExtent? {
      val (objCam, bgCam) = splitDepth(mask, depth)
      val n = objCam.size / 3
      if (n < MIN_POINTS) return null
      val toAnchor = mul(worldToAnchor, cameraToWorld)
      val obj = SurfaceGeometry.transformPoints(toAnchor, objCam)
      val bg = SurfaceGeometry.transformPoints(toAnchor, bgCam)
      val camAnchor = SurfaceGeometry.transformPoint(toAnchor, floatArrayOf(0f, 0f, 0f))

      // Vertical = anchor axis closest to world up (anchors need not be gravity-aligned).
      val up = SurfaceGeometry.transformPoint(rotationOnly(worldToAnchor), floatArrayOf(0f, 1f, 0f))
      val v = (0..2).maxBy { abs(up[it]) }
      val vSign = if (up[v] >= 0f) 1f else -1f
      val horizontal = (0..2).filter { it != v }

      // Visible extent (1st–99th percentile). Trimming more cuts off the corners of objects seen at
      // an angle (corners are the extremes), and the Mac clips its mesh to this box.
      val lo = FloatArray(3)
      val hi = FloatArray(3)
      for (k in 0..2) {
        val s = FloatArray(n) { obj[it * 3 + k] }.also { it.sort() }
        lo[k] = s[(n * 0.01f).toInt()]
        hi[k] = s[min(n - 1, (n * 0.99f).toInt())]
      }
      val min = lo.copyOf()
      val max = hi.copyOf()

      // Support surface: background samples under the object's footprint, near its lowest point.
      val bottom = if (vSign > 0) lo[v] else hi[v]
      val support = bg.indices.step(3).filter { i ->
        horizontal.all { k -> bg[i + k] in (lo[k] - 0.05f)..(hi[k] + 0.05f) } &&
          (bg[i + v] - bottom) * vSign in -0.06f..0.02f
      }.map { bg[it + v] }.sorted()
      val supportHeight = if (support.size >= 10) support[support.size / 2] else null

      // Unseen depth: square up the footprint, extending only away from the camera.
      val footprint = horizontal.maxOf { hi[it] - lo[it] }
      for (k in horizontal) {
        val missing = footprint - (hi[k] - lo[k])
        if (missing <= 0f) continue
        val mid = (lo[k] + hi[k]) / 2f
        if (camAnchor[k] < mid) max[k] += missing else min[k] -= missing
      }
      for (k in 0..2) {
        min[k] -= PAD_M
        max[k] += PAD_M
      }

      // Vertical limits: clear of the support surface (never below the object's lowest point).
      if (vSign > 0) {
        min[v] = if (supportHeight != null) max(supportHeight + SUPPORT_CLEARANCE_M, lo[v] - 0.005f) else lo[v]
      } else {
        max[v] = if (supportHeight != null) min(supportHeight - SUPPORT_CLEARANCE_M, hi[v] + 0.005f) else hi[v]
      }

      // Background behind the object (e.g. a wall): pull the far side in front of it. The part of a
      // wall directly behind the object is hidden by the object itself, so look in the whole column
      // behind the footprint, up to [WALL_SEARCH_ABOVE_M] above the object. Samples within
      // [EDGE_GAP_M] of the visible object are ignored (mask/depth edges disagree by a pixel or so),
      // and a surface needs [MIN_SURFACE_SAMPLES] samples to count.
      val topV = if (vSign > 0) hi[v] else lo[v]
      for (k in horizontal) {
        val farIsMax = camAnchor[k] < (lo[k] + hi[k]) / 2f
        val other = horizontal.first { it != k }
        val hits = ArrayList<Float>()
        for (i in bg.indices step 3) {
          if (bg[i + other] !in min[other]..max[other]) continue
          val h = (bg[i + v] - bottom) * vSign
          if (h < SUPPORT_CLEARANCE_M || h > (topV - bottom) * vSign + WALL_SEARCH_ABOVE_M) continue
          val c = bg[i + k]
          if (farIsMax && c > hi[k] + EDGE_GAP_M && c < max[k] + BACKGROUND_CLEARANCE_M) hits.add(c)
          if (!farIsMax && c < lo[k] - EDGE_GAP_M && c > min[k] - BACKGROUND_CLEARANCE_M) hits.add(c)
        }
        if (hits.size < MIN_SURFACE_SAMPLES) continue
        hits.sort()
        if (farIsMax) {
          val nearest = hits[MIN_SURFACE_SAMPLES - 1]
          max[k] = min(max[k], max(hi[k] + 0.005f, nearest - BACKGROUND_CLEARANCE_M))
        } else {
          val nearest = hits[hits.size - MIN_SURFACE_SAMPLES]
          min[k] = max(min[k], min(lo[k] - 0.005f, nearest + BACKGROUND_CLEARANCE_M))
        }
      }

      val corners = (0 until 8).map { i ->
        SurfaceGeometry.transformPoint(anchorToWorld, floatArrayOf(
          if (i and 1 == 0) min[0] else max[0], if (i and 2 == 0) min[1] else max[1], if (i and 4 == 0) min[2] else max[2]))
      }
      val center = floatArrayOf(corners.map { it[0] }.average().toFloat(), corners.map { it[1] }.average().toFloat(), corners.map { it[2] }.average().toFloat())
      val bottomWorld = corners.minOf { it[1] }
      val halfDiag = corners.maxOf { c -> sqrt((c[0] - center[0]) * (c[0] - center[0]) + (c[2] - center[2]) * (c[2] - center[2])) }
      return SeedExtent(min, max, center, bottomWorld, halfDiag + 0.12f, n, supportHeight)
    }

    /**
     * Back-projects valid depth pixels into camera space, split into samples wholly inside the mask
     * (object) and wholly outside it (background). A depth pixel covers several image pixels and
     * depth is blurred at silhouettes, so mixed samples on the mask boundary are dropped.
     */
    fun splitDepth(mask: SurfaceGeometry.Mask, depth: SurfaceGeometry.DepthImage): Pair<FloatArray, FloatArray> {
      val k = depth.textureIntrinsics.scaledTo(depth.width, depth.height)
      val r = depth.cpuRegion
      val stepX = (r[2] - r[0]) / depth.width
      val stepY = (r[3] - r[1]) / depth.height
      val inMask = ArrayList<Float>()
      val outMask = ArrayList<Float>()
      for (y in 0 until depth.height) {
        val cpuY = r[1] + (y + 0.5f) / depth.height * (r[3] - r[1])
        for (x in 0 until depth.width) {
          val mm = depth.millimeters[y * depth.width + x].toInt() and 0xFFFF
          if (mm == 0) continue
          val z = mm / 1000f
          if (z < SurfaceGeometry.MIN_DEPTH_M || z > SurfaceGeometry.MAX_DEPTH_M) continue
          val cpuX = r[0] + (x + 0.5f) / depth.width * (r[2] - r[0])
          // Centre plus the neighbouring depth-pixel centres: one depth pixel of erosion either way.
          var inside = 0
          for ((ox, oy) in NEIGHBOURS) if (mask[(cpuX + ox * stepX).toInt(), (cpuY + oy * stepY).toInt()]) inside++
          val target = when (inside) {
            NEIGHBOURS.size -> inMask
            0 -> outMask
            else -> continue
          }
          target.add(z * (x + 0.5f - k.cx) / k.fx)
          target.add(z * (k.cy - (y + 0.5f)) / k.fy)
          target.add(-z)
        }
      }
      return inMask.toFloatArray() to outMask.toFloatArray()
    }

    private val NEIGHBOURS = listOf(0 to 0, -1 to 0, 1 to 0, 0 to -1, 0 to 1)

    private fun mul(a: FloatArray, b: FloatArray) = FloatArray(16).also { o ->
      for (r in 0..3) for (c in 0..3) {
        var s = 0f
        for (j in 0..3) s += a[j * 4 + r] * b[c * 4 + j]
        o[c * 4 + r] = s
      }
    }

    private fun rotationOnly(m: FloatArray) = m.copyOf().also { it[12] = 0f; it[13] = 0f; it[14] = 0f }
  }
}
