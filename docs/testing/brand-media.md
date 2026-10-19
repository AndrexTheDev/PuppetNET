# Uploaded brand assets – 19 October 2026

Source: `origin/main` commit `0a30ef6` (`Intro.mp4`, `Logo.jpg`, `Hintergrund.jpg`).
Originals were read from Git, not edited or re-encoded in place. Derived deployable
assets live in `apps/web/public/media/` (about 3.6 MiB total). No branch switch or
merge was needed, and no deployment was performed.

## Preparation

`python scripts/prepare-brand-media.py /path/to/originals` needs Pillow, NumPy,
SciPy and imageio-ffmpeg. This is an offline maintenance step, not a dependency
of Pages builds. The prepared assets are checked into the working tree.

- Logo: border-connected near-white exterior removed; inner details preserved.
  Transparent WebP for UI, smaller icon and PNG for Apple/social-card generation.
- Intro: 8 seconds, cropped from portrait 720×1280 to square 720×720, black bars
  removed. Each frame uses a border-connected white mask, **not** global white
  keying, to avoid holes in the bright animated central glow. VP9 WebM with alpha;
  H.264 MP4 is precomposited on the site's dark colour for Safari/fallback.
  Audio is omitted deliberately for predictable muted autoplay.
- Background: original raster detail preserved, resized, brightness reduced and
  edges feathered into alpha. There is no plain white exterior in the supplied
  radar picture; luminous internal map features are not erroneously deleted.
  A CSS dark overlay preserves readability. No expensive live canvas rendering.

## Integration

Logo in homepage navigation/footer, dashboard header, legacy header, favicon and
newly generated Open Graph cards. Background shared by modern pages and legacy;
analysis panels intentionally remain mostly opaque.

Intro autoplays on homepage entry **once per tab/session**, not on every dashboard
or profile navigation. Replay button provided. Muted, inline, skippable by button
or Escape, closes on completion/error/autoplay refusal, 12-second safety deadline.
Underlying content is inert while open and restored on exit; focus restored.
Reduced-motion and data-saving preferences suppress automatic playback/download.
Safari selects the dark MP4 because VP9 decoding support alone does not guarantee
alpha support. Native Safari/iPhone behaviour is not yet device-tested.

## Verification

Full Pages build, typechecking and export checks passed. Playwright
`tests/brand_browser.mjs` passes actual playback, completion, once-per-session,
replay/skip/Escape, inert restoration, blocked autoplay, reduced-motion/no video
request, MP4 failover, logo loading and responsive homepage widths.
Temporary visual evidence: `.screenshots/branding/`.
The older root-relative-asset check and screenshot server now understand shared
`/media/` assets in the actual public export root.

Limitations: white-removal is image processing, not a hand-roto studio master;
minor compression/edge artifacts can remain. MP4 fallback is dark-composited, not
transparent. No audio autoplay, no original portrait black bars. Main uploads are
not automatically processed on every build; rerun the preparation script for replacements.
