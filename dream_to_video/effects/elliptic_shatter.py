#!/usr/bin/env python3
"""
Elliptic Shatter Effect
-----------------------
Overlays a rotating elliptical broken-edge / refraction effect on video.
- Center of frame: clear, original content
- Edge zone: shattered pixel fragments (displaced image pixels, slightly rotating)
- Outer border: dark/black with scattered micro-fragments
- Chromatic aberration in the transition zone for glass/lens refraction feel
"""

import cv2
import numpy as np
import argparse
import os
import sys
import time


def elliptic_distance(h, w, ellipse_y=0.82):
    """
    Returns per-pixel elliptical distance field.
    Value = 1.0 exactly on the reference ellipse (semi-axes: w/2, h/2 * ellipse_y).
    Value < 1.0 inside, > 1.0 outside.
    ellipse_y < 1.0 makes ellipse taller (portrait-ish) relative to width.
    """
    cx, cy = w / 2.0, h / 2.0
    rx = w / 2.0
    ry = h / 2.0 * ellipse_y
    y_g, x_g = np.mgrid[0:h, 0:w]
    dist = np.sqrt(((x_g - cx) / rx) ** 2 + ((y_g - cy) / ry) ** 2)
    return dist.astype(np.float32)


def make_blend_masks(dist, inner=0.68, outer=1.02):
    """
    Compute three smooth masks:
      center_alpha : 1.0 inside inner ellipse, 0.0 at outer, 0.0 beyond
      edge_alpha   : 0.0 inside inner, peaks ~1.0 at outer boundary, 0.0 beyond
      dark_alpha   : 0.0 inside outer, 1.0 well beyond outer
    """
    # Smooth-step t: 0 inside inner, 1 at outer
    t = np.clip((dist - inner) / max(outer - inner, 1e-6), 0.0, 1.0)
    t_smooth = t * t * (3.0 - 2.0 * t)   # Hermite smoothstep

    # Fade-out beyond outer boundary
    beyond = np.clip((dist - outer) * 5.0, 0.0, 1.0)
    beyond_smooth = beyond * beyond * (3.0 - 2.0 * beyond)

    center_alpha = (1.0 - t_smooth) * (1.0 - beyond_smooth)
    edge_alpha   = t_smooth * (1.0 - beyond_smooth)
    dark_alpha   = beyond_smooth

    return (center_alpha.astype(np.float32),
            edge_alpha.astype(np.float32),
            dark_alpha.astype(np.float32))


def shatter_displacement_map(h, w, dist, frame_idx, max_disp=70):
    """
    Builds a pixel displacement map (cv2.remap compatible) that scatters edge
    pixels in a shattered-glass / rotating-fragment pattern.

    Uses multiple overlapping sine/cosine waves in polar space so the
    displacement looks like jagged shards rather than smooth ripples.
    The wave pattern advances slowly with frame_idx to give the subtle
    rotation feel seen in the reference image.
    """
    y_g, x_g = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2.0, h / 2.0

    # Polar angle from center
    angle = np.arctan2(y_g - cy, x_g - cx)

    t = frame_idx * 0.04  # Slow time advance

    # Radial component: controls outward/inward scatter
    r_wave = (
        np.sin(dist * 14.0 + angle * 3.0 + t       ) * 0.30 +
        np.sin(dist *  8.0 - angle * 2.5 - t * 1.3 ) * 0.25 +
        np.sin(dist * 22.0 + angle * 6.0 + t * 0.5 ) * 0.25 +
        np.sin(dist *  5.0 + angle * 1.0 - t * 0.9 ) * 0.20
    )

    # Tangential component: controls rotational scatter
    th_wave = (
        np.cos(dist * 11.0 - angle * 4.0 + t * 0.8 ) * 0.30 +
        np.cos(dist * 17.0 + angle * 2.0 - t       ) * 0.25 +
        np.cos(dist *  6.0 + angle * 7.0 + t * 1.1 ) * 0.25 +
        np.cos(dist * 26.0 - angle * 3.0 + t * 0.6 ) * 0.20
    )

    # Amplify only in the edge zone; quadratic ramp for punchy shatter
    amp = np.clip((dist - 0.55) * 2.8, 0.0, 1.0) ** 1.4 * max_disp

    # Polar -> Cartesian displacement
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    disp_x = (r_wave * cos_a - th_wave * sin_a) * amp
    disp_y = (r_wave * sin_a + th_wave * cos_a) * amp

    map_x = np.clip(x_g + disp_x, 0, w - 1).astype(np.float32)
    map_y = np.clip(y_g + disp_y, 0, h - 1).astype(np.float32)
    return map_x, map_y


def chromatic_aberration(frame, dist, strength=5):
    """
    Shift R channel outward and B channel inward along the radial direction.
    Strength in pixels at the ellipse boundary.
    Only applied in the edge zone.
    """
    h, w = frame.shape[:2]
    y_g, x_g = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2.0, h / 2.0

    # Normalized radial unit vector
    nx = (x_g - cx) / (w / 2.0)
    ny = (y_g - cy) / (h / 2.0)

    ca_mask = np.clip((dist - 0.60) * 2.5, 0.0, 1.0).astype(np.float32)

    result = frame.astype(np.float32)
    for ch, sign in [(0, -1.0), (2, 1.0)]:   # B inward, R outward
        dx = nx * sign * strength * ca_mask
        dy = ny * sign * strength * ca_mask
        src_x = np.clip(x_g + dx, 0, w - 1).astype(np.float32)
        src_y = np.clip(y_g + dy, 0, h - 1).astype(np.float32)
        shifted = cv2.remap(frame[:, :, ch], src_x, src_y, cv2.INTER_LINEAR)
        blend = ca_mask
        result[:, :, ch] = frame[:, :, ch] * (1.0 - blend) + shifted * blend

    return np.clip(result, 0, 255).astype(np.uint8)


def edge_grain(h, w, dist, frame_idx, strength=18):
    """
    Adds film-grain / noise in the shatter zone for texture, using a
    seed that cycles every 4 frames so it flickers subtly but not wildly.
    """
    seed = (frame_idx // 4) % 256
    rng = np.random.RandomState(seed)
    noise = rng.normal(0, strength, (h, w, 3)).astype(np.float32)
    mask = np.clip((dist - 0.62) * 2.5, 0.0, 1.0)[:, :, np.newaxis]
    return noise * mask


def process_frame(frame, dist, frame_idx, args):
    h, w = frame.shape[:2]

    # 1. Blend masks
    ca, ea, da = make_blend_masks(dist, args.inner_edge, args.outer_edge)
    ca3 = ca[:, :, np.newaxis]
    ea3 = ea[:, :, np.newaxis]

    # 2. Shatter displacement map (different frequencies per frame)
    map_x, map_y = shatter_displacement_map(h, w, dist, frame_idx, args.max_disp)

    # 3. Remapped (shattered) frame
    shattered = cv2.remap(frame, map_x, map_y, cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)

    # 4. Apply chromatic aberration to both layers
    frame_ca     = chromatic_aberration(frame,    dist, args.ca)
    shattered_ca = chromatic_aberration(shattered, dist, args.ca // 2)

    # 5. Grain on shattered layer
    grain = edge_grain(h, w, dist, frame_idx, args.grain)
    shattered_f = np.clip(shattered_ca.astype(np.float32) + grain, 0, 255)

    # 6. Compose:
    #    - center zone  : original (with CA)
    #    - edge zone    : darkened shattered fragments
    #    - outer zone   : black (both masks → 0)
    result = (frame_ca.astype(np.float32) * ca3 +
              shattered_f * ea3 * args.edge_brightness)

    return np.clip(result, 0, 255).astype(np.uint8)


def extract_preview_frames(output_path, n=3):
    """Extract n evenly-spaced frames and save as JPEG for preview."""
    cap = cv2.VideoCapture(output_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    saved = []
    for i in range(n):
        pos = int(total * (i + 1) / (n + 1))
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ret, frame = cap.read()
        if ret:
            out_path = f"/tmp/elliptic_shatter_preview_{i}.jpg"
            cv2.imwrite(out_path, frame)
            saved.append(out_path)
    cap.release()
    return saved


def main():
    parser = argparse.ArgumentParser(description="Elliptic Shatter Edge Effect")
    parser.add_argument("--input",  required=True, help="Input video path")
    parser.add_argument("--output", required=True, help="Output video path")

    # Ellipse shape
    parser.add_argument("--inner-edge",  type=float, default=0.68,
                        help="Ellipse inner boundary (clear center ends here). Default: 0.68")
    parser.add_argument("--outer-edge",  type=float, default=1.02,
                        help="Ellipse outer boundary (black starts here). Default: 1.02")
    parser.add_argument("--ellipse-y",   type=float, default=0.82,
                        help="Y-axis ratio of ellipse (<1 = wider, >1 = taller). Default: 0.82")

    # Effect intensity
    parser.add_argument("--max-disp",   type=float, default=70,
                        help="Max fragment displacement in pixels. Default: 70")
    parser.add_argument("--edge-brightness", type=float, default=0.38,
                        help="Brightness of shattered edge zone (0=black, 1=full). Default: 0.38")
    parser.add_argument("--ca",         type=int,   default=5,
                        help="Chromatic aberration shift in pixels. Default: 5")
    parser.add_argument("--grain",      type=float, default=18,
                        help="Grain/noise strength in edge zone. Default: 18")

    # Processing
    parser.add_argument("--frames",     type=int,   default=0,
                        help="Process only first N frames (0 = all). Default: 0")
    args = parser.parse_args()

    # Open input
    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        print(f"Error: Cannot open input video: {args.input}", file=sys.stderr)
        sys.exit(1)

    fps    = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w      = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h      = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total  = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    n_proc = min(total, args.frames) if args.frames > 0 else total

    print(f"Input : {args.input}")
    print(f"Output: {args.output}")
    print(f"Size  : {w}x{h}  FPS: {fps:.2f}  Frames: {n_proc}/{total}")
    print(f"Params: inner={args.inner_edge}  outer={args.outer_edge}  "
          f"ellipse_y={args.ellipse_y}  disp={args.max_disp}  "
          f"brightness={args.edge_brightness}  ca={args.ca}  grain={args.grain}")

    # Pre-compute static distance field (same for all frames)
    dist = elliptic_distance(h, w, args.ellipse_y)

    # Writer
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(args.output, fourcc, fps, (w, h))

    t0 = time.time()
    frame_idx = 0

    while frame_idx < n_proc:
        ret, frame = cap.read()
        if not ret:
            break
        processed = process_frame(frame, dist, frame_idx, args)
        writer.write(processed)
        frame_idx += 1

        if frame_idx % 30 == 0 or frame_idx == n_proc:
            elapsed = time.time() - t0
            fps_proc = frame_idx / max(elapsed, 1e-6)
            eta = (n_proc - frame_idx) / max(fps_proc, 1e-6)
            print(f"  [{frame_idx:5d}/{n_proc}]  {fps_proc:.1f} fr/s  ETA {eta:.0f}s")

    cap.release()
    writer.release()

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s  ({frame_idx / max(elapsed,1):.1f} fr/s)")
    print(f"Output: {args.output}")

    # Extract preview frames
    previews = extract_preview_frames(args.output, n=3)
    if previews:
        print(f"Preview frames saved: {', '.join(previews)}")


if __name__ == "__main__":
    main()
