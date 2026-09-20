// Spec §0.3 and §15.1: all private reads still pass through the AWS reader's
// account and chapter gates. Only the public application bundle is cached here.
const securityHeaders = {
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
};

function privateResponse(body, init = {}) {
  const response = new Response(body, init);
  response.headers.set("Cache-Control", "private, no-store");
  response.headers.set("CDN-Cache-Control", "no-store");
  response.headers.set("Cloudflare-CDN-Cache-Control", "no-store");
  for (const [name, value] of Object.entries(securityHeaders)) {
    response.headers.set(name, value);
  }
  return response;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = new URL(env.PUBLIC_ORIGIN);
    if (url.host !== origin.host) {
      return privateResponse("Unknown host", { status: 421 });
    }
    if (url.protocol !== "https:") {
      url.protocol = "https:";
      return privateResponse(null, { status: 308, headers: { Location: url.href } });
    }
    if (url.pathname !== "/api" && !url.pathname.startsWith("/api/")) {
      return env.ASSETS.fetch(request);
    }

    const headers = new Headers(request.headers);
    // §15.1: identity comes only from the origin's authenticated session.
    headers.delete("X-Account-ID");
    headers.delete("X-Reader-ID");
    const upstream = new Request(request, {
      headers,
      cache: "no-store",
      // Return OAuth redirects to the browser; never forward its cookies to Google.
      redirect: "manual",
    });
    try {
      // A Worker Route's same-URL fetch goes to the DNS origin (AWS nginx), which
      // strips /api and proxies to reader-api. Cookies and Origin remain intact.
      const response = await fetch(upstream);
      return privateResponse(response.body, response);
    } catch {
      // Never echo request URLs, credentials, private text or upstream exceptions.
      return privateResponse(JSON.stringify({ error: "reader temporarily unavailable" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
