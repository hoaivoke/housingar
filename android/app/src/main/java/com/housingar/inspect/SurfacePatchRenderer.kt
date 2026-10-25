package com.housingar.inspect

import android.opengl.Matrix
import com.google.ar.core.examples.java.common.samplerender.Framebuffer
import com.google.ar.core.examples.java.common.samplerender.IndexBuffer
import com.google.ar.core.examples.java.common.samplerender.Mesh
import com.google.ar.core.examples.java.common.samplerender.SampleRender
import com.google.ar.core.examples.java.common.samplerender.Shader
import com.google.ar.core.examples.java.common.samplerender.VertexBuffer
import com.housingar.inspect.geometry.SurfaceGeometry
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * GPU meshes for one selected surface: a translucent fill and a solid outline, both in the
 * anchor's local coordinates. Must be created, drawn and closed on the GL thread.
 */
class SurfacePatchMeshes(render: SampleRender, surface: SurfaceGeometry.Surface) {
  private val buffers = mutableListOf<AutoCloseable>()
  val fill: Mesh
  val outline: Mesh

  init {
    // Lift 3 mm along the normal so the patch never z-fights with other virtual content.
    val n = surface.normalAnchor
    val lifted = surface.vertices.copyOf()
    for (i in 0 until lifted.size / 3) for (k in 0..2) lifted[i * 3 + k] += n[k] * 0.003f

    val positions = VertexBuffer(render, 3, floatBuffer(lifted)).also { buffers.add(it) }
    val fillIndices = IndexBuffer(render, intBuffer(surface.triangles)).also { buffers.add(it) }
    fill = Mesh(render, Mesh.PrimitiveMode.TRIANGLES, fillIndices, arrayOf(positions)).also { buffers.add(it) }
    val outlinePositions = VertexBuffer(render, 3, floatBuffer(lifted)).also { buffers.add(it) }
    outline = Mesh(render, Mesh.PrimitiveMode.LINE_LOOP, null, arrayOf(outlinePositions)).also { buffers.add(it) }
  }

  fun close() {
    buffers.asReversed().forEach { it.close() }
    buffers.clear()
  }

  private fun floatBuffer(v: FloatArray) =
    ByteBuffer.allocateDirect(v.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply { put(v); rewind() }

  private fun intBuffer(v: IntArray) =
    ByteBuffer.allocateDirect(v.size * 4).order(ByteOrder.nativeOrder()).asIntBuffer().apply { put(v); rewind() }
}

class SurfacePatchRenderer(render: SampleRender) {
  private val fillShader = Shader.createFromAssets(render, "shaders/surface_patch.vert", "shaders/surface_patch.frag", null)
    .setBlend(Shader.BlendFactor.ONE, Shader.BlendFactor.ONE_MINUS_SRC_ALPHA)
    .setDepthTest(true)
    .setDepthWrite(true)
    .setCullFace(false)
  private val outlineShader = Shader.createFromAssets(render, "shaders/surface_patch.vert", "shaders/surface_patch.frag", null)
    .setBlend(Shader.BlendFactor.ONE, Shader.BlendFactor.ONE_MINUS_SRC_ALPHA)
    .setDepthTest(true)
    .setDepthWrite(false)
    .setCullFace(false)
  private val mvp = FloatArray(16)

  fun draw(render: SampleRender, framebuffer: Framebuffer, meshes: SurfacePatchMeshes, anchorModel: FloatArray, viewProjection: FloatArray, color: FloatArray) {
    Matrix.multiplyMM(mvp, 0, viewProjection, 0, anchorModel, 0)
    fillShader.setMat4("u_ModelViewProjection", mvp).setVec4("u_Color", color)
    render.draw(meshes.fill, fillShader, framebuffer)
    outlineShader.setMat4("u_ModelViewProjection", mvp).setVec4("u_Color", floatArrayOf(color[0], color[1], color[2], 0.95f))
    render.draw(meshes.outline, outlineShader, framebuffer)
  }
}
