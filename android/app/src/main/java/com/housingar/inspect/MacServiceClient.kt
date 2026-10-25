package com.housingar.inspect

import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileInputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

/**
 * Minimal blocking HTTP client for the Mac segmentation service. Call only from a background
 * thread. The API contract is documented in segmentation-service/README.md.
 */
class MacServiceClient(private val baseUrl: () -> String) {

  data class Health(val ok: Boolean, val backend: String, val model: String, val device: String, val latencyMs: Long, val error: String?)

  fun health(): Health {
    val start = System.nanoTime()
    return try {
      val body = request("GET", "/health", null, null, timeoutMs = 2000)
      val json = JSONObject(body)
      Health(
        ok = json.optString("status") == "ok",
        backend = json.optString("backend"),
        model = json.optString("model"),
        device = json.optString("device"),
        latencyMs = (System.nanoTime() - start) / 1_000_000,
        error = null,
      )
    } catch (e: Exception) {
      Health(false, "", "", "", (System.nanoTime() - start) / 1_000_000, e.javaClass.simpleName + ": " + e.message)
    }
  }

  /** Single positive point prompt; see the list overload. */
  fun segment(frameId: String, jpeg: ByteArray, pointX: Float, pointY: Float, metadata: JSONObject): JSONObject =
    segment(frameId, jpeg, listOf(floatArrayOf(pointX, pointY, 1f)), metadata)

  /**
   * Sends one camera frame with point prompts [x, y, label] in the JPEG's pixel coordinates
   * (label 1 = include, 0 = exclude). Resending the same [frameId] with identical [jpeg] bytes
   * lets the service reuse its cached image embedding for corrections.
   */
  fun segment(frameId: String, jpeg: ByteArray, points: List<FloatArray>, metadata: JSONObject): JSONObject {
    val boundary = "----housingar" + UUID.randomUUID().toString().replace("-", "")
    val buf = ByteArrayOutputStream(jpeg.size + 4096)
    val out = DataOutputStream(buf)
    fun field(name: String, value: String) {
      out.writeBytes("--$boundary\r\n")
      out.writeBytes("Content-Disposition: form-data; name=\"$name\"\r\n")
      out.writeBytes("Content-Type: text/plain; charset=utf-8\r\n\r\n")
      out.write(value.toByteArray(Charsets.UTF_8))
      out.writeBytes("\r\n")
    }
    field("frame_id", frameId)
    val prompt = JSONArray()
    for (p in points) prompt.put(JSONArray().put(p[0].toDouble()).put(p[1].toDouble()).put(if (p[2] >= 0.5f) 1 else 0))
    field("points", prompt.toString())
    field("metadata", metadata.toString())
    out.writeBytes("--$boundary\r\n")
    out.writeBytes("Content-Disposition: form-data; name=\"image\"; filename=\"$frameId.jpg\"\r\n")
    out.writeBytes("Content-Type: image/jpeg\r\n\r\n")
    out.write(jpeg)
    out.writeBytes("\r\n--$boundary--\r\n")
    out.flush()
    val body = request("POST", "/v1/segment", buf.toByteArray(), "multipart/form-data; boundary=$boundary", timeoutMs = 60_000)
    return JSONObject(body)
  }

  /**
   * Uploads a capture (capture_v1): `manifest` JSON field + `archive` zip, streamed from disk.
   * [progress] receives (bytesSent, totalBytes). Returns {"capture_id", "job_id"}.
   */
  fun uploadCapture(manifest: JSONObject, archive: File, progress: (Long, Long) -> Unit): JSONObject =
    uploadArchive("/v1/captures", manifest, archive, progress)

  /** Uploads a "Scan stuff" walk-through (scan_v1). Returns {"scan_id", "job_id"}. */
  fun uploadScan(manifest: JSONObject, archive: File, progress: (Long, Long) -> Unit): JSONObject =
    uploadArchive("/v1/scans", manifest, archive, progress)

  /** Objects the Mac found in a finished scan: {"scan_id", "proposals": [...]}. */
  fun proposals(path: String): JSONObject = JSONObject(request("GET", path, null, null, timeoutMs = 15_000))

  /** "My stuff" on the Mac: {"items": [...]}. */
  fun library(): JSONObject = JSONObject(request("GET", "/v1/library", null, null, timeoutMs = 10_000))

  /** Adds a scan proposal or a finished single-object capture job to the library. Returns the item. */
  fun addToLibrary(name: String, source: JSONObject): JSONObject = JSONObject(request("POST", "/v1/library",
    JSONObject().put("name", name).put("source", source).toString().toByteArray(Charsets.UTF_8), "application/json", timeoutMs = 30_000))

  fun renameLibraryItem(itemId: String, name: String): JSONObject = JSONObject(request("PATCH", "/v1/library/$itemId",
    JSONObject().put("name", name).toString().toByteArray(Charsets.UTF_8), "application/json", timeoutMs = 10_000))

  fun deleteLibraryItem(itemId: String) {
    request("DELETE", "/v1/library/$itemId", null, null, timeoutMs = 10_000)
  }

  private fun uploadArchive(path: String, manifest: JSONObject, archive: File, progress: (Long, Long) -> Unit): JSONObject {
    val boundary = "----housingar" + UUID.randomUUID().toString().replace("-", "")
    val head = ("--$boundary\r\n" +
      "Content-Disposition: form-data; name=\"manifest\"\r\n" +
      "Content-Type: application/json; charset=utf-8\r\n\r\n").toByteArray(Charsets.UTF_8) +
      manifest.toString().toByteArray(Charsets.UTF_8) +
      ("\r\n--$boundary\r\n" +
        "Content-Disposition: form-data; name=\"archive\"; filename=\"${archive.name}\"\r\n" +
        "Content-Type: application/zip\r\n\r\n").toByteArray(Charsets.UTF_8)
    val tail = "\r\n--$boundary--\r\n".toByteArray(Charsets.UTF_8)
    val total = head.size + archive.length() + tail.size
    val conn = URL(baseUrl() + path).openConnection() as HttpURLConnection
    try {
      conn.requestMethod = "POST"
      conn.connectTimeout = 5000
      conn.readTimeout = 120_000
      conn.doOutput = true
      conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
      conn.setFixedLengthStreamingMode(total)
      var sent = 0L
      conn.outputStream.use { out ->
        out.write(head)
        sent += head.size
        FileInputStream(archive).use { input ->
          val buf = ByteArray(256 * 1024)
          while (true) {
            val n = input.read(buf)
            if (n < 0) break
            out.write(buf, 0, n)
            sent += n
            progress(sent, total)
          }
        }
        out.write(tail)
        sent += tail.size
        progress(sent, total)
      }
      return JSONObject(readBody(conn))
    } finally {
      conn.disconnect()
    }
  }

  fun job(jobId: String): JSONObject = JSONObject(request("GET", "/v1/jobs/$jobId", null, null, timeoutMs = 10_000))

  fun cancelJob(jobId: String): Boolean =
    try {
      request("POST", "/v1/jobs/$jobId/cancel", ByteArray(0), "application/json", timeoutMs = 10_000)
      true
    } catch (e: Exception) {
      Log.w(TAG, "cancel failed: $e")
      false
    }

  /** Downloads an output; [urlOrPath] may be absolute or relative to the service base URL. */
  fun download(urlOrPath: String, dest: File) {
    val url = if (urlOrPath.startsWith("http://") || urlOrPath.startsWith("https://")) urlOrPath
    else baseUrl() + (if (urlOrPath.startsWith("/")) urlOrPath else "/$urlOrPath")
    val conn = URL(url).openConnection() as HttpURLConnection
    try {
      conn.connectTimeout = 5000
      conn.readTimeout = 60_000
      val code = conn.responseCode
      if (code !in 200..299) throw ServiceException(code, conn.errorStream?.bufferedReader()?.use { it.readText() }?.take(300) ?: "")
      val tmp = File(dest.parentFile, dest.name + ".part")
      conn.inputStream.use { input -> tmp.outputStream().use { input.copyTo(it) } }
      if (!tmp.renameTo(dest)) throw java.io.IOException("could not move download to $dest")
    } finally {
      conn.disconnect()
    }
  }

  private fun readBody(conn: HttpURLConnection): String {
    val code = conn.responseCode
    val stream = if (code in 200..299) conn.inputStream else conn.errorStream
    val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
    if (code !in 200..299) throw ServiceException(code, text.take(300))
    return text
  }

  /** Posts a JSON document (diagnostic report or drift measurement). Best effort; returns false on failure. */
  fun postJson(path: String, json: JSONObject): Boolean =
    try {
      request("POST", path, json.toString().toByteArray(Charsets.UTF_8), "application/json", timeoutMs = 5000)
      true
    } catch (e: Exception) {
      Log.w(TAG, "POST $path failed: $e")
      false
    }

  private fun request(method: String, path: String, body: ByteArray?, contentType: String?, timeoutMs: Int): String {
    val conn = URL(baseUrl() + path).openConnection() as HttpURLConnection
    try {
      setMethod(conn, method)
      conn.connectTimeout = minOf(timeoutMs, 3000)
      conn.readTimeout = timeoutMs
      if (body != null) {
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", contentType)
        conn.setFixedLengthStreamingMode(body.size)
        conn.outputStream.use { it.write(body) }
      }
      return readBody(conn)
    } finally {
      conn.disconnect()
    }
  }

  /**
   * Android's HttpURLConnection accepts PATCH; the desktop JVM's (used by the contract tests) does
   * not, so there the method field is set directly. The Mac also honours X-HTTP-Method-Override.
   */
  private fun setMethod(conn: HttpURLConnection, method: String) {
    try {
      conn.requestMethod = method
    } catch (e: java.net.ProtocolException) {
      conn.setRequestProperty("X-HTTP-Method-Override", method)
      try {
        val f = HttpURLConnection::class.java.getDeclaredField("method")
        f.isAccessible = true
        f.set(conn, method)
      } catch (reflection: Exception) {
        conn.requestMethod = "POST"
      }
    }
  }

  class ServiceException(val code: Int, detail: String) : Exception("HTTP $code: $detail")

  companion object {
    private const val TAG = "MacServiceClient"
  }
}
