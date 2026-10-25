package com.housingar.inspect

import com.housingar.inspect.capture.AnchorFrames
import com.housingar.inspect.capture.CaptureFormat
import com.housingar.inspect.capture.PointVisibility
import com.housingar.inspect.capture.TappedObject
import com.housingar.inspect.library.LibraryItem
import com.housingar.inspect.library.LibraryStore
import com.housingar.inspect.library.Placement
import com.housingar.inspect.library.Proposal
import com.housingar.inspect.mesh.GlbLoader
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sqrt

/**
 * The whole "Scan stuff" chain with the phone's own code against the real Mac service: the Mac's
 * synthetic walk-through (ground truth kept aside) is re-expressed in the level scan anchor the
 * phone's renderer creates, packaged as scan_v1 with CaptureFormat, uploaded with
 * MacServiceClient, polled like ScanController, reviewed like LibraryController (proposals,
 * thumbnails, meshes via GlbLoader), confirmed into My stuff, synced to the offline LibraryStore,
 * renamed, deleted, saved as a layout, and re-uploaded to check the 409 → new-ID retry and cancel.
 * Skipped unless -PsegmentationUrl=... -PscanE2eDir=<dir containing synth/> are passed.
 */
class ScanEndToEndMacTest {
  private fun floats(a: JSONArray) = FloatArray(a.length()) { a.getDouble(it).toFloat() }
  private fun intr(o: JSONObject) = CaptureFormat.Intrinsics(o.getDouble("fx").toFloat(), o.getDouble("fy").toFloat(), o.getDouble("cx").toFloat(), o.getDouble("cy").toFloat())
  private fun mul(a: FloatArray, b: FloatArray) = FloatArray(16).also { o ->
    for (r in 0..3) for (c in 0..3) { var v = 0f; for (k in 0..3) v += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = v }
  }
  private fun apply(m: FloatArray, p: FloatArray) = floatArrayOf(
    m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12], m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13], m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14])
  private fun invertRigid(m: FloatArray): FloatArray {
    val o = FloatArray(16)
    for (r in 0..2) for (c in 0..2) o[c * 4 + r] = m[r * 4 + c]
    for (r in 0..2) o[12 + r] = -(o[r] * m[12] + o[4 + r] * m[13] + o[8 + r] * m[14])
    o[15] = 1f
    return o
  }

  /** Width × height of a baseline or progressive JPEG, from its SOF marker. */
  private fun jpegSize(b: ByteArray): Pair<Int, Int>? {
    if (b.size < 4 || b[0] != 0xFF.toByte() || b[1] != 0xD8.toByte()) return null
    var i = 2
    while (i + 9 < b.size) {
      if (b[i] != 0xFF.toByte()) { i++; continue }
      val marker = b[i + 1].toInt() and 0xFF
      val len = ((b[i + 2].toInt() and 0xFF) shl 8) or (b[i + 3].toInt() and 0xFF)
      if (marker in 0xC0..0xC3) {
        val h = ((b[i + 5].toInt() and 0xFF) shl 8) or (b[i + 6].toInt() and 0xFF)
        val w = ((b[i + 7].toInt() and 0xFF) shl 8) or (b[i + 8].toInt() and 0xFF)
        return w to h
      }
      i += 2 + len
    }
    return null
  }

  private fun getJson(base: String, path: String): JSONObject {
    val c = URL(base + path).openConnection() as HttpURLConnection
    try {
      return JSONObject(c.inputStream.bufferedReader().use { it.readText() })
    } finally {
      c.disconnect()
    }
  }

  private fun waitFor(client: MacServiceClient, jobId: String, timeoutS: Int, states: MutableList<String>, terminal: Set<String>): JSONObject {
    val deadline = System.currentTimeMillis() + timeoutS * 1000L
    while (System.currentTimeMillis() < deadline) {
      val job = client.job(jobId)
      val s = job.getString("state")
      if (states.lastOrNull() != s) states += s
      if (s in terminal) return job
      Thread.sleep(1000)
    }
    fail("job $jobId did not finish in $timeoutS s (states $states)")
    throw IllegalStateException()
  }

  @Test
  fun phoneScanThroughMacToMyStuff() {
    val url = System.getProperty("segmentation.url").orEmpty()
    val root = System.getProperty("scan.e2e.dir").orEmpty()
    assumeTrue("needs -PsegmentationUrl and -PscanE2eDir", url.isNotBlank() && root.isNotBlank())
    val client = MacServiceClient { url }
    val result = JSONObject()
    val src = File(root, "synth")
    val truth = JSONObject(File(src, "ground_truth.json").readText()).getJSONArray("objects")
    val m = JSONObject(File(src, "manifest.json").readText())
    val fa = m.getJSONArray("frames")

    // 1. The phone's scan anchor: level, at the first tracked camera, +Z back towards the user
    //    (InspectRenderer.consumeRequests). Every pose is re-expressed in it.
    val generatorAnchorToWorld = floats(m.getJSONArray("anchor_to_world"))
    val cam0 = mul(generatorAnchorToWorld, floats(fa.getJSONObject(0).getJSONArray("camera_to_anchor")))
    val camPos = floatArrayOf(cam0[12], cam0[13], cam0[14])
    val anchorToWorld = AnchorFrames.gravityAligned(camPos, floatArrayOf(camPos[0] + cam0[8], camPos[1], camPos[2] + cam0[10])).matrix
    val worldToAnchor = invertRigid(anchorToWorld)
    val pkg = File(root, "phone_scan_pkg").apply { deleteRecursively(); mkdirs() }
    for (sub in listOf("frames", "depth", "confidence")) File(src, sub).takeIf { it.isDirectory }?.copyRecursively(File(pkg, sub))
    val frames = (0 until fa.length()).map { i ->
      val f = fa.getJSONObject(i)
      val d = f.getJSONObject("depth")
      val camToWorld = mul(generatorAnchorToWorld, floats(f.getJSONArray("camera_to_anchor")))
      CaptureFormat.Frame(f.getInt("index"), f.getLong("timestamp_ns"), f.getString("image"), f.getInt("width"), f.getInt("height"),
        intr(f.getJSONObject("intrinsics")), mul(worldToAnchor, camToWorld), camToWorld,
        CaptureFormat.Depth(d.getString("file"), d.getInt("width"), d.getInt("height"), d.getLong("timestamp_ns"), intr(d.getJSONObject("intrinsics")), floats(d.getJSONArray("to_image"))),
        f.optJSONObject("confidence")?.getString("file"), f.optDouble("sharpness", 100.0).toFloat(), 0f)
    }
    // Tapped things (scan_v2): the generator's taps, turned into the phone's TappedObjects, with
    // coverage measured by the phone's own visibility rule on every frame's depth.
    val tapped = mutableListOf<TappedObject>()
    val genObjects = m.optJSONArray("objects")
    if (genObjects != null) {
      File(src, "seeds").takeIf { it.isDirectory }?.copyRecursively(File(pkg, "seeds"))
      val depths = (0 until fa.length()).map { i ->
        val d = fa.getJSONObject(i).getJSONObject("depth")
        val raw = File(src, d.getString("file")).readBytes()
        ShortArray(raw.size / 2) { ((raw[it * 2].toInt() and 0xFF) or ((raw[it * 2 + 1].toInt() and 0xFF) shl 8)).toShort() }
      }
      for (n in 0 until genObjects.length()) {
        val g = genObjects.getJSONObject(n)
        val pw = apply(generatorAnchorToWorld, floats(g.getJSONArray("point_anchor")))
        val seedIndex = g.getInt("seed_frame_index")
        val pt = g.getJSONArray("points").getJSONArray(0)
        val o = TappedObject(n + 1, pw, apply(worldToAnchor, pw), seedIndex, floatArrayOf(pt.getDouble(0).toFloat(), pt.getDouble(1).toFloat()))
        assertEquals(g.getString("object_id"), o.objectId)
        o.maskFile = g.optString("mask").takeIf { !g.isNull("mask") && it.isNotBlank() }
        frames.forEachIndexed { i, f ->
          if (f.index == seedIndex || PointVisibility.visible(pw, f.cameraToWorld, f.intrinsics, f.width, f.height, depths[i], f.depth.width, f.depth.height, f.depth.toImage)) {
            o.coverage.add(floatArrayOf(f.cameraToWorld[12], f.cameraToWorld[13], f.cameraToWorld[14]))
          }
        }
        tapped += o
      }
      result.put("phone_coverage", JSONArray(tapped.map { JSONObject().put("object_id", it.objectId).put("views", it.coverage.views).put("coverage_deg", it.coverage.coverageDeg.toDouble()).put("ready", it.coverage.ready) }))
      assertTrue("phone coverage says a thing isn't covered: ${result.get("phone_coverage")}", tapped.all { it.coverage.ready })
    }
    val sessionId = "e2e${System.currentTimeMillis() % 100000}"
    val scanId = "${sessionId}_scan_${System.currentTimeMillis() % 100_000_000}"
    val manifest = CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest(scanId, sessionId, null, anchorToWorld, frames, tapped.map { it.toSeed() }))
    File(pkg, "manifest.json").writeText(manifest.toString(2))
    val zip = File(root, "$scanId.zip")
    zip.outputStream().buffered().use { CaptureFormat.writeZip(pkg, it) }

    // 2. Upload and poll like ScanController.
    val t0 = System.nanoTime()
    var lastProgress = 0L to 0L
    val up = client.uploadScan(manifest, zip) { s, t -> lastProgress = s to t }
    result.put("upload_s", (System.nanoTime() - t0) / 1e9).put("upload_bytes", lastProgress.second).put("scan_id", up.optString("scan_id"))
    assertEquals(scanId, up.getString("scan_id"))
    val states = mutableListOf<String>()
    val job = waitFor(client, up.getString("job_id"), 1800, states, setOf("done", "failed", "cancelled"))
    result.put("job_states", JSONArray(states)).put("processing_s", (System.nanoTime() - t0) / 1e9)
    if (job.getString("state") == "failed" && job.optString("error").contains("requires official SAM")) {
      // The upload was validated and accepted; tapped scans need the Mac's real SAM to process.
      result.put("note", "accepted and validated; processing needs official SAM: ${job.optString("error")}")
      File(root, "phone_scan_e2e_result.json").writeText(result.toString(2))
      assumeTrue("scan_v2 processing needs official SAM on the service", false)
    }
    assertEquals("job ended ${job.optString("state")}: ${job.optString("error")}", "done", job.getString("state"))
    val unknown = states.filter { it !in JobCard.STAGES && it !in setOf("done", "failed", "cancelled") }
    assertTrue("states without a phone label: $unknown", unknown.isEmpty())
    val proposalsPath = job.getJSONObject("outputs").optString("proposals").ifBlank { "/v1/scans/$scanId/proposals" }

    // 3. Review like LibraryController.showProposals.
    val rawProposals = client.proposals(proposalsPath)
    val proposals = Proposal.listFrom(rawProposals)
    val rawById = (0 until rawProposals.getJSONArray("proposals").length()).associate {
      val o = rawProposals.getJSONArray("proposals").getJSONObject(it); o.getString("proposal_id") to o
    }
    val dl = File(root, "phone_scan_downloads").apply { deleteRecursively(); mkdirs() }
    val rows = JSONArray()
    val matched = mutableMapOf<String, String>() // truth name → proposal id
    for (p in proposals) {
      val row = JSONObject().put("proposal_id", p.proposalId).put("parent_id", p.parentId ?: JSONObject.NULL)
        .put("views", p.views).put("coverage_deg", p.coverageDeg.toDouble()).put("observed_fraction", p.observedFraction.toDouble())
        .put("low_quality", p.lowQuality).put("warnings", JSONArray(p.warnings))
      p.thumbnailUrl?.let { t ->
        val f = File(dl, "${p.proposalId}.jpg")
        client.download(t, f)
        val size = jpegSize(f.readBytes())
        assertNotNull("thumbnail ${p.proposalId} is not a JPEG", size)
        row.put("thumbnail_px", JSONArray(listOf(size!!.first, size.second)))
        assertTrue("thumbnail ${p.proposalId} larger than 512 px: $size", max(size.first, size.second) <= 512)
      }
      if (!p.usable) { rows.put(row.put("mesh", false)); continue }
      val glbFile = File(dl, "${p.proposalId}.glb")
      client.download(p.meshUrl!!, glbFile)
      val mesh = GlbLoader.load(glbFile.readBytes())
      val (mn, mx) = mesh.bounds()
      val dims = p.dimensionsM!!
      val glbDims = FloatArray(3) { mx[it] - mn[it] }
      row.put("mesh", true).put("triangles", mesh.triangleCount).put("texture", mesh.baseColorImage != null)
        .put("dimensions_m", JSONArray(dims.map { it.toDouble() })).put("glb_bounds_m", JSONArray(glbDims.map { it.toDouble() }))
      for (k in 0..2) assertEquals("GLB size vs dimensions_m (${p.proposalId}, axis $k)", dims[k], glbDims[k], 0.005f)
      assertEquals("origin at the bottom (${p.proposalId})", 0f, mn[1], 0.005f)
      assertEquals("origin centred in x (${p.proposalId})", 0f, (mn[0] + mx[0]) / 2, 0.01f)
      assertEquals("origin centred in z (${p.proposalId})", 0f, (mn[2] + mx[2]) / 2, 0.01f)
      // Where it was in the room: phone scan anchor × object_to_anchor (the Mac only saw the phone's anchor).
      val objToAnchor = MeshController.findMatrix(rawById.getValue(p.proposalId), "object_to_anchor")!!
      val objToWorld = mul(anchorToWorld, objToAnchor)
      val bottom = apply(objToWorld, floatArrayOf(0f, 0f, 0f))
      var best: Pair<JSONObject, Float>? = null
      for (i in 0 until truth.length()) {
        val t = truth.getJSONObject(i)
        val tmin = floats(t.getJSONArray("min_world")); val tmax = floats(t.getJSONArray("max_world"))
        val tb = floatArrayOf((tmin[0] + tmax[0]) / 2, tmin[1], (tmin[2] + tmax[2]) / 2)
        val d = sqrt((0..2).map { (bottom[it] - tb[it]).let { v -> v * v } }.sum())
        if (best == null || d < best.second) best = t to d
      }
      val t = best!!.first
      val tmin = floats(t.getJSONArray("min_world")); val tmax = floats(t.getJSONArray("max_world"))
      val truthFoot = listOf(tmax[0] - tmin[0], tmax[2] - tmin[2]).sorted()
      val foot = listOf(dims[0], dims[2]).sorted()
      val dimErr = maxOf(abs(foot[0] - truthFoot[0]), abs(foot[1] - truthFoot[1]), abs(dims[1] - (tmax[1] - tmin[1])))
      matched[t.getString("name")] = p.proposalId
      row.put("matches", t.getString("name")).put("max_dimension_error_m", dimErr.toDouble()).put("bottom_centre_error_m", best.second.toDouble())
      rows.put(row)
    }
    result.put("proposals", rows)
    if (tapped.isNotEmpty()) {
      // Exactly the tapped things, in tap order, no parents.
      assertEquals(tapped.map { it.objectId }, proposals.map { it.proposalId })
      assertTrue(proposals.all { it.parentId == null })
    }
    // Parent links (automatic discovery only): items on the desk point at the desk's proposal.
    if (tapped.isEmpty()) for (i in 0 until truth.length()) {
      val t = truth.getJSONObject(i)
      val pid = matched[t.getString("name")] ?: continue
      val parent = proposals.first { it.proposalId == pid }.parentId
      val expected = if (t.isNull("parent")) null else matched[t.getString("parent")]
      assertEquals("parent of ${t.getString("name")}", expected, parent)
    }

    // 4. Confirm into My stuff (LibraryController.addProposals) and sync the offline copy.
    val names = mapOf("desk" to "Desk", "box" to "Shoe box", "cylinder" to "Plant pot", "chair" to "Desk chair")
    val added = matched.map { (truthName, pid) ->
      LibraryItem.from(client.addToLibrary(names[truthName] ?: truthName, JSONObject().put("scan_id", scanId).put("proposal_id", pid)))
    }
    val store = LibraryStore(File(root, "phone_library").apply { deleteRecursively() })
    val (synced, error) = store.sync(client)
    assertEquals(null, error)
    for (item in added) {
      val local = synced.first { it.itemId == item.itemId }
      assertTrue("${item.name} not offline", store.isAvailableOffline(local))
      val pid = matched.entries.first { names[it.key] == item.name }.value
      assertArrayEquals("library copy of ${item.name} differs from the proposal mesh", File(dl, "$pid.glb").readBytes(), store.meshFile(item.itemId).readBytes())
      assertTrue("${item.name} photo missing", store.thumbnailFile(item.itemId).length() > 0)
      val d = GlbLoader.load(store.meshFile(item.itemId).readBytes()).bounds().let { (a, b) -> FloatArray(3) { b[it] - a[it] } }
      for (k in 0..2) assertEquals(item.dimensionsM!![k], d[k], 0.005f)
    }
    assertTrue("nothing to do on a second sync", store.plan(LibraryItem.listFrom(client.library())).download.isEmpty())

    // Rename (PATCH) → the phone re-downloads that item and shows the new name.
    val renamed = added.first()
    client.renameLibraryItem(renamed.itemId, "  ${renamed.name} (renamed)  ")
    assertEquals(listOf(renamed.itemId), store.plan(LibraryItem.listFrom(client.library())).download.map { it.itemId })
    assertEquals("${renamed.name} (renamed)", store.sync(client).first.first { it.itemId == renamed.itemId }.name)

    // A temporary duplicate, deleted on the Mac, disappears from the phone on the next sync.
    val temp = LibraryItem.from(client.addToLibrary("Temporary", JSONObject().put("scan_id", scanId).put("proposal_id", matched.values.first())))
    store.sync(client)
    assertTrue(store.isAvailableOffline(temp))
    client.deleteLibraryItem(temp.itemId)
    assertTrue(store.sync(client).first.none { it.itemId == temp.itemId })
    assertTrue("deleted item still on the phone", !store.itemDir(temp.itemId).exists())

    // Offline: the Mac is unreachable, the saved copy is still there to place.
    val offline = LibraryStore(File(root, "phone_library")).sync(MacServiceClient { "http://127.0.0.1:9" })
    assertNotNull(offline.second)
    assertEquals(added.size, offline.first.size)

    // 5. Save a layout as LibraryController.saveLayout does.
    val placements = added.mapIndexed { i, item -> Triple(item.itemId, item.name, Placement.model(floatArrayOf(i * 1.5f, 0f, -2f), i * 30f)) }
    val layoutId = "${sessionId}_layout_e2e"
    assertTrue("layout rejected", client.postJson("/v1/layouts", Placement.layoutJson(layoutId, sessionId, "New bedroom", placements)))
    val layouts = getJson(url, "/v1/layouts").getJSONArray("layouts")
    val saved = (0 until layouts.length()).map { layouts.getJSONObject(it) }.first { it.getString("layout_id") == layoutId }
    assertEquals(added.size, saved.getJSONArray("placements").length())

    // 6. Retry path: the reused scan ID is refused (409), the suffixed one is accepted; cancel it.
    try {
      client.uploadScan(manifest, zip) { _, _ -> }
      fail("reused scan_id accepted")
    } catch (e: MacServiceClient.ServiceException) {
      assertEquals(409, e.code)
    }
    val retryManifest = CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest("${scanId}_r2", sessionId, null, anchorToWorld, frames, tapped.map { it.toSeed() }))
    val retry = client.uploadScan(retryManifest, zip) { _, _ -> }
    assertTrue(client.cancelJob(retry.getString("job_id")))
    val cancelStates = mutableListOf<String>()
    val cancelled = waitFor(client, retry.getString("job_id"), 120, cancelStates, setOf("done", "failed", "cancelled"))
    assertEquals("cancelled", cancelled.getString("state"))
    result.put("retry_cancel_states", JSONArray(cancelStates))

    result.put("library_items", JSONArray(added.map { it.name })).put("layout_id", layoutId)
    File(root, "phone_scan_e2e_result.json").writeText(result.toString(2))

    // Accuracy targets from docs/scan_results.md (2 cm size, 3 cm placement), checked last so the
    // result file is always written.
    assertEquals("objects found", truth.length(), matched.size)
    for (i in 0 until rows.length()) {
      val r = rows.getJSONObject(i)
      if (!r.has("matches")) continue
      assertTrue("${r.getString("matches")} size error ${r.getDouble("max_dimension_error_m")}", r.getDouble("max_dimension_error_m") <= 0.02)
      assertTrue("${r.getString("matches")} placement error ${r.getDouble("bottom_centre_error_m")}", r.getDouble("bottom_centre_error_m") <= 0.03)
    }
  }
}
