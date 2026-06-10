// Canonical-host middleware. Cloudflare Pages `_redirects` sources are
// path-based only, so the *.pages.dev origin cannot be redirected from that
// file; this middleware is the in-repo equivalent. It 301s the production
// alias onto the apex, preserving path and query, and deliberately matches
// ONLY the exact production hostname so hashed preview deployments
// (<hash>.horowitz-law.pages.dev) keep working for review.
//
// Tradeoff, stated plainly: a root middleware means every request on every
// host becomes a Functions invocation (previously only /api/* did). At this
// site's traffic that is negligible against the free tier's daily allowance,
// and the apex path is a single hostname comparison before next(); remove
// this file to return to pure static serving and rely on rel=canonical.
export async function onRequest(context) {
  const url = new URL(context.request.url);
  if (url.hostname === "horowitz-law.pages.dev") {
    url.hostname = "horowitz.law";
    return Response.redirect(url.toString(), 301);
  }
  return context.next();
}
