package com.housingar.inspect.geometry

import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sqrt
import kotlin.random.Random

/**
 * Turns a 2D segmentation mask into bounded, anchor-relative surface geometry (plan step
 * "How the highlight stays in the room", items 3–5). Pure Kotlin so it can be unit tested on the
 * JVM; all inputs come from the *captured* frame, never the latest camera pose.
 *
 * Conventions (match ARCore):
 * - Matrices are 4x4 column-major (android.opengl.Matrix / Pose.toMatrix).
 * - Camera space: +X right, +Y up, camera looks down −Z (Camera.getPose()).
 * - Image pixels: origin top-left, +y down, in sensor orientation (CPU image / depth image).
 * - Depth is in millimetres; 0 means "no estimate".
 */
object SurfaceGeometry {

  class Intrinsics(val fx: Float, val fy: Float, val cx: Float, val cy: Float, val width: Int, val height: Int) {
    /** Scales intrinsics to another resolution covering the same field of view. */
    fun scaledTo(w: Int, h: Int) = Intrinsics(fx * w / width, fy * h / height, cx * w / width, cy * h / height, w, h)
  }

  /**
   * A depth image plus how it lines up with the CPU image.
   * @param textureIntrinsics camera texture intrinsics (the depth image covers the texture's FOV)
   * @param cpuRegion CPU-image pixel rectangle covered by the depth image: x0, y0, x1, y1, from
   *   Frame.transformCoordinates2d(TEXTURE_NORMALIZED (0,0) and (1,1) → IMAGE_PIXELS)
   */
  class DepthImage(
    val width: Int,
    val height: Int,
    val millimeters: ShortArray,
    val textureIntrinsics: Intrinsics,
    val cpuRegion: FloatArray,
  ) {
    init {
      require(millimeters.size == width * height) { "depth buffer size mismatch" }
      require(cpuRegion.size == 4)
    }
  }

  class Mask(val width: Int, val height: Int, val bits: BooleanArray) {
    init {
      require(bits.size == width * height) { "mask size mismatch" }
    }

    operator fun get(x: Int, y: Int): Boolean = x in 0 until width && y in 0 until height && bits[y * width + x]
  }

  class PlaneEstimate(val point: FloatArray, val normal: FloatArray)

  class Input(
    val mask: Mask,
    /** Outline of the selected region in CPU-image pixels (e.g. the service's `polygon`). */
    val outline: List<FloatArray>,
    val cpuIntrinsics: Intrinsics,
    val depth: DepthImage?,
    /** Physical camera pose at capture, camera → world. */
    val cameraToWorld: FloatArray,
    /** Inverse of the anchor's pose at capture, world → anchor. */
    val worldToAnchor: FloatArray,
    /** Optional ARCore plane under the tap, used when depth cannot support a fit. World space. */
    val fallbackPlane: PlaneEstimate? = null,
  )

  enum class Failure(val userMessage: String) {
    NO_OUTLINE("The model returned an empty selection. Tap again or add points."),
    NOT_ENOUGH_DEPTH("Not enough depth on this surface. Move sideways slowly, keep it well lit, and select again."),
    NOT_FLAT("The selected region is not a single flat surface. Remove points that spill onto other surfaces."),
    OUTLINE_PROJECTION_FAILED("The outline could not be placed on the surface (viewing angle too shallow). Face the surface more directly."),
    TOO_SMALL("The selected surface is too small to anchor reliably (under 5 × 5 cm)."),
    TOO_LARGE("The selected surface is implausibly large. Select a single wall section or tabletop."),
  }

  class Stats(
    val depthPixelsInMask: Int,
    val validDepthPixels: Int,
    val inliers: Int,
    val rmsM: Float,
    val medianDepthM: Float,
    val droppedOutlineVertices: Int,
    val outlineVertices: Int,
    val source: String,
    val triangulation: String,
  )

  class Surface(
    /** Polygon vertices in anchor-local metres, xyz interleaved, counter-clockwise seen from the front. */
    val vertices: FloatArray,
    /** Triangle indices into [vertices]. */
    val triangles: IntArray,
    val normalAnchor: FloatArray,
    val centroidAnchor: FloatArray,
    val verticesWorldAtCapture: FloatArray,
    val planeWorld: PlaneEstimate,
    val areaM2: Float,
    val stats: Stats,
  )

  sealed class Outcome {
    class Success(val surface: Surface) : Outcome()
    class Failed(val reason: Failure, val stats: Stats?) : Outcome()
  }

  // Tunables (documented so measurements can be compared run to run).
  const val MIN_DEPTH_M = 0.15f
  const val MAX_DEPTH_M = 8.0f
  const val MIN_POINTS = 60
  const val MIN_INLIER_RATIO = 0.6f
  const val RANSAC_ITERATIONS = 200
  const val MAX_RANSAC_POINTS = 4000
  const val MIN_AREA_M2 = 0.0025f
  const val MAX_AREA_M2 = 60f

  fun build(input: Input, random: Random = Random(7)): Outcome {
    if (input.outline.size < 3 || input.mask.bits.none { it }) return Outcome.Failed(Failure.NO_OUTLINE, null)

    val camOrigin = transformPoint(input.cameraToWorld, floatArrayOf(0f, 0f, 0f))

    // 1. Masked depth samples → world points.
    val sampled = input.depth?.let { maskedDepthPoints(input.mask, it) }
    val cameraPts = sampled?.points ?: FloatArray(0)
    val worldPts = transformPoints(input.cameraToWorld, cameraPts)
    val n = worldPts.size / 3
    val medianDepth = sampled?.medianDepthM ?: 0f

    // 2. Plane from depth (RANSAC + PCA refine), else ARCore plane fallback.
    var plane: PlaneEstimate? = null
    var inliers = 0
    var rms = 0f
    var source = "depth_plane_fit"
    var fitFailure: Failure? = null
    if (n >= MIN_POINTS) {
      val threshold = max(0.015f, 0.02f * medianDepth)
      val fit = fitPlaneRansac(worldPts, threshold, random)
      if (fit != null && fit.inlierCount >= MIN_INLIER_RATIO * n) {
        val refined = refinePlane(worldPts, fit.inlierFlags)
        plane = refined.first
        rms = refined.second
        inliers = fit.inlierCount
      } else {
        fitFailure = Failure.NOT_FLAT
        inliers = fit?.inlierCount ?: 0
      }
    } else {
      fitFailure = Failure.NOT_ENOUGH_DEPTH
    }
    if (plane == null && input.fallbackPlane != null && fitFailure == Failure.NOT_ENOUGH_DEPTH) {
      plane = PlaneEstimate(input.fallbackPlane.point.copyOf(), normalize(input.fallbackPlane.normal))
      source = "arcore_plane"
    }

    fun stats(dropped: Int, tri: String) = Stats(
      depthPixelsInMask = sampled?.maskedPixels ?: 0,
      validDepthPixels = n,
      inliers = inliers,
      rmsM = rms,
      medianDepthM = medianDepth,
      droppedOutlineVertices = dropped,
      outlineVertices = input.outline.size,
      source = source,
      triangulation = tri,
    )
    if (plane == null) return Outcome.Failed(fitFailure ?: Failure.NOT_ENOUGH_DEPTH, stats(0, "none"))

    // Face the normal toward the camera that observed the surface.
    val toCam = sub(camOrigin, plane.point)
    val normal = if (dot(plane.normal, toCam) < 0f) scale(plane.normal, -1f) else plane.normal
    val planeW = PlaneEstimate(plane.point, normal)

    // 3. Project the outline onto the plane along camera rays.
    val maxT = if (medianDepth > 0f) min(MAX_DEPTH_M * 1.25f, max(3f * medianDepth, 1.5f)) else MAX_DEPTH_M
    val rotation = rotationOnly(input.cameraToWorld)
    val projected = ArrayList<FloatArray>(input.outline.size)
    var dropped = 0
    for (p in input.outline) {
      val dirCam = floatArrayOf((p[0] - input.cpuIntrinsics.cx) / input.cpuIntrinsics.fx, (input.cpuIntrinsics.cy - p[1]) / input.cpuIntrinsics.fy, -1f)
      val dir = normalize(transformDirection(rotation, dirCam))
      val hit = intersectRay(camOrigin, dir, planeW, minCos = 0.08f, maxT = maxT)
      if (hit == null) dropped++ else projected.add(hit)
    }
    if (projected.size < 3 || dropped > 0.3f * input.outline.size) {
      return Outcome.Failed(Failure.OUTLINE_PROJECTION_FAILED, stats(dropped, "none"))
    }

    // 4. Triangulate in the plane's 2D basis.
    val basis = planeBasis(normal)
    val pts2 = projected.map { floatArrayOf(dot(sub(it, planeW.point), basis.first), dot(sub(it, planeW.point), basis.second)) }
    var poly2 = pts2
    var order = pts2.indices.toList()
    if (signedArea(poly2) < 0f) {
      order = order.reversed()
      poly2 = order.map { pts2[it] }
    }
    var polygon3 = order.map { projected[it] }
    var triangles = earClip(poly2)
    var triangulation = "ear_clipping"
    if (triangles == null) {
      val hull = convexHull(poly2)
      polygon3 = hull.map { polygon3[it] }
      poly2 = hull.map { poly2[it] }
      triangles = IntArray((poly2.size - 2) * 3).also { t -> for (i in 1 until poly2.size - 1) { t[(i - 1) * 3] = 0; t[(i - 1) * 3 + 1] = i; t[(i - 1) * 3 + 2] = i + 1 } }
      triangulation = "convex_hull_fallback"
    }
    val area = abs(signedArea(poly2))
    if (area < MIN_AREA_M2) return Outcome.Failed(Failure.TOO_SMALL, stats(dropped, triangulation))
    if (area > MAX_AREA_M2) return Outcome.Failed(Failure.TOO_LARGE, stats(dropped, triangulation))

    // 5. Express everything relative to the anchor as it was at capture.
    val worldFlat = FloatArray(polygon3.size * 3).also { f -> polygon3.forEachIndexed { i, v -> v.copyInto(f, i * 3) } }
    val local = transformPoints(input.worldToAnchor, worldFlat)
    val normalLocal = normalize(transformDirection(rotationOnly(input.worldToAnchor), normal))
    val centroid = FloatArray(3)
    for (i in 0 until local.size / 3) for (k in 0..2) centroid[k] += local[i * 3 + k]
    for (k in 0..2) centroid[k] /= (local.size / 3).toFloat()

    return Outcome.Success(Surface(local, triangles, normalLocal, centroid, worldFlat, planeW, area, stats(dropped, triangulation)))
  }

  // ---------------------------------------------------------------- depth sampling

  class DepthSamples(val points: FloatArray, val maskedPixels: Int, val medianDepthM: Float)

  /** Back-projects depth pixels whose corresponding CPU-image pixel is inside the mask. */
  fun maskedDepthPoints(mask: Mask, depth: DepthImage): DepthSamples {
    val k = depth.textureIntrinsics.scaledTo(depth.width, depth.height)
    val (x0, y0, x1, y1) = depth.cpuRegion.let { listOf(it[0], it[1], it[2], it[3]) }
    val out = ArrayList<Float>()
    val depths = ArrayList<Float>()
    var masked = 0
    for (y in 0 until depth.height) {
      val cpuY = (y0 + (y + 0.5f) / depth.height * (y1 - y0))
      for (x in 0 until depth.width) {
        val cpuX = (x0 + (x + 0.5f) / depth.width * (x1 - x0))
        if (!mask[cpuX.toInt(), cpuY.toInt()]) continue
        masked++
        val mm = depth.millimeters[y * depth.width + x].toInt() and 0xFFFF
        if (mm == 0) continue
        val z = mm / 1000f
        if (z < MIN_DEPTH_M || z > MAX_DEPTH_M) continue
        // Pixel-centre convention (u = x + 0.5), as in docs/capture_v1.md.
        out.add(z * (x + 0.5f - k.cx) / k.fx)
        out.add(z * (k.cy - (y + 0.5f)) / k.fy)
        out.add(-z)
        depths.add(z)
      }
    }
    depths.sort()
    val median = if (depths.isEmpty()) 0f else depths[depths.size / 2]
    return DepthSamples(out.toFloatArray(), masked, median)
  }

  // ---------------------------------------------------------------- plane fitting

  class RansacFit(val plane: PlaneEstimate, val inlierFlags: BooleanArray, val inlierCount: Int)

  fun fitPlaneRansac(points: FloatArray, threshold: Float, random: Random, iterations: Int = RANSAC_ITERATIONS): RansacFit? {
    val n = points.size / 3
    if (n < 3) return null
    // Hypothesise on a subsample; score on all points.
    val candidates = if (n <= MAX_RANSAC_POINTS) IntArray(n) { it } else IntArray(MAX_RANSAC_POINTS) { random.nextInt(n) }
    var best: PlaneEstimate? = null
    var bestCount = -1
    repeat(iterations) {
      val a = candidates[random.nextInt(candidates.size)]
      val b = candidates[random.nextInt(candidates.size)]
      val c = candidates[random.nextInt(candidates.size)]
      if (a == b || b == c || a == c) return@repeat
      val pa = pt(points, a)
      val nrm = cross(sub(pt(points, b), pa), sub(pt(points, c), pa))
      val len = length(nrm)
      if (len < 1e-6f) return@repeat
      val unit = scale(nrm, 1f / len)
      var count = 0
      for (i in candidates) if (abs(dot(sub(pt(points, i), pa), unit)) <= threshold) count++
      if (count > bestCount) {
        bestCount = count
        best = PlaneEstimate(pa, unit)
      }
    }
    val plane = best ?: return null
    val flags = BooleanArray(n)
    var count = 0
    for (i in 0 until n) {
      if (abs(dot(sub(pt(points, i), plane.point), plane.normal)) <= threshold) {
        flags[i] = true
        count++
      }
    }
    return RansacFit(plane, flags, count)
  }

  /** Least-squares plane through the inliers (centroid + smallest-eigenvalue direction). Returns plane and RMS distance. */
  fun refinePlane(points: FloatArray, inliers: BooleanArray): Pair<PlaneEstimate, Float> {
    val c = DoubleArray(3)
    var m = 0
    for (i in inliers.indices) if (inliers[i]) {
      for (k in 0..2) c[k] += points[i * 3 + k].toDouble()
      m++
    }
    for (k in 0..2) c[k] /= m
    val cov = Array(3) { DoubleArray(3) }
    for (i in inliers.indices) if (inliers[i]) {
      val d = DoubleArray(3) { points[i * 3 + it] - c[it] }
      for (r in 0..2) for (s in 0..2) cov[r][s] += d[r] * d[s]
    }
    val (values, vectors) = jacobiEigen(cov)
    var minIdx = 0
    for (k in 1..2) if (values[k] < values[minIdx]) minIdx = k
    val normal = normalize(floatArrayOf(vectors[0][minIdx].toFloat(), vectors[1][minIdx].toFloat(), vectors[2][minIdx].toFloat()))
    val centroid = floatArrayOf(c[0].toFloat(), c[1].toFloat(), c[2].toFloat())
    var sq = 0.0
    for (i in inliers.indices) if (inliers[i]) {
      val dist = dot(sub(pt(points, i), centroid), normal)
      sq += dist * dist
    }
    return PlaneEstimate(centroid, normal) to sqrt(sq / m).toFloat()
  }

  /** Eigen decomposition of a symmetric 3x3 matrix. Returns eigenvalues and column eigenvectors. */
  fun jacobiEigen(input: Array<DoubleArray>): Pair<DoubleArray, Array<DoubleArray>> {
    val a = Array(3) { input[it].copyOf() }
    val v = Array(3) { r -> DoubleArray(3) { if (it == r) 1.0 else 0.0 } }
    repeat(50) {
      var p = 0
      var q = 1
      var largest = abs(a[0][1])
      if (abs(a[0][2]) > largest) { p = 0; q = 2; largest = abs(a[0][2]) }
      if (abs(a[1][2]) > largest) { p = 1; q = 2; largest = abs(a[1][2]) }
      if (largest < 1e-15) return@repeat
      val theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
      val t = (if (theta >= 0) 1.0 else -1.0) / (abs(theta) + sqrt(theta * theta + 1))
      val cs = 1 / sqrt(t * t + 1)
      val sn = t * cs
      for (k in 0..2) {
        val akp = a[k][p]
        val akq = a[k][q]
        a[k][p] = cs * akp - sn * akq
        a[k][q] = sn * akp + cs * akq
      }
      for (k in 0..2) {
        val apk = a[p][k]
        val aqk = a[q][k]
        a[p][k] = cs * apk - sn * aqk
        a[q][k] = sn * apk + cs * aqk
      }
      for (k in 0..2) {
        val vkp = v[k][p]
        val vkq = v[k][q]
        v[k][p] = cs * vkp - sn * vkq
        v[k][q] = sn * vkp + cs * vkq
      }
    }
    return doubleArrayOf(a[0][0], a[1][1], a[2][2]) to v
  }

  // ---------------------------------------------------------------- rays and polygons

  /** Ray/plane intersection; null when nearly parallel (|cos| < minCos), behind the camera, or beyond maxT. */
  fun intersectRay(origin: FloatArray, dir: FloatArray, plane: PlaneEstimate, minCos: Float, maxT: Float): FloatArray? {
    val denom = dot(dir, plane.normal)
    if (abs(denom) < minCos) return null
    val t = dot(sub(plane.point, origin), plane.normal) / denom
    if (t <= 0f || t > maxT) return null
    return floatArrayOf(origin[0] + dir[0] * t, origin[1] + dir[1] * t, origin[2] + dir[2] * t)
  }

  fun planeBasis(normal: FloatArray): Pair<FloatArray, FloatArray> {
    val n = normalize(normal)
    val helper = if (abs(n[1]) < 0.9f) floatArrayOf(0f, 1f, 0f) else floatArrayOf(1f, 0f, 0f)
    val u = normalize(cross(helper, n))
    val v = cross(n, u)
    return u to v
  }

  fun signedArea(poly: List<FloatArray>): Float {
    var s = 0f
    for (i in poly.indices) {
      val a = poly[i]
      val b = poly[(i + 1) % poly.size]
      s += a[0] * b[1] - b[0] * a[1]
    }
    return s / 2f
  }

  /** Ear clipping for a simple counter-clockwise polygon. Returns null if the polygon is not simple. */
  fun earClip(poly: List<FloatArray>): IntArray? {
    val n = poly.size
    if (n < 3) return null
    val idx = MutableList(n) { it }
    val tris = ArrayList<Int>((n - 2) * 3)
    var guard = 0
    while (idx.size > 3) {
      if (guard++ > n * n) return null
      var clipped = false
      for (i in idx.indices) {
        val ia = idx[(i + idx.size - 1) % idx.size]
        val ib = idx[i]
        val ic = idx[(i + 1) % idx.size]
        val a = poly[ia]
        val b = poly[ib]
        val c = poly[ic]
        if (cross2(a, b, c) <= 1e-12f) continue // reflex or degenerate
        var contains = false
        for (j in idx) {
          if (j == ia || j == ib || j == ic) continue
          if (pointInTriangle(poly[j], a, b, c)) { contains = true; break }
        }
        if (contains) continue
        tris.add(ia); tris.add(ib); tris.add(ic)
        idx.removeAt(i)
        clipped = true
        break
      }
      if (!clipped) return null
    }
    tris.add(idx[0]); tris.add(idx[1]); tris.add(idx[2])
    if (hasSelfIntersection(poly)) return null
    return tris.toIntArray()
  }

  /** Andrew's monotone chain; returns indices of the hull in counter-clockwise order. */
  fun convexHull(poly: List<FloatArray>): List<Int> {
    val order = poly.indices.sortedWith(compareBy<Int>({ poly[it][0] }, { poly[it][1] }))
    val lower = ArrayList<Int>()
    for (i in order) {
      while (lower.size >= 2 && cross2(poly[lower[lower.size - 2]], poly[lower.last()], poly[i]) <= 0f) lower.removeAt(lower.size - 1)
      lower.add(i)
    }
    val upper = ArrayList<Int>()
    for (i in order.reversed()) {
      while (upper.size >= 2 && cross2(poly[upper[upper.size - 2]], poly[upper.last()], poly[i]) <= 0f) upper.removeAt(upper.size - 1)
      upper.add(i)
    }
    lower.removeAt(lower.size - 1)
    upper.removeAt(upper.size - 1)
    return lower + upper
  }

  private fun hasSelfIntersection(poly: List<FloatArray>): Boolean {
    val n = poly.size
    for (i in 0 until n) {
      val a1 = poly[i]
      val a2 = poly[(i + 1) % n]
      for (j in i + 2 until n) {
        if (i == 0 && j == n - 1) continue
        if (segmentsCross(a1, a2, poly[j], poly[(j + 1) % n])) return true
      }
    }
    return false
  }

  private fun segmentsCross(p1: FloatArray, p2: FloatArray, q1: FloatArray, q2: FloatArray): Boolean {
    val d1 = cross2(q1, q2, p1)
    val d2 = cross2(q1, q2, p2)
    val d3 = cross2(p1, p2, q1)
    val d4 = cross2(p1, p2, q2)
    return ((d1 > 0f && d2 < 0f) || (d1 < 0f && d2 > 0f)) && ((d3 > 0f && d4 < 0f) || (d3 < 0f && d4 > 0f))
  }

  private fun pointInTriangle(p: FloatArray, a: FloatArray, b: FloatArray, c: FloatArray): Boolean {
    val d1 = cross2(a, b, p)
    val d2 = cross2(b, c, p)
    val d3 = cross2(c, a, p)
    return d1 >= 0f && d2 >= 0f && d3 >= 0f
  }

  private fun cross2(a: FloatArray, b: FloatArray, c: FloatArray) = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

  // ---------------------------------------------------------------- small linear algebra

  fun transformPoint(m: FloatArray, p: FloatArray) = floatArrayOf(
    m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
    m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
    m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14],
  )

  fun transformPoints(m: FloatArray, pts: FloatArray): FloatArray {
    val out = FloatArray(pts.size)
    for (i in 0 until pts.size / 3) {
      val x = pts[i * 3]
      val y = pts[i * 3 + 1]
      val z = pts[i * 3 + 2]
      out[i * 3] = m[0] * x + m[4] * y + m[8] * z + m[12]
      out[i * 3 + 1] = m[1] * x + m[5] * y + m[9] * z + m[13]
      out[i * 3 + 2] = m[2] * x + m[6] * y + m[10] * z + m[14]
    }
    return out
  }

  private fun rotationOnly(m: FloatArray) = m.copyOf().also { it[12] = 0f; it[13] = 0f; it[14] = 0f }

  private fun transformDirection(m: FloatArray, d: FloatArray) = transformPoint(m, d)

  private fun pt(a: FloatArray, i: Int) = floatArrayOf(a[i * 3], a[i * 3 + 1], a[i * 3 + 2])
  fun sub(a: FloatArray, b: FloatArray) = floatArrayOf(a[0] - b[0], a[1] - b[1], a[2] - b[2])
  fun dot(a: FloatArray, b: FloatArray) = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
  fun cross(a: FloatArray, b: FloatArray) = floatArrayOf(a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
  fun length(a: FloatArray) = sqrt(dot(a, a))
  fun scale(a: FloatArray, s: Float) = floatArrayOf(a[0] * s, a[1] * s, a[2] * s)
  fun normalize(a: FloatArray): FloatArray {
    val l = length(a)
    return if (l < 1e-12f) floatArrayOf(0f, 1f, 0f) else scale(a, 1f / l)
  }
}
