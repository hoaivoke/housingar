package com.housingar.inspect.geometry

/**
 * Maps points between a sensor-oriented image (w × h) and the same image rotated clockwise by
 * 0/90/180/270° for display (android.graphics.Matrix.postRotate + Bitmap.createBitmap).
 */
object ImageRotation {
  fun toRotated(x: Float, y: Float, w: Int, h: Int, degrees: Int): FloatArray = when (normalize(degrees)) {
    0 -> floatArrayOf(x, y)
    90 -> floatArrayOf(h - y, x)
    180 -> floatArrayOf(w - x, h - y)
    else -> floatArrayOf(y, w - x)
  }

  fun fromRotated(xr: Float, yr: Float, w: Int, h: Int, degrees: Int): FloatArray = when (normalize(degrees)) {
    0 -> floatArrayOf(xr, yr)
    90 -> floatArrayOf(yr, h - xr)
    180 -> floatArrayOf(w - xr, h - yr)
    else -> floatArrayOf(w - yr, xr)
  }

  private fun normalize(d: Int): Int {
    val n = ((d % 360) + 360) % 360
    require(n % 90 == 0) { "rotation must be a multiple of 90°, got $d" }
    return n
  }
}
