#version 300 es
// Draws the reconstructed background over the live camera image to hide the selected object.
// Real depth that belongs to the selected object (inside its removal region) must NOT occlude the
// patch; real depth in front of that region (a person, a chair) and elsewhere still occludes it.
precision highp float;

uniform sampler2D u_Texture;
uniform vec4 u_ColorFactor;
uniform sampler2D u_CameraDepthTexture;   // ARCore depth, RG8-packed millimetres
uniform sampler2D u_RegionDepthTexture;   // depth buffer of the removal-region box; 1.0 = none
uniform vec2 u_Viewport;
// Screen NDC -> camera-depth texture UV: uv = u_Uv0 + u_UvX * (ndc.x + 1) / 2 + u_UvY * (ndc.y + 1) / 2
uniform vec2 u_Uv0;
uniform vec2 u_UvX;
uniform vec2 u_UvY;
uniform float u_UseCameraDepth;
uniform float u_ZNear;
uniform float u_ZFar;

in vec2 v_TexCoord;
in float v_ViewDepthM;
layout(location = 0) out vec4 o_FragColor;

float linearDepthMm(float d) {
  float ndc = 2.0 * d - 1.0;
  return 2.0 * u_ZNear * u_ZFar / (u_ZFar + u_ZNear - ndc * (u_ZFar - u_ZNear)) * 1000.0;
}

void main() {
  if (u_UseCameraDepth > 0.5) {
    vec2 screen = gl_FragCoord.xy / u_Viewport;
    vec2 ndc = screen * 2.0 - 1.0;
    vec2 depthUv = u_Uv0 + u_UvX * (ndc.x + 1.0) * 0.5 + u_UvY * (ndc.y + 1.0) * 0.5;
    float realMm = dot(texture(u_CameraDepthTexture, depthUv).xy, vec2(255.0, 256.0 * 255.0));
    if (realMm > 0.0) {
      float patchMm = v_ViewDepthM * 1000.0;
      float region = texture(u_RegionDepthTexture, screen).x;
      if (region < 0.9999) {
        float regionMm = linearDepthMm(region);
        // Over the removal region: ignore the removed object, keep anything clearly in front of it.
        if (realMm < regionMm - max(40.0, 0.03 * regionMm)) discard;
      } else {
        // Ordinary occlusion against the patch surface (which approximates the real background).
        if (realMm < patchMm - max(40.0, 0.03 * patchMm)) discard;
      }
    }
  }
  vec4 c = texture(u_Texture, v_TexCoord) * u_ColorFactor;
  o_FragColor = vec4(c.rgb, 1.0);
}
