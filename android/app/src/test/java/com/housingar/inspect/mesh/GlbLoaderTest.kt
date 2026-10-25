package com.housingar.inspect.mesh

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

class GlbLoaderTest {
  private val fakePng = byteArrayOf(0x89.toByte(), 'P'.code.toByte(), 'N'.code.toByte(), 'G'.code.toByte(), 1, 2, 3, 4)

  /** Builds a GLB with one triangle; optional interleaving, uint16 indices, node TRS and texture. */
  private fun glb(interleaved: Boolean = false, shortIndices: Boolean = false, node: JSONObject? = null, texture: Boolean = true, json: (JSONObject) -> Unit = {}): ByteArray {
    val pos = floatArrayOf(0f, 0f, 0f, 1f, 0f, 0f, 0f, 1f, 0f)
    val nrm = floatArrayOf(0f, 0f, 1f, 0f, 0f, 1f, 0f, 0f, 1f)
    val uv = floatArrayOf(0f, 0f, 1f, 0f, 0f, 1f)
    val bin = ByteArrayOutputStream()
    fun putFloats(v: FloatArray) = bin.write(ByteBuffer.allocate(v.size * 4).order(ByteOrder.LITTLE_ENDIAN).also { b -> v.forEach { b.putFloat(it) } }.array())
    val views = JSONArray()
    val accessors = JSONArray()
    if (interleaved) {
      // POSITION + NORMAL interleaved with 24-byte stride.
      val inter = FloatArray(18)
      for (i in 0..2) { for (k in 0..2) { inter[i * 6 + k] = pos[i * 3 + k]; inter[i * 6 + 3 + k] = nrm[i * 3 + k] } }
      putFloats(inter)
      views.put(JSONObject().put("buffer", 0).put("byteOffset", 0).put("byteLength", 72).put("byteStride", 24))
      accessors.put(JSONObject().put("bufferView", 0).put("componentType", 5126).put("count", 3).put("type", "VEC3"))
      accessors.put(JSONObject().put("bufferView", 0).put("byteOffset", 12).put("componentType", 5126).put("count", 3).put("type", "VEC3"))
    } else {
      putFloats(pos); putFloats(nrm)
      views.put(JSONObject().put("buffer", 0).put("byteOffset", 0).put("byteLength", 36))
      views.put(JSONObject().put("buffer", 0).put("byteOffset", 36).put("byteLength", 36))
      accessors.put(JSONObject().put("bufferView", 0).put("componentType", 5126).put("count", 3).put("type", "VEC3"))
      accessors.put(JSONObject().put("bufferView", 1).put("componentType", 5126).put("count", 3).put("type", "VEC3"))
    }
    val uvOffset = bin.size()
    putFloats(uv)
    views.put(JSONObject().put("buffer", 0).put("byteOffset", uvOffset).put("byteLength", 24))
    accessors.put(JSONObject().put("bufferView", views.length() - 1).put("componentType", 5126).put("count", 3).put("type", "VEC2"))
    val idxOffset = bin.size()
    if (shortIndices) {
      bin.write(ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putShort(0).putShort(1).putShort(2).putShort(0).array())
      views.put(JSONObject().put("buffer", 0).put("byteOffset", idxOffset).put("byteLength", 6))
      accessors.put(JSONObject().put("bufferView", views.length() - 1).put("componentType", 5123).put("count", 3).put("type", "SCALAR"))
    } else {
      bin.write(ByteBuffer.allocate(12).order(ByteOrder.LITTLE_ENDIAN).putInt(0).putInt(1).putInt(2).array())
      views.put(JSONObject().put("buffer", 0).put("byteOffset", idxOffset).put("byteLength", 12))
      accessors.put(JSONObject().put("bufferView", views.length() - 1).put("componentType", 5125).put("count", 3).put("type", "SCALAR"))
    }
    val imgOffset = bin.size()
    bin.write(fakePng)
    views.put(JSONObject().put("buffer", 0).put("byteOffset", imgOffset).put("byteLength", fakePng.size))
    while (bin.size() % 4 != 0) bin.write(0)
    val prim = JSONObject().put("attributes", JSONObject().put("POSITION", 0).put("NORMAL", 1).put("TEXCOORD_0", 2)).put("indices", 3)
    val gltf = JSONObject()
      .put("asset", JSONObject().put("version", "2.0"))
      .put("buffers", JSONArray().put(JSONObject().put("byteLength", bin.size())))
      .put("bufferViews", views).put("accessors", accessors)
      .put("meshes", JSONArray().put(JSONObject().put("primitives", JSONArray().put(prim))))
      .put("nodes", JSONArray().put(node ?: JSONObject().put("mesh", 0)))
    if (texture) {
      prim.put("material", 0)
      gltf.put("materials", JSONArray().put(JSONObject().put("pbrMetallicRoughness", JSONObject().put("baseColorTexture", JSONObject().put("index", 0)))))
        .put("textures", JSONArray().put(JSONObject().put("source", 0)))
        .put("images", JSONArray().put(JSONObject().put("bufferView", views.length() - 1).put("mimeType", "image/png")))
    }
    json(gltf)
    var jsonBytes = gltf.toString().toByteArray()
    while (jsonBytes.size % 4 != 0) jsonBytes += ' '.code.toByte()
    val binBytes = bin.toByteArray()
    val total = 12 + 8 + jsonBytes.size + 8 + binBytes.size
    return ByteBuffer.allocate(total).order(ByteOrder.LITTLE_ENDIAN)
      .putInt(0x46546C67).putInt(2).putInt(total)
      .putInt(jsonBytes.size).putInt(0x4E4F534A).put(jsonBytes)
      .putInt(binBytes.size).putInt(0x004E4942).put(binBytes).array()
  }

  @Test
  fun loadsPositionsNormalsUvsIndicesAndTexture() {
    val m = GlbLoader.load(glb())
    assertEquals(3, m.vertexCount)
    assertEquals(1, m.triangleCount)
    assertArrayEquals(floatArrayOf(0f, 0f, 0f, 1f, 0f, 0f, 0f, 1f, 0f), m.positions, 0f)
    assertArrayEquals(floatArrayOf(0f, 0f, 1f, 0f, 0f, 1f, 0f, 0f, 1f), m.normals, 0f)
    assertArrayEquals(floatArrayOf(0f, 0f, 1f, 0f, 0f, 1f), m.uvs, 0f)
    assertArrayEquals(intArrayOf(0, 1, 2), m.indices)
    assertArrayEquals(fakePng, m.baseColorImage)
    assertEquals("image/png", m.baseColorMime)
  }

  @Test
  fun handlesInterleavedBuffersAndShortIndices() {
    val m = GlbLoader.load(glb(interleaved = true, shortIndices = true))
    assertArrayEquals(floatArrayOf(0f, 0f, 0f, 1f, 0f, 0f, 0f, 1f, 0f), m.positions, 0f)
    assertArrayEquals(floatArrayOf(0f, 0f, 1f, 0f, 0f, 1f, 0f, 0f, 1f), m.normals, 0f)
    assertArrayEquals(intArrayOf(0, 1, 2), m.indices)
  }

  @Test
  fun bakesNodeTranslationRotationAndScale() {
    // 90° about +Y (x -> -z), scale 2, then translate (1, 2, 3).
    val s = Math.sqrt(0.5)
    val node = JSONObject().put("mesh", 0)
      .put("translation", JSONArray().put(1).put(2).put(3))
      .put("rotation", JSONArray().put(0).put(s).put(0).put(s))
      .put("scale", JSONArray().put(2).put(2).put(2))
    val m = GlbLoader.load(glb(node = node))
    // Vertex 1 = (1,0,0) -> scaled (2,0,0) -> rotated (0,0,-2) -> translated (1,2,1)
    assertArrayEquals(floatArrayOf(1f, 2f, 1f), m.positions.copyOfRange(3, 6), 1e-5f)
    // Normal (0,0,1) -> rotated (1,0,0), still unit length.
    assertArrayEquals(floatArrayOf(1f, 0f, 0f), m.normals!!.copyOfRange(0, 3), 1e-5f)
  }

  @Test
  fun untexturedMeshesLoadWithoutAnImage() {
    val m = GlbLoader.load(glb(texture = false))
    assertNull(m.baseColorImage)
    assertNotNull(m.uvs)
  }

  @Test(expected = GlbFormatException::class)
  fun compressionExtensionsAreRejected() {
    GlbLoader.load(glb { it.put("extensionsRequired", JSONArray().put("KHR_draco_mesh_compression")) })
  }

  @Test(expected = GlbFormatException::class)
  fun garbageIsRejected() {
    GlbLoader.load(ByteArray(64))
  }
}
