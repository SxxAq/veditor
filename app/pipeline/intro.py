"""Opening title slate and audio jingle rendering module for VEditor pipeline.

Renders high-definition title slates (event name, talk title, speakers, room/date,
and event logo) and muxes an opening audio jingle to produce a standard introductory
video segment for conference talks.
"""

from __future__ import annotations

import logging
import textwrap
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)


def _create_title_slate_image(
    title: str,
    speakers: list[str] | str,
    event_name: str,
    room_date: str,
    logo_path: Path | str | None,
    resolution: tuple[int, int],
) -> np.ndarray:
    """Renders a title slate RGB numpy array."""
    width, height = resolution
    img = Image.new("RGB", (width, height), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)

    # 1. Subtle vertical gradient background (slate dark -> midnight blue)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        r = int(15 + (30 - 15) * ratio)
        g = int(23 + (41 - 23) * ratio)
        b = int(42 + (59 - 42) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # 2. Composite event logo if supplied
    content_x = int(width * 0.08)
    content_y = int(height * 0.12)

    if logo_path:
        lp = Path(logo_path)
        if lp.is_file():
            try:
                with Image.open(lp) as logo_img:
                    logo_rgba = logo_img.convert("RGBA")
                    # Scale logo to fit nicely within header bounds
                    max_logo_w = int(width * 0.25)
                    max_logo_h = int(height * 0.15)
                    logo_rgba.thumbnail(
                        (max_logo_w, max_logo_h), Image.Resampling.LANCZOS
                    )
                    logo_x = width - content_x - logo_rgba.width
                    img.paste(logo_rgba, (logo_x, content_y), mask=logo_rgba)
            except (OSError, ValueError) as exc:
                logger.warning("Failed to composite logo image %s: %s", logo_path, exc)

    # 3. Draw Event Name
    y_offset = content_y
    if event_name:
        draw.text(
            (content_x, y_offset),
            event_name.upper(),
            fill=(56, 189, 248),  # Sky blue accent
        )
        y_offset += int(height * 0.08)

    # 4. Draw Talk Title (wrapped)
    title_text = _wrap_text(title, max_chars=42)
    draw.text(
        (content_x, y_offset),
        title_text,
        fill=(255, 255, 255),  # Pure white
    )
    title_lines = title_text.count("\n") + 1
    y_offset += title_lines * int(height * 0.07) + int(height * 0.05)

    # 5. Draw Speaker Names
    if isinstance(speakers, (list, tuple)):
        speaker_str = ", ".join(speakers)
    else:
        speaker_str = str(speakers)

    if speaker_str:
        draw.text(
            (content_x, y_offset),
            f"Speaker: {speaker_str}",
            fill=(203, 213, 225),  # Light slate
        )
        y_offset += int(height * 0.08)

    # 6. Draw Room & Date
    if room_date:
        draw.text(
            (content_x, y_offset),
            room_date,
            fill=(148, 163, 184),  # Muted slate
        )

    return np.array(img)


def _wrap_text(text: str, max_chars: int = 40) -> str:
    """Wraps text across multiple lines for title display."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph.strip():
            lines.extend(textwrap.wrap(paragraph, width=max_chars))
    return "\n".join(lines)


def _get_audio_samples(
    jingle_path: Path | str | None,
    duration_s: float,
    sample_rate: int = 44100,
) -> np.ndarray:
    """Decodes or synthesizes stereo int16 audio samples of shape (2, N)."""
    total_samples = int(duration_s * sample_rate)

    if jingle_path:
        jp = Path(jingle_path)
        if not jp.is_file():
            raise FileNotFoundError(f"Audio jingle file not found: {jp}")

        samples_left: list[np.ndarray] = []
        samples_right: list[np.ndarray] = []

        with av.open(str(jp)) as container:
            if container.streams.audio:
                for packet in container.demux(container.streams.audio[0]):
                    for frame in packet.decode():
                        arr = frame.to_ndarray()
                        if "s16" in frame.format.name:
                            arr = arr.astype(np.float64) / 32768.0
                        elif "flt" in frame.format.name:
                            arr = arr.astype(np.float64)

                        if arr.ndim == 1:
                            samples_left.append(arr)
                            samples_right.append(arr)
                        elif arr.shape[0] >= 2:
                            samples_left.append(arr[0])
                            samples_right.append(arr[1])
                        else:
                            samples_left.append(arr[0])
                            samples_right.append(arr[0])

        if samples_left:
            left = np.concatenate(samples_left)
            right = np.concatenate(samples_right)

            if len(left) < total_samples:
                repeats = (total_samples // len(left)) + 1
                left = np.tile(left, repeats)[:total_samples]
                right = np.tile(right, repeats)[:total_samples]
            else:
                left = left[:total_samples]
                right = right[:total_samples]

            # Smooth fade out over the last 0.4s
            fade_samples = min(total_samples, int(0.4 * sample_rate))
            if fade_samples > 0:
                fade_curve = np.linspace(1.0, 0.0, fade_samples)
                left[-fade_samples:] *= fade_curve
                right[-fade_samples:] *= fade_curve

            int_left = (np.clip(left, -1.0, 1.0) * 32767).astype(np.int16)
            int_right = (np.clip(right, -1.0, 1.0) * 32767).astype(np.int16)
            return np.vstack([int_left, int_right])

    # Default gentle opening chime (harmonic tone)
    t = np.arange(total_samples, dtype=np.float64) / sample_rate
    audio_sig = 0.25 * np.sin(2 * np.pi * 523.25 * t) * np.exp(-1.2 * t)
    audio_int16 = (np.clip(audio_sig, -1.0, 1.0) * 32767).astype(np.int16)
    return np.vstack([audio_int16, audio_int16])


def generate_intro_clip(
    output_path: Path | str,
    title: str,
    speakers: list[str] | str,
    event_name: str = "",
    room_date: str = "",
    logo_path: Path | str | None = None,
    audio_jingle_path: Path | str | None = None,
    duration_seconds: float = 4.0,
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 24,
) -> None:
    """Generates an opening title slate video clip with synchronized audio.

    Args:
        output_path: Destination path for the rendered intro video.
        title: Talk title.
        speakers: Speaker name or list of speaker names.
        event_name: Name of the event or conference.
        room_date: Track, room, or date metadata string.
        logo_path: Optional path to event logo image.
        audio_jingle_path: Optional path to opening audio jingle media.
        duration_seconds: Desired intro clip duration (default: 4.0s).
        resolution: Target video resolution tuple (width, height) (default: (1920, 1080)).
        fps: Target video framerate (default: 24).

    Raises:
        ValueError: If duration_seconds, fps, or resolution are non-positive.
        FileNotFoundError: If logo_path or audio_jingle_path are specified but not found.
    """
    if duration_seconds <= 0:
        raise ValueError(f"duration_seconds must be positive, got {duration_seconds}")
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    if resolution[0] <= 0 or resolution[1] <= 0:
        raise ValueError(f"resolution must be positive, got {resolution}")

    if logo_path is not None:
        lp = Path(logo_path)
        if not lp.is_file():
            raise FileNotFoundError(f"Logo file not found: {lp}")

    out_path = Path(output_path)

    # storage-boundary-exempt: creating parent directory for pipeline output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Render title slate frame
    slate_ndarray = _create_title_slate_image(
        title=title,
        speakers=speakers,
        event_name=event_name,
        room_date=room_date,
        logo_path=logo_path,
        resolution=resolution,
    )

    # 2. Get audio samples
    sample_rate = 44100
    audio_samples = _get_audio_samples(
        jingle_path=audio_jingle_path,
        duration_s=duration_seconds,
        sample_rate=sample_rate,
    )

    # 3. Encode video and audio with PyAV
    total_video_frames = round(duration_seconds * fps)
    total_audio_samples = audio_samples.shape[1]

    with av.open(str(out_path), mode="w", format="mp4") as out_container:
        # Configure video stream
        out_v = out_container.add_stream(
            "libx264",
            rate=fps,
            options={"crf": "20", "preset": "veryfast"},
        )
        out_v.width = resolution[0]
        out_v.height = resolution[1]
        out_v.pix_fmt = "yuv420p"

        # Configure audio stream
        out_a = out_container.add_stream("aac", rate=sample_rate)
        out_a.layout = "stereo"

        # Encode video frames
        for frame_idx in range(total_video_frames):
            v_frame = av.VideoFrame.from_ndarray(slate_ndarray, format="rgb24")
            v_frame.pts = frame_idx
            v_frame.time_base = Fraction(1, fps)
            for packet in out_v.encode(v_frame):
                out_container.mux(packet)

        for packet in out_v.encode():
            out_container.mux(packet)

        # Encode audio chunks
        chunk_size = 1024
        audio_pts = 0
        for start_idx in range(0, total_audio_samples, chunk_size):
            chunk = audio_samples[:, start_idx : start_idx + chunk_size]
            if chunk.shape[1] == 0:
                continue

            a_frame = av.AudioFrame.from_ndarray(chunk, format="s16p", layout="stereo")
            a_frame.sample_rate = sample_rate
            a_frame.pts = audio_pts
            a_frame.time_base = Fraction(1, sample_rate)
            audio_pts += chunk.shape[1]

            for packet in out_a.encode(a_frame):
                out_container.mux(packet)

        for packet in out_a.encode():
            out_container.mux(packet)
