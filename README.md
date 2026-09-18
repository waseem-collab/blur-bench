# Blur Bench

A local tool for hiding things in a video. Open a clip, step to the exact frame,
draw over what should not be seen, and export a copy with those spots blurred
out. The video never leaves your machine.

```bash
./run.sh          # then open http://127.0.0.1:7788
```

Needs `ffmpeg` on the PATH plus Python 3.9+ with `flask`, `opencv-python` and
`numpy` (`pip3 install -r requirements.txt`).

## Opening a clip

Drop or pick a file to copy it into `workspace/`, type a path to open it where it
already sits, or browse this machine. Clips you have opened come back in a list
with a poster frame.

## Moving through the video

Every position is a frame number, not an approximate time, so the same frame
always gives the same picture.

| | |
|---|---|
| <kbd>&larr;</kbd> <kbd>&rarr;</kbd> | one frame |
| <kbd>Shift</kbd> + <kbd>&larr;</kbd> <kbd>&rarr;</kbd> | ten frames |
| <kbd>Home</kbd> <kbd>End</kbd> | first, last frame |
| <kbd>Space</kbd> | play or pause |
| <kbd>B</kbd> | toggle the blurred preview |

Frames are decoded on the server, so playback runs as fast as decoding allows
rather than at true speed — it is for reviewing, not for watching.

## Drawing ovals

Drag across the picture to draw one. Hold <kbd>Shift</kbd> for a circle, or
<kbd>Alt</kbd> to draw out from the centre. An oval covers the whole clip by
default; **From** and **To** in its settings narrow that to a range of frames,
and "Start here" / "End here" use the frame you are on.

Once drawn it stays editable: click to select, drag the middle to move, drag any
of the eight handles to resize, and use the handle on the stem above it to rotate
(<kbd>Shift</kbd> snaps to 15°). <kbd>Del</kbd> removes it,
<kbd>Ctrl</kbd>+<kbd>D</kbd> duplicates and <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes.
Work is saved automatically.

Each one has its own **effect** (a blur, big pixels or solid black), **strength**
measured against its own size, and **edge softness**. The ◉ beside it in the list
leaves it out without deleting it. Turn on **See it blurred** to see the frame
exactly as it will export.

### Following something that moves

Turn on **Follow something that moves** and every time you reposition the oval on
a new frame that position is recorded, with the oval sliding, growing and turning
between recorded positions. Those positions show as diamonds on its strip in the
timeline.

## Exporting

**Export blurred copy** writes a new H.264 MP4 and never touches the original.
Export the whole clip or a range of frames, choose picture quality and encoding
speed, and keep or drop the sound.

Audio is copied through untouched where the format allows and re-encoded to AAC
only when it does not; if both fail the video still exports without sound. The
export is constant-frame-rate at the source average, so a variable-frame-rate
recording can drift over long clips. Odd frame dimensions are cropped by a single
pixel, since H.264 needs even ones.

## Notes

Everything is stored in `workspace/`, one folder per clip, with the shapes in a
plain `project.json` you can read or back up.
