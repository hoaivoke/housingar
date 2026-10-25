package com.housingar.inspect.library

import android.util.Log
import com.housingar.inspect.MacServiceClient
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.math.cos
import kotlin.math.sin

/** One object the Mac found in a scan (docs/scan_v1.md, GET /v1/scans/<id>/proposals). */
class Proposal(
  val proposalId: String,
  val parentId: String?,
  val thumbnailUrl: String?,
  val meshUrl: String?,
  val dimensionsM: FloatArray?,
  val method: String?,
  val views: Int,
  val coverageDeg: Float,
  val observedFraction: Float,
  val lowQuality: Boolean,
  val warnings: List<String>,
) {
  val usable get() = meshUrl != null

  companion object {
    fun listFrom(json: JSONObject): List<Proposal> {
      val arr = json.optJSONArray("proposals") ?: return emptyList()
      return (0 until arr.length()).map { i ->
        val p = arr.getJSONObject(i)
        val q = p.optJSONObject("quality") ?: JSONObject()
        val w = q.optJSONArray("warnings") ?: JSONArray()
        Proposal(
          proposalId = p.getString("proposal_id"),
          parentId = p.optStringOrNull("parent_id"),
          thumbnailUrl = p.optStringOrNull("thumbnail_url"),
          meshUrl = p.optStringOrNull("mesh_url"),
          dimensionsM = p.optJSONArray("dimensions_m")?.toFloats(),
          method = p.optStringOrNull("method"),
          views = q.optInt("views", 0),
          coverageDeg = q.optDouble("coverage_deg", 0.0).toFloat(),
          observedFraction = q.optDouble("observed_fraction", 0.0).toFloat(),
          lowQuality = q.optBoolean("low_quality", false),
          warnings = (0 until w.length()).map { w.optString(it) },
        )
      }
    }
  }
}

/** An object in "My stuff" (GET /v1/library), plus where the phone keeps its offline copy. */
class LibraryItem(
  val itemId: String,
  val name: String,
  val dimensionsM: FloatArray?,
  val meshUrl: String?,
  val thumbnailUrl: String?,
  val method: String?,
  val lowQuality: Boolean,
  val created: String?,
) {
  fun toJson(): JSONObject = JSONObject()
    .put("item_id", itemId)
    .put("name", name)
    .put("dimensions_m", dimensionsM?.let { d -> JSONArray(d.map { it.toDouble() }) } ?: JSONObject.NULL)
    .put("mesh_url", meshUrl ?: JSONObject.NULL)
    .put("thumbnail_url", thumbnailUrl ?: JSONObject.NULL)
    .put("method", method ?: JSONObject.NULL)
    .put("low_quality", lowQuality)
    .put("created", created ?: JSONObject.NULL)

  /** Same content as [other] (a renamed or re-meshed item must be refreshed). */
  fun sameAs(other: LibraryItem) = toJson().toString() == other.toJson().toString()

  companion object {
    fun from(o: JSONObject) = LibraryItem(
      itemId = o.getString("item_id"),
      name = o.optString("name", "Unnamed"),
      dimensionsM = o.optJSONArray("dimensions_m")?.toFloats(),
      meshUrl = o.optStringOrNull("mesh_url"),
      thumbnailUrl = o.optStringOrNull("thumbnail_url"),
      method = o.optStringOrNull("method"),
      lowQuality = o.optBoolean("low_quality", false),
      created = o.optStringOrNull("created"),
    )

    fun listFrom(json: JSONObject): List<LibraryItem> {
      val arr = json.optJSONArray("items") ?: return emptyList()
      return (0 until arr.length()).map { from(arr.getJSONObject(it)) }
    }
  }
}

/**
 * The phone's offline copy of "My stuff": an index plus each item's mesh and thumbnail, so objects
 * can be placed in a new apartment without reaching the Mac. The Mac's library is the source of
 * truth; [plan] says what to download and delete after fetching it.
 */
class LibraryStore(private val dir: File) {
  class SyncPlan(val download: List<LibraryItem>, val delete: List<String>, val keep: List<LibraryItem>)

  private val index = File(dir, "index.json")

  init {
    dir.mkdirs()
  }

  fun load(): List<LibraryItem> = try {
    if (!index.isFile) emptyList() else {
      val arr = JSONArray(index.readText())
      (0 until arr.length()).map { LibraryItem.from(arr.getJSONObject(it)) }
    }
  } catch (e: Exception) {
    emptyList()
  }

  fun save(items: List<LibraryItem>) {
    val tmp = File(dir, "index.json.tmp")
    tmp.writeText(JSONArray(items.map { it.toJson() }).toString(2))
    if (!tmp.renameTo(index)) {
      index.delete()
      tmp.renameTo(index)
    }
  }

  fun itemDir(itemId: String) = File(dir, safe(itemId))
  fun meshFile(itemId: String) = File(itemDir(itemId), "object.glb")
  fun thumbnailFile(itemId: String) = File(itemDir(itemId), "thumbnail.jpg")
  fun isAvailableOffline(item: LibraryItem) = meshFile(item.itemId).isFile

  fun plan(remote: List<LibraryItem>): SyncPlan {
    val local = load().associateBy { it.itemId }
    val remoteIds = remote.map { it.itemId }.toSet()
    val download = remote.filter { r -> local[r.itemId]?.let { !it.sameAs(r) || !isAvailableOffline(r) } ?: true }
    val keep = remote.filter { it !in download }
    return SyncPlan(download, local.keys.filter { it !in remoteIds }, keep)
  }

  fun delete(itemId: String) {
    itemDir(itemId).deleteRecursively()
  }

  /**
   * Worker thread: fetch the Mac's library, download new or changed items' mesh and photo, drop
   * items deleted on the Mac, and save the index. Returns the items to show, plus the error if the
   * Mac could not be reached (then the saved offline copy is returned unchanged).
   */
  fun sync(client: MacServiceClient): Pair<List<LibraryItem>, String?> {
    val remote = try {
      LibraryItem.listFrom(client.library())
    } catch (e: Exception) {
      Log.w(TAG, "library sync failed: $e")
      return load() to (e.message ?: e.toString())
    }
    val plan = plan(remote)
    for (item in plan.download) {
      try {
        itemDir(item.itemId).mkdirs()
        item.meshUrl?.let { client.download(it, meshFile(item.itemId)) }
        item.thumbnailUrl?.let { client.download(it, thumbnailFile(item.itemId)) }
      } catch (e: Exception) {
        Log.w(TAG, "download ${item.itemId} failed: $e")
      }
    }
    plan.delete.forEach { delete(it) }
    save(remote)
    return remote to null
  }

  private fun safe(id: String) = id.replace(Regex("[^A-Za-z0-9_-]"), "_")

  companion object {
    private const val TAG = "LibraryStore"
  }
}

/** A placed library object in the new room: position on a surface, upright, turned by [yawDeg]. */
object Placement {
  /** Column-major model matrix: translate to [position], rotate [yawDeg] about world +Y. */
  fun model(position: FloatArray, yawDeg: Float): FloatArray {
    val a = Math.toRadians(yawDeg.toDouble())
    val c = cos(a).toFloat()
    val s = sin(a).toFloat()
    return floatArrayOf(c, 0f, -s, 0f, 0f, 1f, 0f, 0f, s, 0f, c, 0f, position[0], position[1], position[2], 1f)
  }

  /** Ray through a view pixel (origin top-left) from the inverse view-projection matrix. */
  fun rayFromView(inverseViewProjection: FloatArray, x: Float, y: Float, width: Int, height: Int): Pair<FloatArray, FloatArray> {
    val nx = 2f * x / width - 1f
    val ny = 1f - 2f * y / height
    val near = unproject(inverseViewProjection, nx, ny, -1f)
    val far = unproject(inverseViewProjection, nx, ny, 1f)
    val d = floatArrayOf(far[0] - near[0], far[1] - near[1], far[2] - near[2])
    val l = kotlin.math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
    return near to floatArrayOf(d[0] / l, d[1] / l, d[2] / l)
  }

  /**
   * Distance along the ray to an object's local box [min, max] under a rigid [model], or null.
   * The ray is moved into the object's frame (inverse of rotation about Y + translation).
   */
  fun hitBox(origin: FloatArray, dir: FloatArray, model: FloatArray, min: FloatArray, max: FloatArray): Float? {
    // Inverse rigid transform: R^T (p - t).
    val t = floatArrayOf(model[12], model[13], model[14])
    fun toLocal(v: FloatArray, point: Boolean): FloatArray {
      val p = if (point) floatArrayOf(v[0] - t[0], v[1] - t[1], v[2] - t[2]) else v
      return floatArrayOf(
        model[0] * p[0] + model[1] * p[1] + model[2] * p[2],
        model[4] * p[0] + model[5] * p[1] + model[6] * p[2],
        model[8] * p[0] + model[9] * p[1] + model[10] * p[2],
      )
    }
    val o = toLocal(origin, true)
    val d = toLocal(dir, false)
    var t0 = 0f
    var t1 = Float.MAX_VALUE
    for (k in 0..2) {
      if (kotlin.math.abs(d[k]) < 1e-9f) {
        if (o[k] < min[k] || o[k] > max[k]) return null
        continue
      }
      var a = (min[k] - o[k]) / d[k]
      var b = (max[k] - o[k]) / d[k]
      if (a > b) { val s = a; a = b; b = s }
      t0 = maxOf(t0, a)
      t1 = minOf(t1, b)
      if (t0 > t1) return null
    }
    return t0
  }

  /**
   * Layout record. The Mac's POST /v1/layouts accepts exactly {item_id, pose_world} per placement
   * (unknown fields are rejected), so object names go only into the phone's local copy ([withNames]).
   */
  fun layoutJson(layoutId: String, sessionId: String, name: String, placements: List<Triple<String, String, FloatArray>>, withNames: Boolean = false): JSONObject =
    JSONObject()
      .put("layout_id", layoutId)
      .put("session_id", sessionId)
      .put("name", name)
      .put("placements", JSONArray(placements.map { (itemId, itemName, pose) ->
        JSONObject().put("item_id", itemId).put("pose_world", JSONArray(pose.map { it.toDouble() }))
          .also { if (withNames) it.put("name", itemName) }
      }))

  /** The Mac accepts names of 1–80 characters. */
  const val MAX_NAME_LENGTH = 80

  private fun unproject(m: FloatArray, x: Float, y: Float, z: Float): FloatArray {
    val rx = m[0] * x + m[4] * y + m[8] * z + m[12]
    val ry = m[1] * x + m[5] * y + m[9] * z + m[13]
    val rz = m[2] * x + m[6] * y + m[10] * z + m[14]
    val rw = m[3] * x + m[7] * y + m[11] * z + m[15]
    return floatArrayOf(rx / rw, ry / rw, rz / rw)
  }
}

internal fun JSONObject.optStringOrNull(key: String): String? = if (!has(key) || isNull(key)) null else optString(key)
internal fun JSONArray.toFloats() = FloatArray(length()) { getDouble(it).toFloat() }
