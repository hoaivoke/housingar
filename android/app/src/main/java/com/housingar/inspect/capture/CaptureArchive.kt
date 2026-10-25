package com.housingar.inspect.capture

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * The "housingar.capture.v1" upload format (docs/capture_v1.md, owned by the Mac service).
 * Pure Kotlin + org.json so the manifest and archive can be tested on the JVM.
 */
object CaptureFormat {
  const val SCHEMA = "housingar.capture.v1"
  const val CONVENTIONS = "camera: +X right, +Y up, looks down -Z (ARCore/OpenGL). image: origin top-left, +y down. " +
    "matrices: 4x4 column-major. 'anchor' = ARCore anchor created at selection; world +Y is up (gravity)."
  private val ID = Regex("[A-Za-z0-9_-]{1,64}")

  class Intrinsics(val fx: Float, val fy: Float, val cx: Float, val cy: Float) {
    fun toJson(): JSONObject = JSONObject().put("fx", fx.toDouble()).put("fy", fy.toDouble()).put("cx", cx.toDouble()).put("cy", cy.toDouble())
  }

  class Depth(
    val file: String,
    val width: Int,
    val height: Int,
    val timestampNs: Long,
    val intrinsics: Intrinsics,
    /** sx, sy, tx, ty: depth pixel centre (u + 0.5, v + 0.5) → image px (sx·u′ + tx, sy·v′ + ty). */
    val toImage: FloatArray,
  )

  class Frame(
    val index: Int,
    val timestampNs: Long,
    val image: String,
    val width: Int,
    val height: Int,
    val intrinsics: Intrinsics,
    val cameraToAnchor: FloatArray,
    val cameraToWorld: FloatArray,
    val depth: Depth,
    val confidenceFile: String?,
    val sharpness: Float,
    val orbitDeg: Float,
  )

  class Manifest(
    val captureId: String,
    val sessionId: String,
    val label: String?,
    val anchorToWorld: FloatArray,
    val seedFrameIndex: Int,
    /** x, y, label (1 include / 0 exclude) in the seed frame's image pixels. */
    val seedPoints: List<FloatArray>,
    val seedMask: String,
    val bboxMin: FloatArray,
    val bboxMax: FloatArray,
    val frames: List<Frame>,
  ) {
    init {
      require(ID.matches(captureId)) { "capture_id must match [A-Za-z0-9_-]{1,64}" }
      require(ID.matches(sessionId)) { "session_id must match [A-Za-z0-9_-]{1,64}" }
      require(anchorToWorld.size == 16)
    }
  }

  fun depthToImage(regionX0: Float, regionY0: Float, regionX1: Float, regionY1: Float, depthW: Int, depthH: Int) =
    floatArrayOf((regionX1 - regionX0) / depthW, (regionY1 - regionY0) / depthH, regionX0, regionY0)

  /** The part of a depth image that lies inside the RGB image, with its capture_v1 mapping. */
  class DepthCrop(val i0: Int, val j0: Int, val width: Int, val height: Int, val toImage: FloatArray, val intrinsics: Intrinsics) {
    fun crop(src: ShortArray, srcWidth: Int) = ShortArray(width * height) { k -> src[(j0 + k / width) * srcWidth + i0 + k % width] }
    fun crop(src: ByteArray, srcWidth: Int) = ByteArray(width * height) { k -> src[(j0 + k / width) * srcWidth + i0 + k % width] }
  }

  /**
   * Crops a depth image to the columns/rows whose pixels fall inside the RGB image (the Mac
   * rejects mappings that leave the image) and derives depth intrinsics from the RGB intrinsics
   * through the mapping, so `sx·fx_d = fx`, `sx·cx_d + tx = cx` hold exactly (pixel-centre
   * convention: depth centre (i + 0.5) maps to image sx·(i + 0.5) + tx).
   * [region] = x0, y0, x1, y1: RGB pixels covered by the full depth image. Null if under 8×8 remains.
   */
  fun cropDepthToImage(depthW: Int, depthH: Int, region: FloatArray, imageW: Int, imageH: Int, image: Intrinsics): DepthCrop? {
    val sx = (region[2] - region[0]) / depthW
    val sy = (region[3] - region[1]) / depthH
    if (sx <= 0f || sy <= 0f) return null
    val eps = 1e-3f
    val i0 = maxOf(0, kotlin.math.ceil((-region[0]) / sx - eps).toInt())
    val i1 = minOf(depthW, kotlin.math.floor((imageW - region[0]) / sx + eps).toInt())
    val j0 = maxOf(0, kotlin.math.ceil((-region[1]) / sy - eps).toInt())
    val j1 = minOf(depthH, kotlin.math.floor((imageH - region[1]) / sy + eps).toInt())
    val w = i1 - i0
    val h = j1 - j0
    if (w < 8 || h < 8) return null
    val tx = maxOf(0f, region[0] + sx * i0)
    val ty = maxOf(0f, region[1] + sy * j0)
    val intr = Intrinsics(image.fx / sx, image.fy / sy, (image.cx - tx) / sx, (image.cy - ty) / sy)
    return DepthCrop(i0, j0, w, h, floatArrayOf(sx, sy, tx, ty), intr)
  }

  fun manifestJson(m: Manifest): JSONObject {
    val frames = framesJson(m.frames)
    val json = JSONObject()
      .put("schema", SCHEMA)
      .put("capture_id", m.captureId)
      .put("session_id", m.sessionId)
      .put("mode", "object")
      .put("units", "meters")
      .put("conventions", CONVENTIONS)
      .put("anchor_to_world", floats(m.anchorToWorld))
      .put("seed", JSONObject()
        .put("frame_index", m.seedFrameIndex)
        .put("points", JSONArray(m.seedPoints.map { p -> JSONArray().put(p[0].toDouble()).put(p[1].toDouble()).put(if (p[2] >= 0.5f) 1 else 0) }))
        .put("mask", m.seedMask)
        .put("bbox_anchor", JSONObject().put("min", floats(m.bboxMin)).put("max", floats(m.bboxMax))))
      .put("frames", frames)
    if (!m.label.isNullOrBlank()) json.put("label", m.label)
    return json
  }

  /** Frame records shared by capture_v1 and scan_v1. */
  fun framesJson(list: List<Frame>): JSONArray {
    val frames = JSONArray()
    for (f in list) {
      require(f.cameraToAnchor.size == 16 && f.cameraToWorld.size == 16)
      require((f.cameraToAnchor + f.cameraToWorld).all { it.isFinite() }) { "non-finite matrix in frame ${f.index}" }
      frames.put(JSONObject()
        .put("index", f.index)
        .put("timestamp_ns", f.timestampNs)
        .put("tracking", "TRACKING")
        .put("image", f.image)
        .put("width", f.width)
        .put("height", f.height)
        .put("intrinsics", f.intrinsics.toJson())
        .put("camera_to_anchor", floats(f.cameraToAnchor))
        .put("camera_to_world", floats(f.cameraToWorld))
        .put("depth", JSONObject()
          .put("file", f.depth.file)
          .put("width", f.depth.width)
          .put("height", f.depth.height)
          .put("timestamp_ns", f.depth.timestampNs)
          .put("intrinsics", f.depth.intrinsics.toJson())
          .put("to_image", floats(f.depth.toImage)))
        .put("confidence", f.confidenceFile?.let { JSONObject().put("file", it) } ?: JSONObject.NULL)
        .put("sharpness", f.sharpness.toDouble())
        .put("orbit_deg", f.orbitDeg.toDouble()))
    }
    return frames
  }

  const val SCAN_SCHEMA = "housingar.scan.v1"
  /** scan_v1 plus the objects the user tapped (docs/scan_v2.md); the Mac builds exactly those. */
  const val SCAN_V2_SCHEMA = "housingar.scan.v2"

  /** One tapped object: where to start following it, and what the phone knows about it. */
  class ScanObjectSeed(
    val objectId: String,
    /** Index (frames[].index) of the keyframe taken at the tap. */
    val seedFrameIndex: Int,
    /** Image pixels in the seed frame + label (1 = object, 0 = not). */
    val points: List<FloatArray>,
    /** "seeds/<object_id>.png": SAM's outline at tap time, when the Mac was reachable. */
    val mask: String?,
    /** The tapped 3D point in the scan anchor's frame. */
    val pointAnchor: FloatArray?,
    /** The phone's own estimate of how well the scan saw it (informational). */
    val views: Int,
    val coverageDeg: Float,
  ) {
    init {
      require(ID.matches(objectId)) { "object_id must match [A-Za-z0-9_-]{1,64}" }
      require(points.isNotEmpty())
    }
  }

  /** "Scan stuff" walk-through (docs/scan_v1.md, v2 with [objects]): capture_v1 frames, level scan anchor. */
  class ScanManifest(
    val scanId: String,
    val sessionId: String,
    val label: String?,
    val anchorToWorld: FloatArray,
    val frames: List<Frame>,
    val objects: List<ScanObjectSeed> = emptyList(),
  ) {
    init {
      require(ID.matches(scanId)) { "scan_id must match [A-Za-z0-9_-]{1,64}" }
      require(ID.matches(sessionId)) { "session_id must match [A-Za-z0-9_-]{1,64}" }
      require(anchorToWorld.size == 16)
      val indices = frames.map { it.index }.toSet()
      objects.forEach { require(it.seedFrameIndex in indices) { "seed frame ${it.seedFrameIndex} of ${it.objectId} is not in frames" } }
      require(objects.map { it.objectId }.toSet().size == objects.size) { "duplicate object_id" }
    }
  }

  fun scanManifestJson(m: ScanManifest): JSONObject {
    val json = JSONObject()
      .put("schema", if (m.objects.isEmpty()) SCAN_SCHEMA else SCAN_V2_SCHEMA)
      .put("scan_id", m.scanId)
      .put("session_id", m.sessionId)
      .put("units", "meters")
      .put("conventions", CONVENTIONS)
      .put("anchor_to_world", floats(m.anchorToWorld))
      .put("frames", framesJson(m.frames))
    if (!m.label.isNullOrBlank()) json.put("label", m.label)
    if (m.objects.isNotEmpty()) json.put("objects", JSONArray(m.objects.map { o ->
      JSONObject()
        .put("object_id", o.objectId)
        .put("seed_frame_index", o.seedFrameIndex)
        .put("points", JSONArray(o.points.map { p -> JSONArray().put(p[0].toDouble()).put(p[1].toDouble()).put(if (p[2] >= 0.5f) 1 else 0) }))
        .put("mask", o.mask ?: JSONObject.NULL)
        .put("point_anchor", o.pointAnchor?.let { floats(it) } ?: JSONObject.NULL)
        .put("phone_coverage", JSONObject().put("views", o.views).put("coverage_deg", o.coverageDeg.toDouble()))
    }))
    return json
  }

  /** uint16 little-endian millimetres, row-major, tightly packed. */
  fun depthBytes(mm: ShortArray): ByteArray {
    val buf = ByteBuffer.allocate(mm.size * 2).order(ByteOrder.LITTLE_ENDIAN)
    buf.asShortBuffer().put(mm)
    return buf.array()
  }

  /**
   * Zips exactly the capture_v1 archive contents: frames/, depth/, confidence/ and seed_mask.png.
   * The manifest travels as the separate multipart field (a copy stays beside the capture on the
   * phone as manifest.json, but is not put in the archive).
   */
  fun writeZip(captureDir: File, out: OutputStream) {
    ZipOutputStream(out).use { zip ->
      val files = captureDir.walkTopDown().filter { it.isFile }.sortedBy { it.relativeTo(captureDir).path }
      for (f in files) {
        val name = f.relativeTo(captureDir).invariantSeparatorsPath
        val allowed = name == "seed_mask.png" || name.startsWith("frames/") || name.startsWith("depth/") || name.startsWith("confidence/") ||
          (name.startsWith("seeds/") && name.endsWith(".png"))
        if (!allowed || name.contains("..")) continue
        val entry = ZipEntry(name)
        // JPEGs are already compressed; store them to save phone CPU.
        if (name.endsWith(".jpg")) {
          entry.method = ZipEntry.STORED
          entry.size = f.length()
          entry.compressedSize = f.length()
          entry.crc = crc32(f)
        }
        zip.putNextEntry(entry)
        FileInputStream(f).use { it.copyTo(zip) }
        zip.closeEntry()
      }
    }
  }

  private fun crc32(f: File): Long {
    val crc = java.util.zip.CRC32()
    FileInputStream(f).use { input ->
      val buf = ByteArray(64 * 1024)
      while (true) {
        val n = input.read(buf)
        if (n < 0) break
        crc.update(buf, 0, n)
      }
    }
    return crc.value
  }

  private fun floats(v: FloatArray) = JSONArray(v.map { it.toDouble() })
}
