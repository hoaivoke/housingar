package com.housingar.inspect.library

import com.housingar.inspect.MacServiceClient
import com.housingar.inspect.capture.CaptureFormat
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.file.Files
import java.util.Collections
import kotlin.concurrent.thread

/**
 * The phone's side of the scan → proposals → My stuff → offline copy flow, against an in-process
 * fake of the Mac endpoints (the request and response shapes the phone expects; see android/README.md).
 */
class LibraryClientTest {
  private class Req(val method: String, val path: String, val body: String, val override: String?)

  /** Minimal HTTP/1.1 server (the JVM test classpath is android.jar, so no com.sun.net.httpserver). */
  private class Exchange(val requestMethod: String, val path: String, val headers: Map<String, String>, val body: ByteArray) {
    var code = 200
    var contentType = "application/json"
    var response = ByteArray(0)
  }

  private lateinit var server: ServerSocket
  private lateinit var client: MacServiceClient
  private val requests = Collections.synchronizedList(mutableListOf<Req>())
  private val glb = ByteArray(300) { (it * 7).toByte() }
  private var name = "Desk lamp"

  @Before
  fun start() {
    server = ServerSocket(0, 16, InetAddress.getByName("127.0.0.1"))
    thread(isDaemon = true) {
      while (!server.isClosed) {
        val socket = try { server.accept() } catch (e: Exception) { break }
        thread(isDaemon = true) { socket.use { serve(it) } }
      }
    }
    client = MacServiceClient { "http://127.0.0.1:${server.localPort}" }
  }

  @After
  fun stop() = server.close()

  private fun serve(socket: Socket) {
    val input = BufferedInputStream(socket.getInputStream())
    val requestLine = readLine(input) ?: return
    val headers = mutableMapOf<String, String>()
    while (true) {
      val line = readLine(input) ?: return
      if (line.isEmpty()) break
      val k = line.indexOf(':')
      if (k > 0) headers[line.substring(0, k).trim().lowercase()] = line.substring(k + 1).trim()
    }
    val length = headers["content-length"]?.toInt() ?: 0
    val body = ByteArray(length)
    var read = 0
    while (read < length) {
      val n = input.read(body, read, length - read)
      if (n < 0) break
      read += n
    }
    val parts = requestLine.split(" ")
    val ex = Exchange(parts[0], parts[1].substringBefore('?'), headers, body)
    handle(ex)
    val out = socket.getOutputStream()
    out.write(("HTTP/1.1 ${ex.code} X\r\nContent-Type: ${ex.contentType}\r\nContent-Length: ${ex.response.size}\r\nConnection: close\r\n\r\n").toByteArray())
    out.write(ex.response)
    out.flush()
  }

  private fun readLine(input: InputStream): String? {
    val b = ByteArrayOutputStream()
    while (true) {
      val c = input.read()
      if (c < 0) return if (b.size() == 0) null else b.toString(Charsets.ISO_8859_1.name())
      if (c == '\n'.code) return b.toString(Charsets.ISO_8859_1.name()).trimEnd('\r')
      b.write(c)
    }
  }

  private fun item() = JSONObject()
    .put("item_id", "item_1").put("name", name).put("dimensions_m", org.json.JSONArray(listOf(0.2, 0.45, 0.2)))
    .put("mesh_url", "/v1/library/item_1/files/object.glb").put("thumbnail_url", "/v1/library/item_1/files/thumbnail.jpg")
    .put("method", "scan").put("low_quality", false).put("created", "2026-10-24T20:00:00Z")

  private fun handle(ex: Exchange) {
    val text = String(ex.body, Charsets.ISO_8859_1)
    val path = ex.path
    requests += Req(ex.requestMethod, path, text, ex.headers["x-http-method-override"])
    fun json(o: JSONObject, code: Int = 200) {
      ex.code = code
      ex.response = o.toString().toByteArray()
    }
    when {
      ex.requestMethod == "POST" && path == "/v1/scans" -> json(JSONObject().put("scan_id", "s1").put("job_id", "job_s1"), 202)
      path == "/v1/jobs/job_s1" -> json(JSONObject().put("job_id", "job_s1").put("state", "done").put("progress", 1.0)
        .put("outputs", JSONObject().put("proposals", "/v1/scans/s1/proposals")))
      path == "/v1/scans/s1/proposals" -> json(JSONObject("""
        {"scan_id": "s1", "proposals": [
          {"proposal_id": "p1", "parent_id": null, "thumbnail_url": "/v1/scans/s1/files/p1.jpg", "mesh_url": "/v1/scans/s1/files/p1.glb",
           "dimensions_m": [0.2, 0.45, 0.2], "method": "scan",
           "quality": {"views": 41, "coverage_deg": 300, "observed_fraction": 0.9, "low_quality": false, "warnings": []}},
          {"proposal_id": "p2", "parent_id": "p1", "thumbnail_url": null, "mesh_url": null, "dimensions_m": null, "method": "scan",
           "quality": {"views": 3, "coverage_deg": 40, "observed_fraction": 0.2, "low_quality": true, "warnings": ["seen from one side only"]}}
        ]}"""))
      ex.requestMethod == "POST" && path == "/v1/library" -> {
        name = JSONObject(text).getString("name")
        json(item(), 201)
      }
      ex.requestMethod == "GET" && path == "/v1/library" -> json(JSONObject().put("items", org.json.JSONArray().put(item())))
      ex.requestMethod == "PATCH" && path == "/v1/library/item_1" -> {
        name = JSONObject(text).getString("name")
        json(item())
      }
      ex.requestMethod == "DELETE" && path == "/v1/library/item_1" -> json(JSONObject().put("deleted", "item_1"))
      path == "/v1/library/item_1/files/object.glb" -> {
        ex.contentType = "model/gltf-binary"
        ex.response = glb
      }
      else -> json(JSONObject().put("detail", "not found"), 404)
    }
  }

  @Test
  fun scanUploadToLibraryRoundTrip() {
    // Upload a (tiny) scan: multipart with a scan_v1 manifest and the archive.
    val dir = Files.createTempDirectory("scan").toFile()
    File(dir, "frames").mkdirs()
    File(dir, "frames/0000.jpg").writeBytes(byteArrayOf(1, 2, 3))
    val zip = File(dir.parentFile, "${dir.name}.zip")
    zip.outputStream().use { CaptureFormat.writeZip(dir, it) }
    val identity = FloatArray(16).also { it[0] = 1f; it[5] = 1f; it[10] = 1f; it[15] = 1f }
    val manifest = CaptureFormat.scanManifestJson(CaptureFormat.ScanManifest("abc_scan_1", "abc", null, identity, emptyList()))
    var lastProgress = 0L to 0L
    val up = client.uploadScan(manifest, zip) { sent, total -> lastProgress = sent to total }
    assertEquals("job_s1", up.getString("job_id"))
    assertEquals(lastProgress.second, lastProgress.first)
    val upload = requests.first { it.path == "/v1/scans" }
    assertTrue(upload.body.contains("name=\"manifest\""))
    assertTrue(upload.body.contains("\"schema\":\"housingar.scan.v1\""))
    assertTrue(upload.body.contains("name=\"archive\"; filename=\"${zip.name}\""))

    // Job done → proposals.
    val job = client.job("job_s1")
    val proposals = Proposal.listFrom(client.proposals(job.getJSONObject("outputs").getString("proposals")))
    assertEquals(listOf("p1", "p2"), proposals.map { it.proposalId })
    assertTrue(proposals[0].usable && !proposals[0].lowQuality)
    assertEquals("p1", proposals[1].parentId)
    assertTrue(!proposals[1].usable && proposals[1].lowQuality)
    assertEquals(listOf("seen from one side only"), proposals[1].warnings)

    // Keep p1 under a name.
    val added = LibraryItem.from(client.addToLibrary("Desk lamp", JSONObject().put("scan_id", "s1").put("proposal_id", "p1")))
    assertEquals("item_1", added.itemId)
    val post = JSONObject(requests.last { it.path == "/v1/library" && it.method == "POST" }.body)
    assertEquals("Desk lamp", post.getString("name"))
    assertEquals("p1", post.getJSONObject("source").getString("proposal_id"))

    // Offline copy: plan → download → save; a second plan has nothing to do.
    val store = LibraryStore(Files.createTempDirectory("lib").toFile())
    val remote = LibraryItem.listFrom(client.library())
    val plan = store.plan(remote)
    assertEquals(listOf("item_1"), plan.download.map { it.itemId })
    store.itemDir("item_1").mkdirs()
    client.download(remote[0].meshUrl!!, store.meshFile("item_1"))
    store.save(remote)
    assertArrayEquals(glb, store.meshFile("item_1").readBytes())
    assertTrue(store.isAvailableOffline(remote[0]))
    assertTrue(store.plan(LibraryItem.listFrom(client.library())).download.isEmpty())

    // Rename (PATCH, which the desktop JVM's HttpURLConnection does not allow directly).
    client.renameLibraryItem("item_1", "Reading lamp")
    val patch = requests.last { it.path == "/v1/library/item_1" }
    assertEquals("PATCH", patch.method)
    assertEquals("Reading lamp", JSONObject(patch.body).getString("name"))
    // A renamed item must be refreshed in the offline index.
    assertEquals(listOf("item_1"), store.plan(LibraryItem.listFrom(client.library())).download.map { it.itemId })

    client.deleteLibraryItem("item_1")
    assertEquals("DELETE", requests.last().method)
  }
}
