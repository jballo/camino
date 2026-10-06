/**
 * Returns `value` only when it is a same-origin path, else `fallback`.
 * Rejects protocol-relative (`//host`) and backslash (`/\host`) forms, which
 * browsers resolve to another origin, and anything with control characters.
 */
export function safeRedirectPath(
  value: string | string[] | undefined | null,
  fallback: string,
): string {
  const raw = Array.isArray(value) ? value[0] : value;
  if (!raw || !raw.startsWith("/")) return fallback;
  if (raw.startsWith("//") || raw.startsWith("/\\")) return fallback;
  if (/[\u0000-\u001f\u007f]/.test(raw)) return fallback;
  return raw;
}
