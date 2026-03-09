# WetzelWest — Landing Page

Static site for **wetzelwest.com**, deployed via **Cloudflare Pages**.

## Structure

```
wetzelwest/
├── index.html          # Main landing page
├── styles.css          # All styles
├── _redirects          # Cloudflare Pages redirect rules
├── _headers            # Cloudflare Pages security headers
└── wetzelcrm/
    └── index.html      # WetzelCRM placeholder (wetzelwest.com/wetzelcrm)
```

## Cloudflare Pages Setup

1. Push this directory to a GitHub repo (e.g. `wetzelwest`)
2. In Cloudflare dashboard → Pages → Create a project → Connect to Git
3. Select the repo
4. Build settings:
   - **Framework preset**: None
   - **Build command**: *(leave empty)*
   - **Build output directory**: `/` (or the folder root)
5. Add custom domain → `wetzelwest.com`
6. Cloudflare will handle SSL automatically

## Projects linked

| Card | URL | Status |
|------|-----|--------|
| 1stVibe.ai | https://1stvibe.ai | External |
| Paffl.com | https://paffl.com | External |
| FSCollective.com | https://fscollective.com | External |
| LiftWithErin.com | https://liftwitherin.com | External |
| WetzelCRM | /wetzelcrm | Internal |

## Hero image

The hero uses an inline SVG mountain silhouette of Mt. Bachelor and the Three Sisters viewed looking west at dusk. To replace with a real photo:

1. Add your image as `hero.jpg` (recommend 2400×1350px, compressed)
2. In `index.html`, remove `.hero-scene` and its SVG
3. Add `background-image: url('hero.jpg')` to `.hero` in `styles.css`
4. Add `background-size: cover; background-position: center;` to `.hero`
