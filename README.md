# Blur Bench

A local tool for hiding things in a video or a photo. Open a clip, step to the
exact frame, draw an oval or a rectangle over what should not be seen, and export
a copy with those spots blurred out. Nothing ever leaves your machine.

```bash
./run.sh          # then open http://127.0.0.1:7788
```

Needs `ffmpeg` on the PATH plus Python 3.9+ with `flask`, `opencv-python` and
`numpy` (`pip3 install -r requirements.txt`).

## Opening something

Videos (`.mp4 .mov .mkv .avi .webm .m4v .mpg .wmv .flv .ts`) and still images
(`.jpg .png .bmp .webp .tif`) open the same way, three ways from the opening
screen:

- **Drop or pick a file** — copies it into `workspace/` and leaves the original alone.
- **Type a path** — opens the file where it already sits, without copying. Best for big files.
- **Look through this machine** — a small file browser for the same thing.

Clips you have opened before are listed on the right with a poster frame and how
many shapes you have drawn. Removing one from the list deletes only the notes
about it, never the video.

## Moving through the video

Every position is a frame number, not an approximate time, so the same frame
always gives the same picture.

| | |
|---|---|
| <kbd>←</kbd> <kbd>→</kbd> | one frame |
| <kbd>Shift</kbd> + <kbd>←</kbd> <kbd>→</kbd> | ten frames |
| <kbd>Home</kbd> <kbd>End</kbd> | first, last frame |
| <kbd>[</kbd> <kbd>]</kbd> | previous, next frame that has a shape on it |
| <kbd>Del</kbd> | uncover this frame only |
| <kbd>Shift</kbd>+<kbd>Del</kbd> | remove the shape everywhere |
| <kbd>Space</kbd> | play or pause |
| <kbd>B</kbd> | toggle the blurred preview |
| scroll, <kbd>+</kbd> <kbd>-</kbd> | zoom in and out |
| <kbd>0</kbd> | fit the frame to the window |

You can also type a frame number in the box, or click and drag anywhere on the
timeline.

### Zooming in

**Scroll over the picture to zoom**, and whatever is under the pointer stays
under the pointer, so you can push straight into the corner of a licence plate
without chasing it around. <kbd>+</kbd> and <kbd>-</kbd> do the same from the
middle, <kbd>0</kbd> or the **Fit** button puts the whole frame back in view, and
the slider still works if you prefer it.

Since the wheel is spoken for, **drag with the middle mouse button to pan** once
the picture is bigger than the window. Past 200% the real pixels are shown rather
than a smoothed guess, which is what you want when covering something small.

One thing to know: while the picture is smaller than the window it sits centred,
so there is nowhere to scroll and zoom goes to the middle rather than to the
pointer. Once it fills the window, zoom follows the cursor. Frames are decoded on the server, so playback runs as fast as decoding
allows rather than at true speed — it is for reviewing, not for watching.

## Drawing shapes

**What you draw next** at the top of the right panel sets two things, and they
stick until you change them:

- **Oval** or **Rectangle** — a rectangle is covered as a true rectangle, right
  out to its square corners, and it stays a rectangle when you turn it.
- **This frame**, **Whole video**, or **Follow it**:
  - *This frame* — it belongs to the one frame you are on, and no other.
  - *Whole video* — kept from the first frame to the last. Good for something
    that never moves: a station logo, a burned-in timestamp, a fixed nameplate.
  - *Follow it* — draw over the thing and it is chased through the rest of the
    clip on its own. See below.

By default a shape belongs to the frame you drew it on and to no other, so
stepping forward gives you a clean picture again. The list below follows the
playhead and shows only what is on the current frame; switch it to **Every
frame** to see the whole set, and clicking any of those takes you to the frame it
lives on.

Drag across the picture to draw one. Hold <kbd>Shift</kbd> to keep it square or
circular, or <kbd>Alt</kbd> to draw out from the centre.

Once drawn, a shape stays editable: click to select it, drag the middle to move
it, drag any of the eight handles to resize, and use the handle on the stem above
it to rotate (<kbd>Shift</kbd> snaps to 15°). <kbd>Ctrl</kbd>+<kbd>D</kbd>
duplicates it and <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes. Work is saved
automatically as you go.

### Taking a cover off again

<kbd>Del</kbd> uncovers **the frame you are on, and only that frame**. On a shape
that only ever lived on this frame, that removes it. On a run of frames — a
follow, or anything covering a stretch — the rest of the run carries on: delete
at its first or last frame and the run simply starts or ends a frame later;
delete somewhere in the middle and it becomes two runs with a one-frame gap,
every remaining frame keeping the exact position it had.

So if a follow drifts for a few frames, step through and tap <kbd>Del</kbd> on
the bad ones without losing the good ones either side.

To get rid of a shape in its entirety, use <kbd>Shift</kbd>+<kbd>Del</kbd> or
**Remove it everywhere** in its settings.

Each shape has its own settings in the right panel:

- **Shape** — switch it between an oval and a rectangle at any time, keeping its
  size, angle and everything else.
- **Cover it with** — a blur, big pixels, or solid black.
- **Strength** — measured against the shape's own size, so a small one and a
  large one look equally covered.
- **Edge softness** — how far the cover fades out at the rim.
- **Covers** — *this frame only* or *a run of frames*, with **Whole clip** for
  the full length. This is the same choice you made before drawing, so you can
  change your mind afterwards.
- The ◉ next to a shape in the list leaves it out without deleting it.

### Following something automatically

Set **Follow it** before you draw, put a shape over a face, a plate or a screen,
and let go. The clip is read forward from that frame and the shape is carried
along with the thing underneath it, growing and shrinking as it comes closer or
further away. A progress strip shows how far it has got, with a **Stop** button
if you have seen enough. There is also **Follow it from this frame** in a
selected shape's own settings, for when you drew first and decided later.

It stops on its own at the end of the clip, or the moment it is no longer
confident — "lost it at frame 412" — rather than carrying on and quietly
covering the wrong part of the picture. Whatever it found up to that point is
kept, so you can step to where it gave up, drag the shape back onto the object
and follow again from there.

What comes back is an ordinary set of recorded positions, so nothing is locked:
drag, resize or turn the shape on any frame afterwards and that frame is
corrected, with the motion either side left alone. Positions that a straight
line already predicts are thinned out, so a long follow stays a small file.

**Follow by** picks the method, when more than one is available:

- **Motion** (the default) watches how the picture itself moves, using a grid of
  points checked forwards and backwards so only the trustworthy ones count. It
  is quick and it handles something that drifts or changes size.
- **Appearance** learns what the thing looks like. Steadier when an object turns
  on the spot, but it will not resize with it.
- If you install `opencv-contrib-python`, **Accurate** (CSRT) and **Quick** (KCF)
  appear in the list as well. The tool finds them by itself; nothing to configure.

*Motion* needs some distinguishable detail to hold on to — corners and texture,
not just a strong edge, since it cannot tell how far something slid along a plain
straight line. Faces, number plates, screens and printed labels all give it
plenty. A flat painted panel or a smooth colour band does not, and it will say so
at once rather than drift; use *Appearance* there, or mark the frames by hand.

### Working frame by frame

Redrawing the same shape on every frame would be miserable, so **Copy from frame
N** (<kbd>C</kbd>) brings the previous annotated frame's shapes onto this one,
keeping their kind, size and settings. The usual rhythm is: draw it once, step forward,
press <kbd>C</kbd>, nudge it onto its new position, repeat.

<kbd>[</kbd> and <kbd>]</kbd> jump to the previous and next frame that carries a
shape, so you can move through your own work rather than through the footage. The
timeline shows a mark for every annotated frame, which on a clip worked through
frame by frame reads as a band of coverage.

### Covering a stretch in one go

Set a shape to cover *a run of frames* and it applies from its first frame to its
last. Such a shape can also move: turn on **Follow something that moves**, and
every time you reposition it on a new frame that position is recorded, with the
shape sliding, growing and turning between recorded positions. Useful when a face
crosses the shot at a steady pace and you would rather mark two ends than thirty
frames.

Turn **See it blurred** on at any time to see the frame exactly as it will
export.

## Working on a photo

A still image is treated as a clip of one frame, so drawing, editing, the three
cover effects and the blurred preview all behave exactly as they do on video. The
parts that only make sense over time — the timeline, the frame stepper, *just
this frame* versus *the whole video* — simply are not there. Zoom still is.

Exporting asks only for a name and a format:

- **PNG** is lossless. Everything outside your shapes comes out pixel for pixel
  identical to the original, which is what you want for a document or a
  screenshot.
- **JPEG** with a quality slider, for photographs where file size matters.

Transparency and greyscale are carried through to a PNG export rather than being
flattened. A 16-bit image is written back as 8-bit.

## Exporting

This section is about video; see above for photos.

**Export blurred copy** writes a new H.264 MP4 and never touches the original.
Every shape is applied to exactly the frames it covers, so a clip annotated frame
by frame comes out blurred frame by frame. You can export the whole clip or just
a range of frames, choose picture quality and encoding speed, and keep or drop
the sound. Shapes you have left out are skipped.

The finished file lands in `workspace/<clip>/` and you can either download it
from the browser or tick **Put it in the same folder as the original**, which is
usually what you want for a clip you opened in place.

## Notes

- Audio is copied through untouched where the format allows, and re-encoded to
  AAC only when it does not; if both fail the video still exports without sound.
- The export is written at a constant frame rate matching the source average. On
  a variable-frame-rate recording, audio can drift from the picture over long
  clips.
- Odd frame dimensions are cropped by a single pixel, since H.264 needs even ones.
- Everything is stored in `workspace/`, one folder per clip or photo, with the
  shapes in a plain `project.json` you can read or back up.
- One file per project. To redact a batch of photos, open them one after another;
  each keeps its own shapes and shows up in the list on the opening screen.
- The address bar carries the clip and the frame, so reloading the page puts you
  back exactly where you were.
