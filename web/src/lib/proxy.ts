/**
 * Build the control-plane path for proxied catch-all segments, or return null if it is not allowed.
 *
 * Next decodes each segment after splitting on `/`, so `..%2Fmetrics` arrives as the single segment
 * `../metrics`. Only `/v1/...` paths made of plain URL-safe segments are allowed, so a request can never
 * be normalized into another API route such as `/metrics`.
 */
export function proxiedApiPath(segments: string[]): string | null {
  if (segments[0] !== "v1") return null;
  const ok = segments.every((s) => /^[A-Za-z0-9_~-][A-Za-z0-9_.~-]*$/.test(s) && s !== "." && s !== "..");
  return ok ? `/${segments.join("/")}` : null;
}
