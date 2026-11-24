"""The README's hero GIF is made of the frames the recorder saved, by a rule.

`scripts/demo_gif.py` keeps a frame only when the one kept before it is old enough,
and always the last one, and shows each until the next, between a floor and a cap.
Those rules are tried on lists of frames alone. Then a few frames of a few pixels each,
made here, go through the whole of it, and the GIF it writes is read back.
"""

import json
from pathlib import Path

import pytest
from PIL import Image, ImageSequence
from scripts import demo_gif
from scripts.demo_gif import LAST_MS, LONGEST_MS, Frame

SIZE = (8, 6)


def at(*times_ms: int) -> list[Frame]:
    """Frames painted at these moments, in milliseconds."""
    return [Frame(f"{index:04}.png", ms) for index, ms in enumerate(times_ms)]


def test_a_frame_is_kept_once_the_one_kept_before_it_is_66_ms_old() -> None:
    frames = at(0, 65, 66, 100, 132, 150, 300)

    assert [frame.at_ms for frame in demo_gif.kept(frames)] == [0, 66, 132, 300]


def test_the_last_frame_is_kept_however_soon_it_came() -> None:
    frames = at(0, 100, 110)

    assert demo_gif.kept(frames) == frames
    assert demo_gif.kept(at(0)) == at(0)
    assert demo_gif.kept([]) == []


def test_a_frame_is_shown_until_the_next_between_the_floor_and_the_cap() -> None:
    frames = at(0, 120, 186, 2186, 2196)

    # GIF counts in hundredths of a second: a frame held to the floor, 66 ms, is shown
    # for 70.
    assert demo_gif.durations(frames) == [120, 70, LONGEST_MS, 70, LAST_MS]
    assert demo_gif.durations(at(0)) == [LAST_MS]


def test_the_colours_are_chosen_from_frames_spread_over_the_recording_and_the_last() -> None:
    assert demo_gif.sampled(150) == [0, 12, 25, 37, 50, 62, 75, 87, 100, 112, 125, 137, 149]
    assert demo_gif.sampled(5) == [0, 1, 2, 3, 4]
    assert demo_gif.sampled(1) == [0]


def record(folder: Path, colours: list[tuple[int, int, int]], times_ms: list[int]) -> None:
    """Save frames of one colour each, painted at these moments, as the recorder does."""
    listed = []
    for index, (colour, ms) in enumerate(zip(colours, times_ms, strict=True)):
        name = f"{index:04}.png"
        Image.new("RGB", SIZE, colour).save(folder / name)
        listed.append({"name": name, "at": 1_795_508_965 + ms / 1000})
    (folder / demo_gif.LISTED).write_text(json.dumps(listed))


def shown(gif: Path) -> list[tuple[tuple[int, ...] | float, int]]:
    """Each frame of the GIF: its one colour, and for how long it is shown. And that
    the GIF has the frames' size, and loops for ever."""
    with Image.open(gif) as image:
        assert image.size == SIZE
        assert image.info["loop"] == 0
        frames = []
        for frame in ImageSequence.Iterator(image):
            colours = frame.convert("RGB").getcolors()
            assert colours is not None
            assert len(colours) == 1
            frames.append((colours[0][1], int(frame.info["duration"])))
        return frames


def test_the_gif_shows_the_kept_frames_for_as_long_as_the_rule_says(tmp_path: Path) -> None:
    red, green, blue, grey, white = (200, 0, 0), (0, 160, 0), (0, 0, 220), (90, 90, 90), (255,) * 3
    # The second frame comes too soon after the first, and the last too soon after the
    # fourth: the one is left out, and the other kept all the same.
    record(tmp_path, [red, green, blue, grey, white], [0, 30, 100, 3100, 3120])

    demo_gif.assemble(tmp_path, tmp_path / "out" / "hero.gif")

    assert shown(tmp_path / "out" / "hero.gif") == [
        (red, 100),
        (blue, LONGEST_MS),
        (grey, 70),
        (white, LAST_MS),
    ]


def test_the_colours_of_the_last_frame_are_in_the_gif(tmp_path: Path) -> None:
    # Thirty frames of greys, kept every 100 ms, and a last one of a colour no other
    # frame has: as the page that turns dark at the end of the recording. Twelve frames
    # spread over the thirty leave out the last one, so its colour comes from it alone.
    greys = [(level, level, level) for level in range(0, 240, 8)]
    colours = [*greys[:29], (255, 0, 0)]
    record(tmp_path, colours, [index * 100 for index in range(30)])

    demo_gif.assemble(tmp_path, tmp_path / "hero.gif")

    assert shown(tmp_path / "hero.gif")[-1] == ((255, 0, 0), LAST_MS)


def test_it_says_what_the_gif_holds(tmp_path: Path) -> None:
    record(tmp_path, [(0, 0, 0), (255, 255, 255)], [0, 500])
    demo_gif.assemble(tmp_path, tmp_path / "hero.gif")

    said = demo_gif.described(tmp_path / "hero.gif")

    assert said.startswith(f"{tmp_path / 'hero.gif'}: 2 frames, 3.5 s, ")
    assert said.endswith(" MB, 8x6")


def test_a_gif_too_heavy_for_a_readme_fails_the_run_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record(tmp_path, [(0, 0, 0), (255, 255, 255)], [0, 500])
    light, heavy = tmp_path / "light.gif", tmp_path / "heavy.gif"

    assert demo_gif.main([str(tmp_path), str(light)]) == 0
    assert capsys.readouterr().err == ""

    # The same recording, held to less than it weighs.
    monkeypatch.setattr(demo_gif, "MOST_BYTES", light.stat().st_size - 1)
    assert demo_gif.main([str(tmp_path), str(heavy)]) == 1
    assert "too heavy for a README" in capsys.readouterr().err
    # Written all the same, to be looked at.
    assert heavy.read_bytes() == light.read_bytes()


def test_a_frame_is_drawn_without_dithering(tmp_path: Path) -> None:
    # More colours than the palette holds, 400 of them, each a column of the frame from
    # top to bottom. Without dithering every pixel takes the palette's nearest colour,
    # so a column stays one colour. Dithering would speckle it: it passes what a pixel
    # lost on to its neighbours, the ones below it too.
    width, height = 400, 6
    frame = Image.new("RGB", (width, height))
    frame.putdata(
        [(x * 255 // (width - 1), x * 37 % 256, 160) for _ in range(height) for x in range(width)]
    )
    frame.save(tmp_path / "0000.png")
    (tmp_path / demo_gif.LISTED).write_text(json.dumps([{"name": "0000.png", "at": 0}]))

    demo_gif.assemble(tmp_path, tmp_path / "hero.gif")

    with Image.open(tmp_path / "hero.gif") as image:
        drawn = image.convert("RGB")
    for x in range(width):
        assert len({drawn.getpixel((x, y)) for y in range(height)}) == 1
