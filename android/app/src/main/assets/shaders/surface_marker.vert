#version 300 es
// Surface marker: a flat quad in the anchor's local XZ plane (local +Y is the surface normal).
uniform mat4 u_ModelViewProjection;

layout(location = 0) in vec3 a_Position;
layout(location = 1) in vec2 a_TexCoord;

out vec2 v_Uv;

void main() {
  v_Uv = a_TexCoord;
  gl_Position = u_ModelViewProjection * vec4(a_Position, 1.0);
}
