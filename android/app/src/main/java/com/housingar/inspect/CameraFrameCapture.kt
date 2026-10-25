package com.housingar.inspect

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ImageFormat
import android.graphics.Paint
import android.graphics.Rect
import android.graphics.YuvImage
import android.media.Image
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/**
 * One camera frame captured for the Mac service, together with the spatial data that belongs to
 * *that* frame (pose, intrinsics, timestamps). Later milestones must use this captured data, not
 * the latest camera pose, when turning the returned mask into geometry.
 */
class CapturedFrame(
  val frameId: String,
  val sessionId: String,
  val timestampNs: Long,
  val width: Int,
  val height: Int,
  val nv21: ByteArray,
  /** Tap location converted to CPU-image pixel coordinates. */
  val pointX: Float,
  val pointY: Float,
  /** fx, fy, cx, cy for the CPU image. */
  val intrinsics: FloatArray,
  /** Physical camera pose in world space: tx, ty, tz, qx, qy, qz, qw. */
  val cameraPose: FloatArray,
  val sensorToDisplayRotation: Int,
  val depthTimestampNs: Long?,
) {
  fun toJpeg(quality: Int = 90): ByteArray {
    val out = ByteArrayOutputStream()
    YuvImage(nv21, ImageFormat.NV21, width, height, null).compressToJpeg(Rect(0, 0, width, height), quality, out)
    return out.toByteArray()
  }

  fun metadata(): JSONObject = JSONObject()
    .put("session_id", sessionId)
    .put("timestamp_ns", timestampNs)
    .put("image_width", width)
    .put("image_height", height)
    .put("intrinsics", JSONObject()
      .put("fx", intrinsics[0].toDouble()).put("fy", intrinsics[1].toDouble())
      .put("cx", intrinsics[2].toDouble()).put("cy", intrinsics[3].toDouble()))
    .put("camera_pose_world", JSONArray(cameraPose.map { it.toDouble() }))
    .put("sensor_to_display_rotation_deg", sensorToDisplayRotation)
    // 0 = no depth image yet (the service requires a non-negative integer).
    .put("depth_timestamp_ns", depthTimestampNs ?: 0L)
}

object CameraImages {

  /** Converts a YUV_420_888 [Image] to NV21, honouring row and pixel strides. */
  fun yuv420ToNv21(image: Image): ByteArray {
    require(image.format == ImageFormat.YUV_420_888) { "Expected YUV_420_888, got ${image.format}" }
    val width = image.width
    val height = image.height
    val chromaW = width / 2
    val chromaH = height / 2
    val out = ByteArray(width * height + 2 * chromaW * chromaH)

    val yPlane = image.planes[0]
    val yBuf = yPlane.buffer
    val yRowStride = yPlane.rowStride
    val yPixelStride = yPlane.pixelStride
    var pos = 0
    if (yPixelStride == 1) {
      for (row in 0 until height) {
        yBuf.position(row * yRowStride)
        yBuf.get(out, pos, width)
        pos += width
      }
    } else {
      for (row in 0 until height) for (col in 0 until width) {
        out[pos++] = yBuf.get(row * yRowStride + col * yPixelStride)
      }
    }

    val uPlane = image.planes[1]
    val vPlane = image.planes[2]
    val uBuf = uPlane.buffer
    val vBuf = vPlane.buffer
    for (row in 0 until chromaH) {
      for (col in 0 until chromaW) {
        out[pos++] = vBuf.get(row * vPlane.rowStride + col * vPlane.pixelStride)
        out[pos++] = uBuf.get(row * uPlane.rowStride + col * uPlane.pixelStride)
      }
    }
    return out
  }

  /**
   * Display-oriented preview of a segmentation result: the camera image with the mask tinted on
   * top and the prompt points marked (green = include, red = exclude). 2D only; not anchored.
   */
  fun maskPreview(jpeg: ByteArray, maskPngBase64: String?, points: List<FloatArray>, rotationDeg: Int): Bitmap {
    val photo = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size).copy(Bitmap.Config.ARGB_8888, true)
    val w = photo.width
    val h = photo.height
    if (maskPngBase64 != null) {
      val mask = decodeMask(maskPngBase64, w, h)
      val px = IntArray(w * h)
      photo.getPixels(px, 0, w, 0, 0, w, h)
      for (i in px.indices) {
        if (mask[i]) {
          val p = px[i]
          px[i] = Color.rgb(
            (Color.red(p) * 0.45f + 0x35 * 0.55f).toInt(),
            (Color.green(p) * 0.45f + 0xC4 * 0.55f).toInt(),
            (Color.blue(p) * 0.45f + 0xD6 * 0.55f).toInt(),
          )
        }
      }
      photo.setPixels(px, 0, w, 0, 0, w, h)
    }
    val canvas = Canvas(photo)
    val r = maxOf(w, h) / 70f
    val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    val ring = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = r / 3f; color = Color.WHITE }
    for (p in points) {
      fill.color = if (p.size > 2 && p[2] < 0.5f) Color.rgb(0xE5, 0x39, 0x35) else Color.rgb(0x43, 0xA0, 0x47)
      canvas.drawCircle(p[0], p[1], r, fill)
      canvas.drawCircle(p[0], p[1], r, ring)
    }
    if (rotationDeg % 360 == 0) return photo
    val m = android.graphics.Matrix().apply { postRotate(rotationDeg.toFloat()) }
    return Bitmap.createBitmap(photo, 0, 0, w, h, m, true)
  }

  /** Decodes the service's grayscale mask PNG into a row-major boolean array of size w × h. */
  fun decodeMask(maskPngBase64: String, w: Int, h: Int): BooleanArray {
    val bytes = Base64.decode(maskPngBase64, Base64.DEFAULT)
    val decoded = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
      ?: throw IllegalArgumentException("mask PNG could not be decoded")
    require(decoded.width == w && decoded.height == h) { "mask is ${decoded.width}x${decoded.height}, image is ${w}x$h" }
    val px = IntArray(w * h)
    decoded.getPixels(px, 0, w, 0, 0, w, h)
    return BooleanArray(w * h) { Color.red(px[it]) > 127 }
  }
}
