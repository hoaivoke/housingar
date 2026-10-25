package com.housingar.inspect.mesh

import org.json.JSONArray
import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** CPU-side mesh: interleaving is left to the renderer. Positions in metres. */
class MeshData(
  val positions: FloatArray,
  val normals: FloatArray?,
  val uvs: FloatArray?,
  val indices: IntArray,
  /** Encoded base-colour image (PNG/JPEG) or null. */
  val baseColorImage: ByteArray?,
  val baseColorMime: String?,
  val baseColorFactor: FloatArray,
) {
  val vertexCount get() = positions.size / 3
  val triangleCount get() = indices.size / 3

  fun bounds(): Pair<FloatArray, FloatArray> {
    val min = floatArrayOf(Float.MAX_VALUE, Float.MAX_VALUE, Float.MAX_VALUE)
    val max = floatArrayOf(-Float.MAX_VALUE, -Float.MAX_VALUE, -Float.MAX_VALUE)
    for (i in 0 until vertexCount) for (k in 0..2) {
      val v = positions[i * 3 + k]
      if (v < min[k]) min[k] = v
      if (v > max[k]) max[k] = v
    }
    return min to max
  }
}

class GlbFormatException(message: String) : Exception(message)

/**
 * Minimal glTF 2.0 binary (.glb) loader for the meshes produced by the Mac service: one mesh, one
 * triangle primitive, POSITION / NORMAL / TEXCOORD_0, optional indices, one embedded base-colour
 * image, no compression extensions. A node transform, if present, is baked into the vertices.
 * Anything else fails loudly rather than rendering something wrong.
 */
object GlbLoader {
  private const val MAGIC = 0x46546C67 // "glTF"
  private const val CHUNK_JSON = 0x4E4F534A
  private const val CHUNK_BIN = 0x004E4942

  fun load(bytes: ByteArray): MeshData {
    val buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
    if (bytes.size < 20 || buf.getInt(0) != MAGIC) throw GlbFormatException("not a GLB file")
    if (buf.getInt(4) != 2) throw GlbFormatException("unsupported glTF version ${buf.getInt(4)}")
    val total = buf.getInt(8)
    if (total > bytes.size) throw GlbFormatException("truncated GLB ($total > ${bytes.size} bytes)")

    var offset = 12
    var json: JSONObject? = null
    var bin: ByteBuffer? = null
    while (offset + 8 <= total) {
      val len = buf.getInt(offset)
      val type = buf.getInt(offset + 4)
      val start = offset + 8
      if (start + len > total) throw GlbFormatException("chunk overruns file")
      when (type) {
        CHUNK_JSON -> json = JSONObject(String(bytes, start, len, Charsets.UTF_8).trimEnd(' ', '\u0000'))
        CHUNK_BIN -> bin = ByteBuffer.wrap(bytes, start, len).slice().order(ByteOrder.LITTLE_ENDIAN)
      }
      offset = start + len
    }
    val gltf = json ?: throw GlbFormatException("missing JSON chunk")
    val binary = bin ?: throw GlbFormatException("missing BIN chunk")

    gltf.optJSONArray("extensionsRequired")?.let {
      if (it.length() > 0) throw GlbFormatException("required extensions not supported: $it")
    }
    val meshes = gltf.optJSONArray("meshes") ?: throw GlbFormatException("no meshes")
    if (meshes.length() != 1) throw GlbFormatException("expected exactly one mesh, found ${meshes.length()}")
    val prims = meshes.getJSONObject(0).getJSONArray("primitives")
    if (prims.length() != 1) throw GlbFormatException("expected exactly one primitive, found ${prims.length()}")
    val prim = prims.getJSONObject(0)
    if (prim.optInt("mode", 4) != 4) throw GlbFormatException("only triangle primitives are supported")
    val attrs = prim.getJSONObject("attributes")

    val accessors = gltf.getJSONArray("accessors")
    val views = gltf.getJSONArray("bufferViews")
    var positions = readFloats(accessors, views, binary, attrs.optInt("POSITION", -1), "VEC3", "POSITION")
      ?: throw GlbFormatException("POSITION is required")
    var normals = readFloats(accessors, views, binary, attrs.optInt("NORMAL", -1), "VEC3", "NORMAL")
    val uvs = readFloats(accessors, views, binary, attrs.optInt("TEXCOORD_0", -1), "VEC2", "TEXCOORD_0")
    val vertexCount = positions.size / 3
    if (normals != null && normals.size / 3 != vertexCount) throw GlbFormatException("NORMAL count mismatch")
    if (uvs != null && uvs.size / 2 != vertexCount) throw GlbFormatException("TEXCOORD_0 count mismatch")

    val indices = if (prim.has("indices")) readIndices(accessors, views, binary, prim.getInt("indices")) else IntArray(vertexCount) { it }
    if (indices.size % 3 != 0) throw GlbFormatException("index count is not a multiple of 3")
    if (indices.any { it < 0 || it >= vertexCount }) throw GlbFormatException("index out of range")

    // Bake the (single) node transform, if any.
    nodeMatrix(gltf)?.let { m ->
      positions = transform(m, positions, 1f)
      normals = normals?.let { n -> normalizeAll(transform(normalMatrix(m), n, 0f)) }
    }

    var image: ByteArray? = null
    var mime: String? = null
    var factor = floatArrayOf(1f, 1f, 1f, 1f)
    val materialIndex = prim.optInt("material", -1)
    if (materialIndex >= 0) {
      val pbr = gltf.getJSONArray("materials").getJSONObject(materialIndex).optJSONObject("pbrMetallicRoughness")
      pbr?.optJSONArray("baseColorFactor")?.let { f -> factor = FloatArray(4) { f.getDouble(it).toFloat() } }
      val texIndex = pbr?.optJSONObject("baseColorTexture")?.optInt("index", -1) ?: -1
      if (texIndex >= 0) {
        val source = gltf.getJSONArray("textures").getJSONObject(texIndex).optInt("source", -1)
        if (source < 0) throw GlbFormatException("texture has no image source")
        val img = gltf.getJSONArray("images").getJSONObject(source)
        if (!img.has("bufferView")) throw GlbFormatException("only images embedded in the GLB are supported")
        val view = views.getJSONObject(img.getInt("bufferView"))
        image = slice(binary, view.optInt("byteOffset", 0), view.getInt("byteLength"))
        mime = img.optString("mimeType", "")
        if (mime != "image/png" && mime != "image/jpeg") throw GlbFormatException("unsupported image type '$mime'")
      }
    }
    return MeshData(positions, normals, uvs, indices, image, mime, factor)
  }

  private fun readFloats(accessors: JSONArray, views: JSONArray, bin: ByteBuffer, index: Int, type: String, name: String): FloatArray? {
    if (index < 0) return null
    val a = accessors.getJSONObject(index)
    if (a.getString("type") != type) throw GlbFormatException("$name must be $type")
    if (a.getInt("componentType") != 5126) throw GlbFormatException("$name must be float32")
    if (a.has("sparse")) throw GlbFormatException("sparse accessors are not supported")
    val comps = if (type == "VEC3") 3 else 2
    val count = a.getInt("count")
    val view = views.getJSONObject(a.getInt("bufferView"))
    val stride = view.optInt("byteStride", 0).let { if (it == 0) comps * 4 else it }
    val base = view.optInt("byteOffset", 0) + a.optInt("byteOffset", 0)
    val out = FloatArray(count * comps)
    for (i in 0 until count) for (k in 0 until comps) {
      val pos = base + i * stride + k * 4
      if (pos + 4 > bin.limit()) throw GlbFormatException("$name overruns the binary buffer")
      out[i * comps + k] = bin.getFloat(pos)
    }
    if (out.any { !it.isFinite() }) throw GlbFormatException("$name contains non-finite values")
    return out
  }

  private fun readIndices(accessors: JSONArray, views: JSONArray, bin: ByteBuffer, index: Int): IntArray {
    val a = accessors.getJSONObject(index)
    if (a.getString("type") != "SCALAR") throw GlbFormatException("indices must be SCALAR")
    val count = a.getInt("count")
    val view = views.getJSONObject(a.getInt("bufferView"))
    val base = view.optInt("byteOffset", 0) + a.optInt("byteOffset", 0)
    val size = when (a.getInt("componentType")) {
      5125 -> 4
      5123 -> 2
      5121 -> 1
      else -> throw GlbFormatException("unsupported index type ${a.getInt("componentType")}")
    }
    if (base + count * size > bin.limit()) throw GlbFormatException("indices overrun the binary buffer")
    return IntArray(count) { i ->
      val p = base + i * size
      when (size) {
        4 -> bin.getInt(p)
        2 -> bin.getShort(p).toInt() and 0xFFFF
        else -> bin.get(p).toInt() and 0xFF
      }
    }
  }

  private fun slice(bin: ByteBuffer, offset: Int, length: Int): ByteArray {
    if (offset + length > bin.limit()) throw GlbFormatException("image overruns the binary buffer")
    return ByteArray(length).also { for (i in 0 until length) it[i] = bin.get(offset + i) }
  }

  /** Column-major matrix of the first node referencing the mesh, or null for identity. */
  private fun nodeMatrix(gltf: JSONObject): FloatArray? {
    val nodes = gltf.optJSONArray("nodes") ?: return null
    for (i in 0 until nodes.length()) {
      val n = nodes.getJSONObject(i)
      if (n.optInt("mesh", -1) != 0) continue
      n.optJSONArray("matrix")?.let { m -> return FloatArray(16) { m.getDouble(it).toFloat() } }
      val t = n.optJSONArray("translation")
      val r = n.optJSONArray("rotation")
      val s = n.optJSONArray("scale")
      if (t == null && r == null && s == null) return null
      val tx = t?.getDouble(0)?.toFloat() ?: 0f
      val ty = t?.getDouble(1)?.toFloat() ?: 0f
      val tz = t?.getDouble(2)?.toFloat() ?: 0f
      val qx = r?.getDouble(0)?.toFloat() ?: 0f
      val qy = r?.getDouble(1)?.toFloat() ?: 0f
      val qz = r?.getDouble(2)?.toFloat() ?: 0f
      val qw = r?.getDouble(3)?.toFloat() ?: 1f
      val sx = s?.getDouble(0)?.toFloat() ?: 1f
      val sy = s?.getDouble(1)?.toFloat() ?: 1f
      val sz = s?.getDouble(2)?.toFloat() ?: 1f
      return floatArrayOf(
        (1 - 2 * (qy * qy + qz * qz)) * sx, (2 * (qx * qy + qz * qw)) * sx, (2 * (qx * qz - qy * qw)) * sx, 0f,
        (2 * (qx * qy - qz * qw)) * sy, (1 - 2 * (qx * qx + qz * qz)) * sy, (2 * (qy * qz + qx * qw)) * sy, 0f,
        (2 * (qx * qz + qy * qw)) * sz, (2 * (qy * qz - qx * qw)) * sz, (1 - 2 * (qx * qx + qy * qy)) * sz, 0f,
        tx, ty, tz, 1f,
      )
    }
    return null
  }

  private fun transform(m: FloatArray, v: FloatArray, w: Float): FloatArray {
    val out = FloatArray(v.size)
    for (i in 0 until v.size / 3) {
      val x = v[i * 3]
      val y = v[i * 3 + 1]
      val z = v[i * 3 + 2]
      out[i * 3] = m[0] * x + m[4] * y + m[8] * z + m[12] * w
      out[i * 3 + 1] = m[1] * x + m[5] * y + m[9] * z + m[13] * w
      out[i * 3 + 2] = m[2] * x + m[6] * y + m[10] * z + m[14] * w
    }
    return out
  }

  /** Inverse-transpose of the upper 3x3, as a column-major 4x4. */
  private fun normalMatrix(m: FloatArray): FloatArray {
    val a = m[0]; val b = m[4]; val c = m[8]
    val d = m[1]; val e = m[5]; val f = m[9]
    val g = m[2]; val h = m[6]; val i = m[10]
    val det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if (kotlin.math.abs(det) < 1e-12f) throw GlbFormatException("degenerate node transform")
    val inv = 1f / det
    // (M^-1)^T, written directly in column-major order.
    return floatArrayOf(
      (e * i - f * h) * inv, (c * h - b * i) * inv, (b * f - c * e) * inv, 0f,
      (f * g - d * i) * inv, (a * i - c * g) * inv, (c * d - a * f) * inv, 0f,
      (d * h - e * g) * inv, (b * g - a * h) * inv, (a * e - b * d) * inv, 0f,
      0f, 0f, 0f, 1f,
    )
  }

  private fun normalizeAll(n: FloatArray): FloatArray {
    for (i in 0 until n.size / 3) {
      val l = kotlin.math.sqrt(n[i * 3] * n[i * 3] + n[i * 3 + 1] * n[i * 3 + 1] + n[i * 3 + 2] * n[i * 3 + 2])
      if (l > 1e-12f) for (k in 0..2) n[i * 3 + k] /= l
    }
    return n
  }
}
