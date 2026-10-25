package com.housingar.inspect.mesh

import android.graphics.Bitmap
import android.opengl.GLES30
import android.opengl.GLUtils
import android.opengl.Matrix
import com.google.ar.core.examples.java.common.samplerender.Framebuffer
import com.google.ar.core.examples.java.common.samplerender.IndexBuffer
import com.google.ar.core.examples.java.common.samplerender.Mesh
import com.google.ar.core.examples.java.common.samplerender.SampleRender
import com.google.ar.core.examples.java.common.samplerender.Shader
import com.google.ar.core.examples.java.common.samplerender.Texture
import com.google.ar.core.examples.java.common.samplerender.VertexBuffer
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.cos
import kotlin.math.sin

/** GPU copy of a [MeshData] with its base-colour texture. Create, draw and close on the GL thread. */
class TexturedMesh(render: SampleRender, data: MeshData, bitmap: Bitmap?) {
  private val closeables = mutableListOf<AutoCloseable>()
  val mesh: Mesh
  val texture: Texture
  val colorFactor: FloatArray = data.baseColorFactor
  val triangleCount = data.triangleCount

  init {
    val pos = VertexBuffer(render, 3, floats(data.positions)).also { closeables.add(it) }
    val uv = VertexBuffer(render, 2, floats(data.uvs ?: FloatArray(data.vertexCount * 2))).also { closeables.add(it) }
    val idx = IndexBuffer(render, ints(data.indices)).also { closeables.add(it) }
    mesh = Mesh(render, Mesh.PrimitiveMode.TRIANGLES, idx, arrayOf(pos, uv)).also { closeables.add(it) }

    texture = Texture(render, Texture.Target.TEXTURE_2D, Texture.WrapMode.CLAMP_TO_EDGE, true).also { closeables.add(it) }
    GLES30.glBindTexture(GLES30.GL_TEXTURE_2D, texture.textureId)
    val bmp = bitmap ?: Bitmap.createBitmap(1, 1, Bitmap.Config.ARGB_8888).also { it.eraseColor(android.graphics.Color.WHITE) }
    GLUtils.texImage2D(GLES30.GL_TEXTURE_2D, 0, bmp, 0)
    GLES30.glGenerateMipmap(GLES30.GL_TEXTURE_2D)
    if (bitmap == null) bmp.recycle()
  }

  fun close() {
    closeables.asReversed().forEach { it.close() }
    closeables.clear()
  }

  private fun floats(v: FloatArray) = ByteBuffer.allocateDirect(v.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply { put(v); rewind() }
  private fun ints(v: IntArray) = ByteBuffer.allocateDirect(v.size * 4).order(ByteOrder.nativeOrder()).asIntBuffer().apply { put(v); rewind() }
}

class TexturedMeshRenderer(render: SampleRender) {
  /** For meshes composited with the rest of the virtual scene (depth-occludable). */
  private val sceneShader = Shader.createFromAssets(render, "shaders/textured_mesh.vert", "shaders/textured_mesh.frag", null)
    .setBlend(Shader.BlendFactor.ONE, Shader.BlendFactor.ONE_MINUS_SRC_ALPHA)
    .setDepthTest(true).setDepthWrite(true).setCullFace(false)

  private val mvp = FloatArray(16)

  fun drawInScene(render: SampleRender, fb: Framebuffer, m: TexturedMesh, model: FloatArray, viewProjection: FloatArray, opacity: Float, writeDepth: Boolean = true) {
    Matrix.multiplyMM(mvp, 0, viewProjection, 0, model, 0)
    sceneShader.setMat4("u_ModelViewProjection", mvp).setTexture("u_Texture", m.texture)
      .setVec4("u_ColorFactor", m.colorFactor).setFloat("u_Opacity", opacity).setDepthWrite(writeDepth)
    render.draw(m.mesh, sceneShader, fb)
  }

}

/**
 * Guide ring on the floor around the object: one segment per orbit sector, lit once that angle
 * has been captured. Sector i spans azimuth [i·w, (i+1)·w), measured from +Z towards +X.
 */
class OrbitRingRenderer(render: SampleRender, radius: Float, private val sectors: Int) {
  private val segment: Mesh
  private val buffers = mutableListOf<AutoCloseable>()
  private val shader = Shader.createFromAssets(render, "shaders/surface_patch.vert", "shaders/surface_patch.frag", null)
    .setBlend(Shader.BlendFactor.ONE, Shader.BlendFactor.ONE_MINUS_SRC_ALPHA)
    .setDepthTest(true).setDepthWrite(false).setCullFace(false)
  private val model = FloatArray(16)
  private val mvp = FloatArray(16)

  init {
    val width = 360f / sectors
    val steps = 6
    val inner = radius
    val outer = radius + 0.05f
    val verts = ArrayList<Float>()
    for (s in 0..steps) {
      val a = Math.toRadians((0.6f + (width - 1.2f) * s / steps).toDouble())
      verts.addAll(listOf((inner * sin(a)).toFloat(), 0f, (inner * cos(a)).toFloat()))
      verts.addAll(listOf((outer * sin(a)).toFloat(), 0f, (outer * cos(a)).toFloat()))
    }
    val v = verts.toFloatArray()
    val vb = VertexBuffer(render, 3, ByteBuffer.allocateDirect(v.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply { put(v); rewind() })
    buffers.add(vb)
    segment = Mesh(render, Mesh.PrimitiveMode.TRIANGLE_STRIP, null, arrayOf(vb)).also { buffers.add(it) }
  }

  fun draw(render: SampleRender, fb: Framebuffer, center: FloatArray, floorY: Float, covered: (Int) -> Boolean, viewProjection: FloatArray) {
    for (i in 0 until sectors) {
      Matrix.setIdentityM(model, 0)
      Matrix.translateM(model, 0, center[0], floorY + 0.005f, center[2])
      Matrix.rotateM(model, 0, i * 360f / sectors, 0f, 1f, 0f)
      Matrix.multiplyMM(mvp, 0, viewProjection, 0, model, 0)
      val color = if (covered(i)) floatArrayOf(0.30f, 0.85f, 0.45f, 0.85f) else floatArrayOf(1f, 1f, 1f, 0.35f)
      shader.setMat4("u_ModelViewProjection", mvp).setVec4("u_Color", color)
      render.draw(segment, shader, fb)
    }
  }

  fun close() {
    buffers.asReversed().forEach { it.close() }
    buffers.clear()
  }
}
