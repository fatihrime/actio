"""Turns what `make demo-record` recorded into the README's hero GIF.

The recorder (apps/web/demo/record.mts) saves every picture Chromium painted while it
drove the chat, as a PNG file, and a list of them in the order they came, with the
moment each was painted: `frames.json`, of `{"name": ..., "at": seconds}`. This makes
one GIF of them that plays at the pace they came, loops for ever, and stays small
enough for a README:

- a frame is kept only when the one kept before it is at least LEAST_MS older, and the
  last one always, so that the GIF ends on the picture the recording ended on;
- every frame is drawn with the same 256 colours, chosen from about twelve frames
  spread over the recording and the last one, the page in dark, whose colours appear
  only at the end. Without dithering: its speckles differ from one frame to the next,
  and a GIF stores of each frame only what changed;
- each frame is shown until the next kept one was painted, at least LEAST_MS, and at
  most LONGEST_MS, which shortens a long wait for the model. The last one stays for
  LAST_MS before the GIF starts again.

A GIF heavier than MOST_BYTES is written all the same, to be looked at, and the run
fails.

Usage: demo_gif.py <folder of frames> <GIF to write>
"""

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from PIL import Image, ImageSequence

# The list the recorder writes beside the frames.
LISTED = "frames.json"
# About fifteen frames a second: enough for text that appears and a page that changes
# colour, at a fraction of what every frame Chromium painted would weigh.
LEAST_MS = 66
LONGEST_MS = 1500
LAST_MS = 3000
COLOURS = 256
# How many frames spread over the recording the colours are chosen from, besides the
# last one.
SAMPLED = 12
# The heaviest GIF this passes. The commit hook that keeps large files out of git makes
# an exception of the hero GIF's path, so this is the one place its weight is looked at:
# a recording as it is made today weighs about 0.6 MB.
MOST_BYTES = 1_500_000


@dataclass(frozen=True)
class Frame:
    """A picture the recorder saved: its file, and when it was painted, in whole
    milliseconds."""

    name: str
    at_ms: int


def listed(folder: Path) -> list[Frame]:
    """The frames the recorder saved in `folder`, in the order they came."""
    entries = json.loads((folder / LISTED).read_text())
    return [Frame(str(entry["name"]), round(float(entry["at"]) * 1000)) for entry in entries]


def kept(frames: Sequence[Frame]) -> list[Frame]:
    """The frames the GIF shows: each one at least LEAST_MS after the one kept before
    it, and the last one whenever it came."""
    shown: list[Frame] = []
    for index, frame in enumerate(frames):
        if not shown or index == len(frames) - 1 or frame.at_ms - shown[-1].at_ms >= LEAST_MS:
            shown.append(frame)
    return shown


def durations(frames: Sequence[Frame]) -> list[int]:
    """How long each of the kept `frames` is shown, in milliseconds: until the next one
    was painted, held between LEAST_MS and LONGEST_MS, and LAST_MS for the last one.

    A GIF counts in hundredths of a second, so each time is rounded to one, and a
    frame held to LEAST_MS is shown for 70 ms. Rounded here, and not by Pillow, which
    would drop what is left over, and show it for 60."""
    between = [
        min(LONGEST_MS, max(LEAST_MS, after.at_ms - before.at_ms))
        for before, after in pairwise(frames)
    ]
    return [round(ms, -1) for ms in between] + [LAST_MS]


def sampled(count: int) -> list[int]:
    """Which of `count` kept frames the colours are chosen from: SAMPLED of them spread
    over the recording, and the last one."""
    return sorted({index * count // SAMPLED for index in range(SAMPLED)} | {count - 1})


def assemble(folder: Path, out: Path) -> None:
    """Make the GIF of the frames in `folder`, and write it to `out`."""
    frames = kept(listed(folder))
    if not frames:
        raise ValueError(f"{folder / LISTED} lists no frames")

    def opened(frame: Frame) -> Image.Image:
        with Image.open(folder / frame.name) as image:
            return image.convert("RGB")

    # One strip of the sampled frames, one under another, from which the colours are
    # chosen at once for the whole recording.
    picked = [opened(frames[index]) for index in sampled(len(frames))]
    width, height = picked[0].size
    strip = Image.new("RGB", (width, height * len(picked)))
    for row, image in enumerate(picked):
        strip.paste(image, (0, row * height))
    palette = strip.quantize(
        colors=COLOURS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    drawn = [opened(frame).quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]

    out.parent.mkdir(parents=True, exist_ok=True)
    drawn[0].save(
        out,
        save_all=True,
        append_images=drawn[1:],
        duration=durations(frames),
        loop=0,
        # Each frame is drawn over the one before, which stays, so that Pillow stores
        # of it only the rectangle that changed.
        disposal=1,
        # The colours are the palette's already: Pillow is not to choose others. Given
        # the palette, it writes it once for the whole GIF, and not again with every
        # frame, 768 bytes each time.
        optimize=False,
        palette=palette.getpalette(),
    )


def described(gif: Path) -> str:
    """What the GIF holds, read from the file: frames, length, size and dimensions."""
    with Image.open(gif) as image:
        shown = [frame.info.get("duration", 0) for frame in ImageSequence.Iterator(image)]
        width, height = image.size
    seconds = sum(shown) / 1000
    megabytes = gif.stat().st_size / 1_000_000
    return f"{gif}: {len(shown)} frames, {seconds:.1f} s, {megabytes:.2f} MB, {width}x{height}"


def main(arguments: Sequence[str]) -> int:
    if len(arguments) != 2:
        sys.stderr.write("Usage: demo_gif.py <folder of frames> <GIF to write>\n")
        return 2
    folder, out = Path(arguments[0]), Path(arguments[1])
    assemble(folder, out)
    sys.stdout.write(described(out) + "\n")
    if out.stat().st_size > MOST_BYTES:
        sys.stderr.write(
            f"{out} is heavier than {MOST_BYTES / 1_000_000:.1f} MB, too heavy for a README: "
            "record again, or record less.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
