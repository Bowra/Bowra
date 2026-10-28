/** Copy key for a role in the invitation flow: space roles are admin, viewer and member; anything else is treated as member. */
export function roleKey(
  role?: string | null,
): "roles.admin" | "roles.viewer" | "roles.member" {
  if (role === "admin") return "roles.admin";
  if (role === "viewer") return "roles.viewer";
  return "roles.member";
}
