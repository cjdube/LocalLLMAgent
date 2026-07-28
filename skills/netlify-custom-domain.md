---
description: Point a Cloudflare-registered domain at a Netlify site, and redeploy without losing it.
---

When the user wants a domain they registered at Cloudflare to serve a Netlify
site:

1. **Netlify first.** Site configuration → Domain management → Add custom domain,
   enter the apex domain, confirm ownership. Netlify registers both the apex and
   the `www` subdomain.
2. **Then Cloudflare DNS** (DNS → Records), two CNAMEs:
   - apex: name `@`, target `apex-loadbalancer.netlify.com`
   - www: name `www`, target `<site-name>.netlify.app`
3. **Set both to "DNS only" — the grey cloud.** This is the step people miss. With
   Cloudflare's orange-cloud proxy on, Netlify can't complete the ACME challenge
   and never issues the Let's Encrypt certificate, so the site stays on a TLS
   error with no obvious cause.
4. Wait 2–15 minutes for propagation, then re-check Netlify's Domain management
   tab — it verifies the records and provisions HTTPS on its own. Don't retry the
   setup during this window; it looks broken until it isn't.

**Serving the app at a subpath** (`domain.dev/appname` rather than the root): move
the app's files into a subfolder of the publish directory and put something at the
root. Relative asset references (`style.css`, `app.js`) keep working after the
move — only absolute ones break. Ask what the root should be before moving
anything: a redirect to the app, a placeholder landing page, or left empty.

**Redeploying a drag-and-drop site — the trap.** If the site was originally
deployed by dragging a folder onto `app.netlify.com/drop`, drag the new folder
onto *that existing site's* Deploys tab. Using the generic `/drop` page again
creates a brand-new site, and the custom domain, DNS verification, and
certificate stay behind on the old one. A git push does not deploy this kind of
site at all.

**Converting to git-connected deploys** (so `git push` publishes): on the existing
site, Site configuration → Build & deploy → Continuous deployment → Link
repository. Authorize Netlify's GitHub App, pick the repo, set the branch, leave
the build command empty for a static site, and set the publish directory (`.` if
the entry `index.html` is at the repo root). The custom domain and certificate
stay attached — linking git only changes where deploys come from.
