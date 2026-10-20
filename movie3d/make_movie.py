#!/usr/bin/env python3
"""Make a 3D placement movie: place_simple's run, drawn on a 3D board, as an .mp4.

    python3 -X utf8 movie3d/make_movie.py BOARD OUT.kicad_pcb \\
        --groups groups.json --intent intent.json --seed 0 [--movie OUT_3d.mp4]

BOARD and OUT come first; every argument but this tool's own is
place_simple.py's. Three steps, each printed with its time:

  1. record  (record.py)   place_simple runs in-process with read-only hooks
                           and writes scene.json + timeline.json to --work;
  2. render  (render.mjs)  headless Chromium draws each frame with three.js
                           (page/movie3d.mjs) to --work/frames/f00000.png ...;
  3. encode  (ffmpeg)      the frames become the .mp4, and three stills are
                           copied beside it: <movie>_start.png (the input),
                           <movie>_anneal.png (mid-anneal), <movie>_final.png.

Needs `npm ci` in movie3d/ once (playwright-core + three, pinned), a Chromium
playwright-core can drive (its own cached headless shell, or --browser /
$MOVIE3D_CHROMIUM), and ffmpeg on PATH. Exit codes: place_simple's, or 2
when rendering or encoding fails.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import record  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], allow_abbrev=False,
                                 epilog="BOARD and OUT come first; every other argument is "
                                        "place_simple.py's (--groups, --intent, --seed, ...).")
    record.own_args(ap, work_required=False)
    ap.add_argument('--movie', help="the movie to write (default: OUT's name + _3d.mp4)")
    ap.add_argument('--browser', default=os.environ.get('MOVIE3D_CHROMIUM'),
                    help='a Chromium executable (default: $MOVIE3D_CHROMIUM, else the headless '
                         'shell playwright-core finds in its own browser cache)')
    ap.add_argument('--crf', type=int, default=20, help='x264 quality, lower is better (default 20)')
    a, rest = ap.parse_known_args(argv)
    if len(rest) < 2 or rest[0].startswith('-') or rest[1].startswith('-'):
        ap.error('BOARD and OUT come first')
    stem = os.path.splitext(os.path.abspath(rest[1]))[0]
    a.work = os.path.abspath(a.work or stem + '_movie3d')
    movie = os.path.abspath(a.movie or stem + '_3d.mp4')
    node = shutil.which('node')
    if not node:
        sys.exit('make_movie: no `node` on PATH')
    if not os.path.isdir(os.path.join(HERE, 'node_modules', 'playwright-core')):
        sys.exit(f'make_movie: run `npm ci` in {HERE} first')
    if not shutil.which('ffmpeg'):
        sys.exit('make_movie: no `ffmpeg` on PATH')

    # 1. record
    t0 = time.time()
    rc, got = record.record(a, rest)
    t_rec = time.time() - t0
    if not got:
        print('make_movie: place_simple did not get as far as a placement', file=sys.stderr)
        return rc or 2
    sp, tp, n = got
    print(f"make_movie: recorded {n} frames in {t_rec:.1f} s")

    # 2. render
    frames = os.path.join(a.work, 'frames')
    shutil.rmtree(frames, ignore_errors=True)
    job = {'browser': a.browser, 'width': a.width, 'height': a.height, 'outDir': frames,
           'scene': sp, 'timeline': tp}
    jp = os.path.join(a.work, 'job.json')
    with open(jp, 'w', encoding='utf-8') as f:
        json.dump(job, f)
    t1 = time.time()
    proc = subprocess.Popen([node, os.path.join(HERE, 'render.mjs'), jp], cwd=HERE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    done, err = None, None
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get('type')
        if kind == 'info':
            print(f"make_movie: rendering on {msg.get('renderer')} (Chromium {msg.get('browser')}, "
                  f"three r{msg.get('three')})")
        elif kind == 'progress':
            print(f"  {msg['done']}/{msg['of']} frames", flush=True)
        elif kind == 'done':
            done = msg
        elif kind == 'error':
            err = msg.get('why')
    proc.wait()
    t_ren = time.time() - t1
    if err or done is None or proc.returncode:
        tail = (proc.stderr.read() or '').strip().splitlines()[-3:]
        print(f"make_movie: the render failed: {err or ' | '.join(tail) or proc.returncode}",
              file=sys.stderr)
        return 2
    print(f"make_movie: rendered {done['frames']} frames in {t_ren:.1f} s "
          f"({done['ms_per_frame']:.0f} ms each)")

    # 3. encode, and the stills
    t2 = time.time()
    os.makedirs(os.path.dirname(movie), exist_ok=True)
    r = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-framerate', f'{a.fps:g}',
                        '-i', os.path.join(frames, 'f%05d.png'), '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-crf', str(a.crf), '-movflags', '+faststart', movie],
                       capture_output=True, text=True)
    if r.returncode or not os.path.isfile(movie):
        print(f"make_movie: ffmpeg failed: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 2
    with open(tp, encoding='utf-8') as f:
        labels = [fr['label'] for fr in json.load(f)['frames']]
    anneal = [i for i, lab in enumerate(labels) if lab.startswith('anneal')]
    stills = {'start': 0, 'anneal': anneal[len(anneal) // 2] if anneal else len(labels) // 2,
              'final': len(labels) - 1}
    base = os.path.splitext(movie)[0]
    for name, i in stills.items():
        shutil.copy(os.path.join(frames, f'f{i:05d}.png'), f'{base}_{name}.png')
    print(f"make_movie: {movie} ({n} frames, {n / a.fps:.1f} s, "
          f"{os.path.getsize(movie) / 1e6:.1f} MB) in {time.time() - t2:.1f} s; stills "
          + ', '.join(f'{base}_{k}.png' for k in stills))
    return rc


if __name__ == '__main__':
    sys.exit(main())
