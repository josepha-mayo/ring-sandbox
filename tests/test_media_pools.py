"""Media resolution: single files, pools, and the fallbacks between them."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from ring_sandbox.world import World, default_world

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = default_world()
    w.media_dir = tmp_path
    return w


@pytest.fixture
def device(world: World):
    return next(iter(world.devices.values()))


def test_falls_back_to_generated_placeholder(world: World, device) -> None:
    content, mime = world.snapshot_bytes(device, 1_700_000_000_000, "jpeg")
    assert mime == "image/png"
    assert content.startswith(PNG_MAGIC)


def test_single_device_file_still_wins(world: World, device, tmp_path: Path) -> None:
    # The pre-existing layout must keep working untouched.
    _write(tmp_path / f"{device.id}.jpg", b"device-still")
    _write(tmp_path / "default.jpg", b"shared-still")
    content, mime = world.snapshot_bytes(device, 1_700_000_000_000, "jpeg")
    assert content == b"device-still"
    assert mime == "image/jpeg"


def test_pool_is_preferred_over_single_file(world: World, device, tmp_path: Path) -> None:
    _write(tmp_path / f"{device.id}.jpg", b"single")
    _write(tmp_path / device.id / "a.jpg", b"pooled-a")
    _write(tmp_path / device.id / "b.jpg", b"pooled-b")
    content, _ = world.snapshot_bytes(device, 1_700_000_000_000, "jpeg")
    assert content in {b"pooled-a", b"pooled-b"}


def test_pool_varies_across_timestamps(world: World, device, tmp_path: Path) -> None:
    # The point of the feature: consecutive events must not all look alike.
    for i in range(8):
        _write(tmp_path / device.id / f"{i}.jpg", f"frame-{i}".encode())
    seen = {
        world.snapshot_bytes(device, 1_700_000_000_000 + offset * 8_000, "jpeg")[0]
        for offset in range(24)
    }
    assert len(seen) > 1


def test_selection_is_deterministic(world: World, device, tmp_path: Path) -> None:
    # Scenarios and recorded fixtures must replay identically.
    for i in range(5):
        _write(tmp_path / device.id / f"{i}.jpg", f"frame-{i}".encode())
    ts = 1_700_000_123_456
    first, _ = world.snapshot_bytes(device, ts, "jpeg")
    for _ in range(5):
        again, _ = world.snapshot_bytes(device, ts, "jpeg")
        assert again == first


def test_default_pool_is_shared_by_every_device(world: World, tmp_path: Path) -> None:
    _write(tmp_path / "default" / "a.jpg", b"shared-a")
    _write(tmp_path / "default" / "b.jpg", b"shared-b")
    for device in world.devices.values():
        content, _ = world.snapshot_bytes(device, 1_700_000_000_000, "jpeg")
        assert content in {b"shared-a", b"shared-b"}


def test_device_pool_beats_default_pool(world: World, device, tmp_path: Path) -> None:
    _write(tmp_path / "default" / "shared.jpg", b"shared")
    _write(tmp_path / device.id / "own.jpg", b"own")
    content, _ = world.snapshot_bytes(device, 1_700_000_000_000, "jpeg")
    assert content == b"own"


def test_mime_follows_the_chosen_file(world: World, device, tmp_path: Path) -> None:
    _write(tmp_path / device.id / "only.png", b"a-png")
    _, mime = world.snapshot_bytes(device, 1_700_000_000_000, "jpeg")
    assert mime == "image/png"


def test_non_image_files_are_ignored(world: World, device, tmp_path: Path) -> None:
    _write(tmp_path / device.id / "notes.txt", b"not an image")
    _write(tmp_path / device.id / "frame.jpg", b"an image")
    content, _ = world.snapshot_bytes(device, 1_700_000_000_000, "jpeg")
    assert content == b"an image"


def test_empty_pool_directory_falls_through(world: World, device, tmp_path: Path) -> None:
    (tmp_path / device.id).mkdir(parents=True)
    _write(tmp_path / "default.jpg", b"shared-still")
    content, _ = world.snapshot_bytes(device, 1_700_000_000_000, "jpeg")
    assert content == b"shared-still"


def test_clip_pool_and_backwards_compatible_signature(world: World, device, tmp_path: Path) -> None:
    _write(tmp_path / device.id / "a.mp4", b"clip-a")
    _write(tmp_path / device.id / "b.mp4", b"clip-b")
    # Callers that pass no timestamp must keep working.
    assert world.clip_bytes(device) in {b"clip-a", b"clip-b"}
    assert world.clip_bytes(device, 1_700_000_000_000) in {b"clip-a", b"clip-b"}


def test_clip_single_file_still_works(world: World, device, tmp_path: Path) -> None:
    _write(tmp_path / f"{device.id}.mp4", b"single-clip")
    assert world.clip_bytes(device) == b"single-clip"


def test_clip_falls_back_to_minimal_mp4(world: World, device) -> None:
    data = world.clip_bytes(device)
    assert data[4:8] == b"ftyp"
    assert struct.unpack(">I", data[:4])[0] == 20
