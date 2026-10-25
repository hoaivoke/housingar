package com.housingar.inspect

import android.opengl.Matrix
import com.google.ar.core.examples.java.common.samplerender.Framebuffer
import com.google.ar.core.examples.java.common.samplerender.IndexBuffer
import com.google.ar.core.examples.java.common.samplerender.Mesh
import com.google.ar.core.examples.java.common.samplerender.SampleRender
import com.google.ar.core.examples.java.common.samplerender.Shader
import com.google.ar.core.examples.java.common.samplerender.VertexBuffer
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Draws a translucent square highlight in an anchor's local XZ plane. ARCore plane and depth hit
 * poses have local +Y along the surface normal, so the square lies flat on floors, tabletops and
 * walls alike. Rendered every display frame from the current camera pose; nothing here depends on
 * model inference.
 */
class SurfaceMarkerRenderer(render: SampleRender, sizeMeters: Float) {
  private val mesh: Mesh
  private val shader: Shader
  private val modelViewProjection = FloatArray(16)

  init {
    val h = sizeMeters / 2f
    // A ~2 mm lift along the normal avoids z-fighting with any virtual geometry at the surface.
    val lift = 0.002f
    val positions = floatArrayOf(
      -h, lift, -h,
      -h, lift, h,
      h, lift, h,
      h, lift, -h,
    )
    val uvs = floatArrayOf(
      0f, 0f,
      0f, 1f,
      1f, 1f,
      1f, 0f,
    )
    val indices = intArrayOf(0, 1, 2, 0, 2, 3)

    val positionBuffer = VertexBuffer(render, 3, floatBuffer(positions))
    val uvBuffer = VertexBuffer(render, 2, floatBuffer(uvs))
    val indexBuffer = IndexBuffer(render, intBuffer(indices))
    mesh = Mesh(render, Mesh.PrimitiveMode.TRIANGLES, indexBuffer, arrayOf(positionBuffer, uvBuffer))

    shader = Shader.createFromAssets(render, "shaders/surface_marker.vert", "shaders/surface_marker.frag", null)
      .setBlend(Shader.BlendFactor.ONE, Shader.BlendFactor.ONE_MINUS_SRC_ALPHA)
      .setDepthTest(true)
      .setDepthWrite(true)
      .setCullFace(false)
      .setFloat("u_Border", 0.035f)
      .setFloat("u_Line", 0.006f)
  }

  /** @param color rgb + fill alpha */
  fun draw(
    render: SampleRender,
    framebuffer: Framebuffer,
    modelMatrix: FloatArray,
    viewProjection: FloatArray,
    color: FloatArray,
  ) {
    Matrix.multiplyMM(modelViewProjection, 0, viewProjection, 0, modelMatrix, 0)
    shader.setMat4("u_ModelViewProjection", modelViewProjection).setVec4("u_Color", color)
    render.draw(mesh, shader, framebuffer)
  }

  private fun floatBuffer(values: FloatArray) =
    ByteBuffer.allocateDirect(values.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply {
      put(values)
      rewind()
    }

  private fun intBuffer(values: IntArray) =
    ByteBuffer.allocateDirect(values.size * 4).order(ByteOrder.nativeOrder()).asIntBuffer().apply {
      put(values)
      rewind()
    }
}
