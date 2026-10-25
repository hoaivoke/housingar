package com.housingar.inspect.capture

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.media.Image
import android.util.Log
import com.google.ar.core.Anchor
import com.google.ar.core.Camera
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Frame
import com.google.ar.core.exceptions.NotYetAvailableException
import com.housingar.inspect.CameraImages
import com.housingar.inspect.geometry.SurfaceGeometry
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteOrder
import kotlin.math.abs

/** One keyframe read on the GL thread, ready to be written by a worker thread. */
class GrabbedKeyframe(
  val timestampNs: Long,
  val nv21: ByteArray,
  val width: Int,
  val height: Int,
  val imageK: CaptureFormat.Intrinsics,
  val cameraToWorld: FloatArray,
  val cameraToAnchor: FloatArray,
  val crop: CaptureFormat.DepthCrop,
  val depthMm: ShortArray,
  val confidence: ByteArray?,
  val depthTimestampNs: Long,
  val sharpness: Float,
  /** Disagreement between texture-derived and image-derived depth intrinsics (diagnostic). */
  val intrinsicsDisagreement: Float,
) {
  fun toFrame(index: Int, name: String, orbitDeg: Float) = CaptureFormat.Frame(
    index = index, timestampNs = timestampNs, image = "frames/$name.jpg", width = width, height = height,
    intrinsics = imageK, cameraToAnchor = cameraToAnchor, cameraToWorld = cameraToWorld,
    depth = CaptureFormat.Depth("depth/$name.bin", crop.width, crop.height, depthTimestampNs, crop.intrinsics, crop.toImage),
    confidenceFile = confidence?.let { "confidence/$name.bin" }, sharpness = sharpness, orbitDeg = orbitDeg,
  )

  /** Worker thread: JPEG + depth + confidence files under [dir] (frames/, depth/, confidence/). */
  fun write(dir: File, name: String, jpegQuality: Int) {
    val jpeg = ByteArrayOutputStream()
    YuvImage(nv21, ImageFormat.NV21, width, height, null).compressToJpeg(Rect(0, 0, width, height), jpegQuality, jpeg)
    File(dir, "frames").mkdirs()
    File(dir, "depth").mkdirs()
    File(dir, "frames/$name.jpg").writeBytes(jpeg.toByteArray())
    File(dir, "depth/$name.bin").writeBytes(CaptureFormat.depthBytes(depthMm))
    if (confidence != null) {
      File(dir, "confidence").mkdirs()
      File(dir, "confidence/$name.bin").writeBytes(confidence)
    }
  }
}

/**
 * Reads a capture_v1 keyframe from the current ARCore frame: the newest depth observation (raw
 * depth + confidence when supported, else smoothed), the CPU image, intrinsics, and poses relative
 * to [anchor]. Depth is cropped to the image and its intrinsics derived through the mapping, as
 * docs/capture_v1.md requires. GL thread only.
 */
object KeyframeGrabber {
  sealed class Result {
    class Ok(val keyframe: GrabbedKeyframe) : Result()
    /** Not a keyframe this time; [guidance] (if any) says why. */
    class Skip(val guidance: String?) : Result()
  }

  fun grab(frame: Frame, camera: Camera, anchor: Anchor, rawDepthSupported: Boolean, lastDepthTimestampNs: Long): Result {
    var depthMm: ShortArray? = null
    var depthW = 0
    var depthH = 0
    var depthTs = 0L
    var conf: ByteArray? = null
    try {
      val depthImage = if (rawDepthSupported) frame.acquireRawDepthImage16Bits() else frame.acquireDepthImage16Bits()
      depthImage.use { img ->
        if (img.timestamp == lastDepthTimestampNs) return Result.Skip(null) // wait for a new depth observation
        depthTs = img.timestamp
        depthW = img.width
        depthH = img.height
        depthMm = copyDepth(img)
      }
      if (rawDepthSupported) {
        frame.acquireRawDepthConfidenceImage().use { c -> if (c.width == depthW && c.height == depthH) conf = copyBytes(c) }
      }
    } catch (_: NotYetAvailableException) {
      return Result.Skip("Waiting for depth: keep moving slowly.")
    } catch (e: Exception) {
      Log.w(TAG, "depth unavailable", e)
      return Result.Skip(null)
    }

    val image = try {
      frame.acquireCameraImage()
    } catch (e: Exception) {
      return Result.Skip(null)
    }
    val nv21: ByteArray
    val width: Int
    val height: Int
    image.use {
      width = it.width
      height = it.height
      nv21 = CameraImages.yuv420ToNv21(it)
    }

    val corners = FloatArray(8)
    frame.transformCoordinates2d(Coordinates2d.TEXTURE_NORMALIZED, floatArrayOf(0f, 0f, 0f, 1f, 1f, 0f, 1f, 1f), Coordinates2d.IMAGE_PIXELS, corners)
    if (abs(corners[2] - corners[0]) > 2f || abs(corners[5] - corners[1]) > 2f) {
      return Result.Skip("Depth alignment not supported on this camera configuration.")
    }
    val ii = camera.imageIntrinsics
    val imageK = CaptureFormat.Intrinsics(ii.focalLength[0], ii.focalLength[1], ii.principalPoint[0], ii.principalPoint[1])
    val crop = CaptureFormat.cropDepthToImage(depthW, depthH, floatArrayOf(corners[0], corners[1], corners[6], corners[7]), width, height, imageK)
      ?: return Result.Skip("Depth does not overlap the camera image on this camera configuration.")

    val ti = camera.textureIntrinsics
    val k = SurfaceGeometry.Intrinsics(ti.focalLength[0], ti.focalLength[1], ti.principalPoint[0], ti.principalPoint[1],
      ti.imageDimensions[0], ti.imageDimensions[1]).scaledTo(depthW, depthH)
    val disagreement = maxOf(abs(k.fx - crop.intrinsics.fx) / crop.intrinsics.fx, abs((k.cx - crop.i0) - crop.intrinsics.cx) / crop.intrinsics.fx)

    val cameraToWorld = FloatArray(16).also { camera.pose.toMatrix(it, 0) }
    val worldToAnchor = FloatArray(16).also { anchor.pose.inverse().toMatrix(it, 0) }
    val mm = depthMm ?: return Result.Skip(null)
    return Result.Ok(GrabbedKeyframe(
      timestampNs = frame.timestamp, nv21 = nv21, width = width, height = height, imageK = imageK,
      cameraToWorld = cameraToWorld, cameraToAnchor = ObjectCaptureSession.multiply(worldToAnchor, cameraToWorld),
      crop = crop, depthMm = crop.crop(mm, depthW), confidence = conf?.let { crop.crop(it, depthW) },
      depthTimestampNs = depthTs, sharpness = KeyframePolicy.laplacianVariance(nv21, width, height),
      intrinsicsDisagreement = disagreement,
    ))
  }

  fun copyDepth(img: Image): ShortArray {
    val plane = img.planes[0]
    val buf = plane.buffer.order(ByteOrder.nativeOrder())
    val out = ShortArray(img.width * img.height)
    for (y in 0 until img.height) for (x in 0 until img.width) out[y * img.width + x] = buf.getShort(y * plane.rowStride + x * plane.pixelStride)
    return out
  }

  fun copyBytes(img: Image): ByteArray {
    val plane = img.planes[0]
    val buf = plane.buffer
    val out = ByteArray(img.width * img.height)
    for (y in 0 until img.height) for (x in 0 until img.width) out[y * img.width + x] = buf.get(y * plane.rowStride + x * plane.pixelStride)
    return out
  }

  private const val TAG = "KeyframeGrabber"
}
