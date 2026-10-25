#version 300 es
// Captured mesh (object or background patch). Unlit: the texture already contains the room's light.
uniform mat4 u_ModelViewProjection;
layout(location = 0) in vec3 a_Position;
layout(location = 1) in vec2 a_TexCoord;
out vec2 v_TexCoord;

void main() {
  v_TexCoord = a_TexCoord;
  gl_Position = u_ModelViewProjection * vec4(a_Position, 1.0);
}
