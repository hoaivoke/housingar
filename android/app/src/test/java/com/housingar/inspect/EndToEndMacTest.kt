package com.housingar.inspect

import com.housingar.inspect.capture.CaptureFormat
import com.housingar.inspect.mesh.GlbLoader
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * Whole Create-mesh chain with the phone's own code at both ends: a ground-truth scene (the Mac's
 * synthetic generator) is re-packaged with CaptureFormat, uploaded with MacServiceClient, processed
 * by the real Mac job, polled, downloaded, loaded with GlbLoader and placed with object_to_anchor.
 * Skipped unless -PsegmentationUrl=... -Pe2eDir=<dir containing synth/> are passed.
 */
class EndToEndMacTest {
  private fun floats(a: JSONArray) = FloatArray(a.length()) { a.getDouble(it).toFloat() }
  private fun intr(o: JSONObject) = CaptureFormat.Intrinsics(o.getDouble("fx").toFloat(), o.getDouble("fy").toFloat(), o.getDouble("cx").toFloat(), o.getDouble("cy").toFloat())
  private fun mul(a: FloatArray, b: FloatArray) = FloatArray(16).also { o ->
    for (r in 0..3) for (c in 0..3) { var v = 0f; for (k in 0..3) v += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = v }
  }
  private fun apply(m: FloatArray, p: FloatArray, w: Float = 1f) = floatArrayOf(
    m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12] * w, m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13] * w, m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14] * w)

  @Test
  fun phoneCaptureThroughMacReconstruction() {
    val url = System.getProperty("segmentation.url").orEmpty()
    val root = System.getProperty("e2e.dir").orEmpty()
    assumeTrue("needs -PsegmentationUrl and -Pe2eDir", url.isNotBlank() && root.isNotBlank())
    val src = File(root, "synth")
    val truth = JSONObject(File(src, "ground_truth.json").readText())
    val m = JSONObject(File(src, "manifest.json").readText())

    // 1. Re-package with the phone's code.
    val pkg = File(root, "phone_pkg").apply { deleteRecursively(); mkdirs() }
    for (sub in listOf("frames", "depth", "confidence")) File(src, sub).copyRecursively(File(pkg, sub))
    File(src, "seed_mask.png").copyTo(File(pkg, "seed_mask.png"))
    val fa = m.getJSONArray("frames")
    // The generator's anchor is deliberately tilted; the phone creates level, camera-facing anchors
    // (AnchorFrames), so re-express every pose in the anchor the phone would have created.
    val generatorAnchorToWorld = floats(m.getJSONArray("anchor_to_world"))
    val seed0 = fa.getJSONObject(m.getJSONObject("seed").getInt("frame_index"))
    val seedCamWorld = mul(generatorAnchorToWorld, floats(seed0.getJSONArray("camera_to_anchor")))
    val anchorToWorld = com.housingar.inspect.capture.AnchorFrames.gravityAligned(
      floatArrayOf(generatorAnchorToWorld[12], generatorAnchorToWorld[13], generatorAnchorToWorld[14]),
      floatArrayOf(seedCamWorld[12], seedCamWorld[13], seedCamWorld[14])).matrix
    val worldToAnchor = invertRigid(anchorToWorld)
    val frames = (0 until fa.length()).map { i ->
      val f = fa.getJSONObject(i)
      val d = f.getJSONObject("depth")
      val camToWorld = mul(generatorAnchorToWorld, floats(f.getJSONArray("camera_to_anchor")))
      CaptureFormat.Frame(f.getInt("index"), f.getLong("timestamp_ns"), f.getString("image"), f.getInt("width"), f.getInt("height"),
        intr(f.getJSONObject("intrinsics")), mul(worldToAnchor, camToWorld), camToWorld,
        CaptureFormat.Depth(d.getString("file"), d.getInt("width"), d.getInt("height"), d.getLong("timestamp_ns"), intr(d.getJSONObject("intrinsics")), floats(d.getJSONArray("to_image"))),
        f.optJSONObject("confidence")?.getString("file"), f.optDouble("sharpness", 100.0).toFloat(), f.optDouble("orbit_deg", 0.0).toFloat())
    }
    val seed = m.getJSONObject("seed")
    val pts = seed.getJSONArray("points")
    // The seed box comes from the phone's own SeedExtent on the seed frame, not from the generator.
    val sf = fa.getJSONObject(seed.getInt("frame_index"))
    val sd = sf.getJSONObject("depth")
    val dW = sd.getInt("width")
    val dH = sd.getInt("height")
    val dk = sd.getJSONObject("intrinsics")
    val ti = floats(sd.getJSONArray("to_image"))
    val raw = File(src, sd.getString("file")).readBytes()
    val mm = ShortArray(dW * dH) { ((raw[it * 2].toInt() and 0xFF) or ((raw[it * 2 + 1].toInt() and 0xFF) shl 8)).toShort() }
    val depthImage = com.housingar.inspect.geometry.SurfaceGeometry.DepthImage(dW, dH, mm,
      com.housingar.inspect.geometry.SurfaceGeometry.Intrinsics(dk.getDouble("fx").toFloat(), dk.getDouble("fy").toFloat(), dk.getDouble("cx").toFloat(), dk.getDouble("cy").toFloat(), dW, dH),
      floatArrayOf(ti[2], ti[3], ti[2] + ti[0] * dW, ti[3] + ti[1] * dH))
    val maskBytes = File(src, "seed_mask.u8").readBytes()
    val mask = com.housingar.inspect.geometry.SurfaceGeometry.Mask(sf.getInt("width"), sf.getInt("height"), BooleanArray(maskBytes.size) { maskBytes[it] != 0.toByte() })
    // Same inputs as on the phone: camera→world at capture, world→anchor, anchor→world.
    val extent = com.housingar.inspect.capture.SeedExtent.compute(mask, depthImage, seedCamWorld, worldToAnchor, anchorToWorld)
    assertNotNull("phone SeedExtent", extent)
    val captureId = "phone_e2e_${System.currentTimeMillis() % 1_000_000}"
    val manifest = CaptureFormat.manifestJson(CaptureFormat.Manifest(captureId, "phone_e2e", null, anchorToWorld, seed.getInt("frame_index"),
      (0 until pts.length()).map { floats(pts.getJSONArray(it)) }, "seed_mask.png", extent!!.bboxMinAnchor, extent.bboxMaxAnchor, frames))
    val zip = File(root, "$captureId.zip")
    zip.outputStream().use { CaptureFormat.writeZip(pkg, it) }

    // 2. Upload and poll with the phone's client.
    val client = MacServiceClient { url }
    var lastProgress = 0L
    val started = System.currentTimeMillis()
    val resp = client.uploadCapture(manifest, zip) { sent, total -> if (sent == total) lastProgress = sent }
    assertEquals(zip.length() > 0, lastProgress > 0)
    val jobId = resp.getString("job_id")
    val states = LinkedHashSet<String>()
    var job: JSONObject
    while (true) {
      job = client.job(jobId)
      states.add(job.getString("state"))
      val p = job.getDouble("progress")
      assertTrue("progress $p", p in 0.0..1.0)
      if (job.getString("state") in setOf("done", "failed", "cancelled")) break
      assertTrue("job timed out", System.currentTimeMillis() - started < 25 * 60_000)
      Thread.sleep(2000)
    }
    assertEquals("job ended ${job.getString("state")}: ${job.optString("error")}", "done", job.getString("state"))

    // 3. Download with the phone's URL handling and load with the phone's loader.
    val out = File(root, "phone_downloads").apply { deleteRecursively(); mkdirs() }
    val outputs = job.getJSONObject("outputs")
    for ((key, name) in listOf("object_glb" to "object.glb", "background_glb" to "background.glb", "report" to "report.json")) {
      client.download(MeshController.resolveOutputUrl(outputs.getString(key), jobId), File(out, name))
    }
    val obj = GlbLoader.load(File(out, "object.glb").readBytes())
    val bg = GlbLoader.load(File(out, "background.glb").readBytes())
    val report = JSONObject(File(out, "report.json").readText())
    val o2a = MeshController.findMatrix(report, "object_to_anchor")
    assertNotNull("report.object_to_anchor", o2a)

    // 4. Compare with ground truth.
    val dims = floats(truth.getJSONArray("object_dimensions_m"))
    val tMin = floats(truth.getJSONArray("object_min_world"))
    val tMax = floats(truth.getJSONArray("object_max_world"))
    val (mn, mx) = obj.bounds()
    val ext = FloatArray(3) { mx[it] - mn[it] }
    val horiz = listOf(ext[0], ext[2]).sorted()
    val truthHoriz = listOf(dims[0], dims[2]).sorted()
    val objToWorld = mul(anchorToWorld, o2a!!)
    val bottomWorld = apply(objToWorld, floatArrayOf(0f, 0f, 0f))
    val truthBottom = floatArrayOf((tMin[0] + tMax[0]) / 2, tMin[1], (tMin[2] + tMax[2]) / 2)
    val placementErr = sqrt((0..2).sumOf { ((bottomWorld[it] - truthBottom[it]) * (bottomWorld[it] - truthBottom[it])).toDouble() })
    val up = apply(objToWorld, floatArrayOf(0f, 1f, 0f), 0f)
    val worldMin = floatArrayOf(Float.MAX_VALUE, Float.MAX_VALUE, Float.MAX_VALUE)
    val worldMax = floatArrayOf(-Float.MAX_VALUE, -Float.MAX_VALUE, -Float.MAX_VALUE)
    for (i in 0 until obj.vertexCount) {
      val w = apply(objToWorld, floatArrayOf(obj.positions[i * 3], obj.positions[i * 3 + 1], obj.positions[i * 3 + 2]))
      for (k in 0..2) { worldMin[k] = minOf(worldMin[k], w[k]); worldMax[k] = maxOf(worldMax[k], w[k]) }
    }
    // Corner truncation check: the mesh must reach each true top corner (within 2 cm).
    var cornersReached = 0
    for (c in 0 until 4) {
      val tc = floatArrayOf(if (c and 1 == 0) tMin[0] else tMax[0], tMax[1], if (c and 2 == 0) tMin[2] else tMax[2])
      var best = Float.MAX_VALUE
      for (i in 0 until obj.vertexCount) {
        val w = apply(objToWorld, floatArrayOf(obj.positions[i * 3], obj.positions[i * 3 + 1], obj.positions[i * 3 + 2]))
        val d = sqrt((w[0] - tc[0]) * (w[0] - tc[0]) + (w[1] - tc[1]) * (w[1] - tc[1]) + (w[2] - tc[2]) * (w[2] - tc[2]))
        if (d < best) best = d
      }
      if (best < 0.02f) cornersReached++
    }
    // Background patch (anchor frame → world): cast rays straight down through the object's
    // footprint; each should hit patch support at table height, not object geometry.
    val n = bg.vertexCount
    val world = FloatArray(n * 3).also { w -> for (i in 0 until n) apply(anchorToWorld, floatArrayOf(bg.positions[i * 3], bg.positions[i * 3 + 1], bg.positions[i * 3 + 2])).copyInto(w, i * 3) }
    var rays = 0
    var under = 0
    var blockedInside = 0
    for (gx in 0..9) for (gz in 0..9) {
      val x = tMin[0] + 0.02f + (tMax[0] - tMin[0] - 0.04f) * gx / 9f
      val z = tMin[2] + 0.02f + (tMax[2] - tMin[2] - 0.04f) * gz / 9f
      rays++
      val t = firstHitDown(world, bg.indices, x, z, tMax[1] + 0.3f) ?: continue
      if (abs(t - tMin[1]) < 0.03f) under++
      if (t > tMin[1] + 0.03f) blockedInside++
    }
    val insideObject = blockedInside
    // Removal region (object bounds + phone margin) must contain the true box.
    val worldToObj = invertRigid(objToWorld)
    val margin = com.housingar.inspect.mesh.RemovalRenderer.REGION_MARGIN_M
    var cornersInside = 0
    for (c in 0 until 8) {
      val p = apply(worldToObj, floatArrayOf(if (c and 1 == 0) tMin[0] else tMax[0], if (c and 2 == 0) tMin[1] else tMax[1], if (c and 4 == 0) tMin[2] else tMax[2]))
      if ((0..2).all { p[it] >= mn[it] - margin - 1e-3f && p[it] <= mx[it] + margin + 1e-3f }) cornersInside++
    }

    val result = JSONObject()
      .put("capture_id", captureId).put("job_id", jobId).put("states_seen", JSONArray(states.toList()))
      .put("seconds", (System.currentTimeMillis() - started) / 1000.0)
      .put("archive_bytes", zip.length())
      .put("object_triangles", obj.triangleCount).put("background_triangles", bg.triangleCount)
      .put("object_extent_m", JSONArray(ext.map { it.toDouble() }))
      .put("truth_dims_m", JSONArray(dims.map { it.toDouble() }))
      .put("placement_error_m", placementErr)
      .put("object_world_min", JSONArray(worldMin.map { it.toDouble() })).put("object_world_max", JSONArray(worldMax.map { it.toDouble() }))
      .put("true_top_corners_reached", cornersReached)
      .put("object_up_in_world", JSONArray(up.map { it.toDouble() }))
      .put("seed_bbox_min_anchor", JSONArray(extent.bboxMinAnchor.map { it.toDouble() }))
      .put("seed_bbox_max_anchor", JSONArray(extent.bboxMaxAnchor.map { it.toDouble() }))
      .put("seed_support_height_anchor", extent.supportAnchor?.toDouble())
      .put("background_vertices", n).put("footprint_rays", rays).put("footprint_rays_hitting_support", under).put("footprint_rays_blocked_above_support", insideObject)
      .put("report_low_quality", MeshController.findValue(report, "low_quality"))
      .put("report_dimensions_m", MeshController.findValue(report, "object_dimensions_m"))
      .put("truth_corners_inside_removal_region", cornersInside)
      .put("report_method", MeshController.findValue(report, "method")?.toString())
      .put("report_warnings", MeshController.findValue(report, "warnings") ?: JSONArray())
    File(root, "e2e_result.json").writeText(result.toString(2))

    // The truth box is world-aligned: compare the placed mesh's world bounds face by face. (The
    // report's object-frame dimensions follow the frame's yaw, not the object's sides.)
    assertTrue("height ${ext[1]} vs ${dims[1]}", abs(ext[1] - dims[1]) < 0.03f)
    for (k in 0..2) {
      assertTrue("world min[$k] ${worldMin[k]} vs ${tMin[k]}", worldMin[k] - tMin[k] in -0.015f..(if (k == 1) 0.025f else 0.015f))
      assertTrue("world max[$k] ${worldMax[k]} vs ${tMax[k]}", worldMax[k] - tMax[k] in -0.015f..0.015f)
    }
    assertTrue("placement error $placementErr m", placementErr < 0.03)
    assertTrue("object up $up", up[1] > 0.99f)
    assertTrue("patch support under the object: $under / $rays rays", under >= rays * 0.95)
    assertTrue("patch geometry inside the object: $insideObject rays", insideObject == 0)
    assertEquals("Mac flagged the result low quality", false, MeshController.findValue(report, "low_quality"))
    assertEquals("removal region must contain the whole true object", 8, cornersInside)
    assertEquals("mesh truncated at a corner", 4, cornersReached)

    // 5. "Capture one" → My stuff, as MeshController.addToLibrary does once the user names it.
    val item = com.housingar.inspect.library.LibraryItem.from(client.addToLibrary("Lamp base", JSONObject().put("job_id", jobId)))
    assertEquals("Lamp base", item.name)
    val store = com.housingar.inspect.library.LibraryStore(File(root, "phone_library_capture").apply { deleteRecursively() })
    val (items, error) = store.sync(client)
    assertEquals(null, error)
    assertTrue(items.any { it.itemId == item.itemId })
    assertTrue("library copy differs from the job's object.glb",
      File(out, "object.glb").readBytes().contentEquals(store.meshFile(item.itemId).readBytes()))
    val thumb = store.thumbnailFile(item.itemId).readBytes()
    assertTrue("seed-frame photo is not a JPEG", thumb.size > 100 && thumb[0] == 0xFF.toByte() && thumb[1] == 0xD8.toByte())
    client.deleteLibraryItem(item.itemId)
  }

  private fun identity4() = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }

  /** Highest triangle hit by a vertical ray going down from (x, top, z); returns the hit height. */
  private fun firstHitDown(v: FloatArray, idx: IntArray, x: Float, z: Float, top: Float): Float? {
    var best: Float? = null
    for (t in 0 until idx.size / 3) {
      val a = idx[t * 3] * 3; val b = idx[t * 3 + 1] * 3; val c = idx[t * 3 + 2] * 3
      // Barycentric test in the XZ plane, then interpolate height.
      val x0 = v[a]; val z0 = v[a + 2]; val x1 = v[b]; val z1 = v[b + 2]; val x2 = v[c]; val z2 = v[c + 2]
      val det = (z1 - z2) * (x0 - x2) + (x2 - x1) * (z0 - z2)
      if (abs(det) < 1e-12f) continue
      val l0 = ((z1 - z2) * (x - x2) + (x2 - x1) * (z - z2)) / det
      val l1 = ((z2 - z0) * (x - x2) + (x0 - x2) * (z - z2)) / det
      val l2 = 1f - l0 - l1
      if (l0 < 0f || l1 < 0f || l2 < 0f) continue
      val y = l0 * v[a + 1] + l1 * v[b + 1] + l2 * v[c + 1]
      if (y <= top && (best == null || y > best)) best = y
    }
    return best
  }

  private fun invertRigid(m: FloatArray): FloatArray {
    val o = FloatArray(16)
    for (r in 0..2) for (c in 0..2) o[c * 4 + r] = m[r * 4 + c]
    for (r in 0..2) o[12 + r] = -(o[r] * m[12] + o[4 + r] * m[13] + o[8 + r] * m[14])
    o[15] = 1f
    return o
  }
}
