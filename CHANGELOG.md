# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-10-10

First public release.

### Added
- Desktop GUI with side-by-side original and vector previews that re-render live
  in a background thread.
- Light and dark appearance that follows the system, overridable with
  `VEXTRACE_THEME=light|dark`.
- Five tracing modes: color, black & white, potrace, silhouette (the subject as
  one solid shape) and lines (a line drawing of the edges).
- A home page with shortcuts, sample images and recently opened images, and an
  app-drawn title bar with minimize, maximize and close.
- A short guided tour on first launch, available again from the ••• menu.
- A quick low-resolution preview on large images, followed by the
  full-quality trace.
- A layout that adapts to small windows.
- Presets for logos, illustrations and photos.
- Background removal, square 1:1 viewBox with padding, and upscaling of small
  images.
- Batch conversion of whole folders from the GUI and the CLI.
- Large images are scaled down to 1600 px before tracing (`--max-size` to
  change), keeping their original dimensions in the SVG.
- Photos taken sideways are rotated upright from their EXIF orientation.
- Command-line interface with every setting exposed as a flag.
- `install.sh` / `uninstall.sh` for Arch and CachyOS, with a desktop entry and
  Dolphin "Open With" integration.
