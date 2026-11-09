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

/**
 * The browser's IP address from `X-Forwarded-For`, or null when it cannot be trusted.
 *
 * `trustedHops` is the number of reverse proxies in front of the web server that append to the header
 * (`PROBEATLAS_WEB_TRUSTED_PROXY_HOPS`). Each proxy appends the address it received the request from, so the
 * client is the entry `trustedHops` positions from the right; anything further left was supplied by the
 * client and may be forged. With 0 hops Next.js keeps a client-supplied header, so nothing is trusted.
 */
export function clientIpFromForwardedFor(forwardedFor: string | null, trustedHops: number): string | null {
  if (!forwardedFor || !Number.isInteger(trustedHops) || trustedHops < 1) return null;
  const hops = forwardedFor.split(",").map((s) => s.trim()).filter(Boolean);
  const ip = hops[hops.length - trustedHops];
  return ip && /^[0-9A-Fa-f:.]{2,45}$/.test(ip) ? ip : null;
}
