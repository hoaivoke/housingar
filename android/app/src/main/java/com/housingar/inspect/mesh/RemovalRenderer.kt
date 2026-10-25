package com.housingar.inspect.mesh

import android.opengl.GLES30
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

// Not used since the 24 Oct 2026 change of direction (AR removal/inpainting dropped in favour of
// scan-and-place). Kept for reference; nothing constructs it.
/**
 * 3D removal (docs/capture_v1.md, "Phone rendering integration"): draws the background patch over
 * the live camera image, letting real depth occlude it everywhere except where that depth belongs
 * to the removed object. The removal region is the object's 3D box (plus a margin), rendered into
 * an offscreen depth buffer each frame from the current camera pose.
 */
class RemovalRenderer(render: SampleRender) {
  private val regionFramebuffer = Framebuffer(render, 1, 1)
  private val cubeBuffers = mutableListOf<AutoCloseable>()
  private val cube: Mesh
  private val regionShader = Shader.createFromAssets(render, "shaders/surface_patch.vert", "shaders/surface_patch.frag", null)
    .setDepthTest(true).setDepthWrite(true).setCullFace(false)
    .setBlend(Shader.BlendFactor.ZERO, Shader.BlendFactor.ONE) // depth only
    .setVec4("u_Color", floatArrayOf(0f, 0f, 0f, 0f))
  private val patchShader = Shader.createFromAssets(render, "shaders/removal_patch.vert", "shaders/removal_patch.frag", null)
    .setDepthTest(true).setDepthWrite(true).setCullFace(false)
    .setBlend(Shader.BlendFactor.ONE, Shader.BlendFactor.ZERO)
  private val mvp = FloatArray(16)
  private val mv = FloatArray(16)
  private val regionModel = FloatArray(16)
  private var viewportWidth = 1
  private var viewportHeight = 1

  init {
    // Unit cube centred on the origin.
    val v = floatArrayOf(
      -0.5f, -0.5f, -0.5f, 0.5f, -0.5f, -0.5f, 0.5f, 0.5f, -0.5f, -0.5f, 0.5f, -0.5f,
      -0.5f, -0.5f, 0.5f, 0.5f, -0.5f, 0.5f, 0.5f, 0.5f, 0.5f, -0.5f, 0.5f, 0.5f,
    )
    val i = intArrayOf(0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 0, 4, 5, 0, 5, 1, 3, 2, 6, 3, 6, 7, 0, 3, 7, 0, 7, 4, 1, 5, 6, 1, 6, 2)
    val vb = VertexBuffer(render, 3, ByteBuffer.allocateDirect(v.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply { put(v); rewind() })
    val ib = IndexBuffer(render, ByteBuffer.allocateDirect(i.size * 4).order(ByteOrder.nativeOrder()).asIntBuffer().apply { put(i); rewind() })
    cube = Mesh(render, Mesh.PrimitiveMode.TRIANGLES, ib, arrayOf(vb))
    cubeBuffers.addAll(listOf(vb, ib, cube))
  }

  fun resize(width: Int, height: Int) {
    viewportWidth = width
    viewportHeight = height
    regionFramebuffer.resize(width, height)
  }

  /**
   * @param regionToWorld model matrix of the removal box's frame (anchor × object_to_anchor, or the anchor)
   * @param boxMin/boxMax box corners in that frame, already including any margin
   * @param depthUv screen NDC → camera-depth UV mapping as (uv0, uvX, uvY), or null when no depth image is available
   */
  fun draw(
    render: SampleRender,
    patch: TexturedMesh,
    anchorModel: FloatArray,
    regionToWorld: FloatArray,
    boxMin: FloatArray,
    boxMax: FloatArray,
    view: FloatArray,
    projection: FloatArray,
    cameraDepth: Texture,
    depthUv: FloatArray?,
    zNear: Float,
    zFar: Float,
  ) {
    // 1. Removal region depth (front-most surface of the object's box) from the current viewpoint.
    render.clear(regionFramebuffer, 0f, 0f, 0f, 0f)
    if (depthUv != null) {
      Matrix.setIdentityM(regionModel, 0)
      Matrix.translateM(regionModel, 0, (boxMin[0] + boxMax[0]) / 2f, (boxMin[1] + boxMax[1]) / 2f, (boxMin[2] + boxMax[2]) / 2f)
      Matrix.scaleM(regionModel, 0, boxMax[0] - boxMin[0], boxMax[1] - boxMin[1], boxMax[2] - boxMin[2])
      Matrix.multiplyMM(mv, 0, regionToWorld, 0, regionModel, 0)
      Matrix.multiplyMM(mvp, 0, view, 0, mv, 0)
      Matrix.multiplyMM(mv, 0, projection, 0, mvp, 0)
      regionShader.setMat4("u_ModelViewProjection", mv)
      render.draw(cube, regionShader, regionFramebuffer)
    }

    // 2. The patch, straight onto the screen after the camera background.
    GLES30.glBindFramebuffer(GLES30.GL_FRAMEBUFFER, 0)
    GLES30.glViewport(0, 0, viewportWidth, viewportHeight)
    GLES30.glDepthMask(true)
    GLES30.glClear(GLES30.GL_DEPTH_BUFFER_BIT)
    Matrix.multiplyMM(mv, 0, view, 0, anchorModel, 0)
    Matrix.multiplyMM(mvp, 0, projection, 0, mv, 0)
    patchShader
      .setMat4("u_ModelViewProjection", mvp)
      .setMat4("u_ModelView", mv)
      .setTexture("u_Texture", patch.texture)
      .setVec4("u_ColorFactor", patch.colorFactor)
      .setTexture("u_CameraDepthTexture", cameraDepth)
      .setTexture("u_RegionDepthTexture", regionFramebuffer.depthTexture)
      .setVec2("u_Viewport", floatArrayOf(viewportWidth.toFloat(), viewportHeight.toFloat()))
      .setFloat("u_UseCameraDepth", if (depthUv != null) 1f else 0f)
      .setFloat("u_ZNear", zNear)
      .setFloat("u_ZFar", zFar)
    val uv = depthUv ?: FloatArray(6)
    patchShader.setVec2("u_Uv0", floatArrayOf(uv[0], uv[1])).setVec2("u_UvX", floatArrayOf(uv[2], uv[3])).setVec2("u_UvY", floatArrayOf(uv[4], uv[5]))
    render.draw(patch.mesh, patchShader)
  }

  fun close() {
    cubeBuffers.asReversed().forEach { it.close() }
    regionFramebuffer.close()
  }

  companion object {
    /** Margin around the object's box: covers a slightly undersized reconstruction. */
    const val REGION_MARGIN_M = 0.04f

    /**
     * Screen NDC → texture-UV affine mapping from three corners (−1,−1), (1,−1), (−1,1) given in
     * TEXTURE_NORMALIZED coordinates (Frame.transformCoordinates2d). Returns uv0, uvX, uvY.
     */
    fun ndcToUv(corners: FloatArray): FloatArray = floatArrayOf(
      corners[0], corners[1],
      corners[2] - corners[0], corners[3] - corners[1],
      corners[4] - corners[0], corners[5] - corners[1],
    )
  }
}
