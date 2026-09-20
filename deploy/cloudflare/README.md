# Cloudflare frontend, AWS backend

The React application is deployed as Cloudflare Workers Static Assets. Only `/api`
and `/api/*` invoke the Worker, which forwards to the existing AWS nginx/reader-api
origin. Spec §0.3 and §15.1 account isolation, chapter gates, Google login, cookies
and CSRF remain enforced by the existing backend. PostgreSQL, Redis, private objects,
background work and backups stay on AWS. Local Vite development is unchanged.

## Domain and TLS

- Domain: `qireadr.com`; registrar remains Namecheap.
- Cloudflare Free nameservers: `garrett.ns.cloudflare.com` and
  `mia.ns.cloudflare.com`.
- Apex A record: `3.221.223.180`. Keep this AWS address even after moving the frontend:
  the Worker Route's same-URL `fetch()` uses it to reach the API origin.
- Use SSL/TLS **Full (strict)** after verifying the origin certificate.
- The Worker Route is **`https://qireadr.com/*`**, not a Workers Custom Domain.
  HTTP stays at Traefik, which answers `/.well-known/acme-challenge/*` before
  redirecting ordinary requests to HTTPS. Leave Cloudflare **Always Use HTTPS off**
  to preserve that certificate-renewal path. Do not add a redirect rule covering
  ACME challenges or change the Worker Route to include HTTP without handling them.
- During setup keep the A record DNS only; enable Proxied after publishing and
  checking origin HTTPS. No `www` hostname is configured.
- Preserve imported records. The owner currently does not use domain email;
  Namecheap's free forwarding is not a supported email service after this DNS move.

Cloudflare's zone must be active before routing traffic. DNS caches can continue
using the old nameservers until their TTL expires. Leave the AWS web deployment
running: it remains the API reverse proxy and the DNS-only rollback target.

## Build and deploy

Requires Node 22 or later. From the repository root:

```bash
npm --prefix services/web ci
npm --prefix deploy/cloudflare ci
cd deploy/cloudflare
npx wrangler login --device --browser=false --scopes account:read user:read workers_scripts:write workers_routes:write zone:read
npm run check
npm run deploy
```

Wrangler manages `qireadr-web` and its HTTPS route. A dry run bundles without
publishing. If an account uses a scoped API token instead, provide it through
`CLOUDFLARE_API_TOKEN` with Workers Scripts Edit, Workers Routes Edit, and the
necessary account/zone read permissions; never place it in the Worker, source,
shell history or frontend environment. DNS and SSL settings are managed separately
in the Cloudflare dashboard. Do not add paid services to resolve a permission error.

The initial operator login uses `XDG_CONFIG_HOME=/tmp/qireadr-cloudflare-auth` and
`WRANGLER_SEND_METRICS=false`. Supply the same environment to reuse that session;
if `/tmp` is cleared, sign in again. Credentials are not included in the deployment.

The asset directory is only `services/web/dist`; the repository and private runtime
configuration are not uploaded. `_headers` retains the frontend security policy and
caches hashed JS/CSS for one year. HTML uses the platform's revalidation default.
API requests bypass the edge cache, never follow OAuth redirects on the server, and
return `private, no-store`, including failures. Worker request logging and public
workers.dev/preview URLs are disabled to avoid recording private URL parameters.

Static asset requests are free and unlimited under the current Workers pricing;
API proxy invocations count against the Workers Free request quota (100,000/day).
The frontend move does not eliminate EC2 or RDS charges.

## Verify the cutover

1. Verify the AWS certificate without skipping TLS verification:
   `curl --resolve qireadr.com:443:3.221.223.180 -I https://qireadr.com/`.
2. After enabling proxying, check root HTML and its hashed JS/CSS; their contents
   must match the local build. A second fetch should be cacheable for assets.
3. `/api/healthz` returns success. `/api/auth/session` and `/api/novels` return 401
   without a session, including with forged `X-Account-ID`/`X-Reader-ID` headers.
   API responses must have no shared-cache hit and must be `private, no-store`.
4. `/api/auth/login` returns a browser redirect to Google and a Secure/HttpOnly
   login-state cookie. The callback remains `https://qireadr.com/api/auth/callback`.
   Complete a real invited Google login and a CSRF-protected action in the browser.
5. Verify ordinary HTTP redirects to HTTPS, while a nonexistent HTTP ACME challenge
   gets an origin challenge response rather than the React application.
6. Keep Ask AI's upstream timeout below Cloudflare's origin read timeout. The current
   reader default is 120 seconds; Cloudflare documents 125 seconds. Slow requests
   still need a real-provider smoke check; this migration does not add streaming or
   change timeout behavior.

Rollback by changing the apex A record to DNS only, retaining `3.221.223.180`.
The valid AWS certificate and existing frontend then serve the same domain directly.
This does not roll back the database or affect stored books.

References: [Worker Routes](https://developers.cloudflare.com/workers/configuration/routing/routes/),
[static asset routing](https://developers.cloudflare.com/workers/static-assets/routing/worker-script/),
[static asset billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/),
[connection limits](https://developers.cloudflare.com/fundamentals/reference/connection-limits/).
