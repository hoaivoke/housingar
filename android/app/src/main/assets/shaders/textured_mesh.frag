#version 300 es
precision mediump float;
uniform sampler2D u_Texture;
uniform vec4 u_ColorFactor; // glTF baseColorFactor
uniform float u_Opacity;
in vec2 v_TexCoord;
layout(location = 0) out vec4 o_FragColor;

void main() {
  // glTF UV origin is the top-left of the image; GLUtils uploads row 0 at t = 0, so no flip.
  vec4 c = texture(u_Texture, v_TexCoord) * u_ColorFactor;
  float a = c.a * u_Opacity;
  o_FragColor = vec4(c.rgb * a, a); // premultiplied
}
