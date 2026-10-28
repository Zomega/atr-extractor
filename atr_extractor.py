#!/usr/bin/env python3
"""
ATR Extractor (`atr-extractor`)
===============================

Standalone, cross-platform command-line tool and library for extracting,
decoding, and visualizing `.ATR` (`ATR11`) sprite descriptors and `.RSC`
(`BWRF10`) resource bundles from Knowledge Adventure's JumpStart 3rd Grade
(1996) and related titles.

Supported Inputs (`--input` / `-i`):
  - ISO 9660 / UDF disc image (`.iso` / `.bin`) via `pycdlib`.
  - `.RSC` (`BWRF10`) resource archive file.
  - `.ATR` (`ATR11`) standalone sprite file.
  - Directory containing `.ATR` files (plus `.BMP`/`.PNG` palette bitmaps),
    `.RSC` archives, or subdirectories of unpacked bundles.

Generated Outputs (`--output` / `-o`, default: `output/`):
  - Individual decoded RGBA frame PNGs (`<bundle>/<actor>/frames/001.png`)
  - Per-actor packed sprite sheet PNG (`<bundle>/<actor>/sheet.png`)
  - Per-sequence animated GIFs (`<bundle>/<actor>/animations/seq_00.gif`)
  - Per-actor JSON metadata (`<bundle>/<actor>/metadata.json`)
  - Bundle-wide packed sprite atlas (`<bundle>/<bundle>_atlas.png` + `.json`)
  - Bundle room scene composite (`<bundle>/<bundle>_scene_composite.png`)
  - Bundle `manifest.json` and top-level `index.json`
"""

from __future__ import annotations

import argparse
import io
import json
import math
import posixpath
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit(
        "Error: Pillow is required to run atr-extractor.\n"
        "Install dependencies with: pip install -r requirements.txt"
    )

try:
    import pycdlib
except ImportError:  # pragma: no cover
    pycdlib = None

__version__ = "1.0.0"

RSC_MAGIC = b"BWRF"
ATR_MAGIC = b"ATR"
TRANSPARENT_KEY = 0xFF
LZW_RESET_CODE = 4000
LZW_MAX_DICT_CODE = 3000


# ============================================================================
# Binary Reader Utility
# ============================================================================


class BinaryReader:
    """Bounds-checked little-endian binary stream reader."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    @property
    def remaining(self) -> int:
        return max(0, len(self.data) - self.pos)

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError(
                f"Unexpected EOF at offset 0x{self.pos:X} (wanted {size} bytes, "
                f"only {len(self.data) - self.pos} remaining)"
            )
        chunk = self.data[self.pos : self.pos + size]
        self.pos += size
        return chunk

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def cstr(self, width: int) -> str:
        raw = self.take(width)
        return raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()


# ============================================================================
# Data Models
# ============================================================================


@dataclass
class AtrImageRecord:
    """Single sprite image node (`CTrajectoryNode`, 0x20 byte header + LZW stream)."""

    index: int  # 1-based index
    x1: int
    y1: int
    x2: int
    y2: int
    f14: int
    hot_x: int
    hot_y: int
    compressed_size: int
    data_offset: int
    compressed_data: bytes = field(repr=False)

    @property
    def width(self) -> int:
        return self.x2 - self.x1 + 1

    @property
    def height(self) -> int:
        return self.y2 - self.y1 + 1


@dataclass
class AtrSequenceEntry:
    """Single keyframe entry (`CTrajectoryFrame`, 0x70 / 112 bytes on disk)."""

    image: int  # 1-based index into actor/parent image table (0 = delay/blank)
    duration_ms: int
    hold_delay_ms: int
    dx: int
    dy: int
    sound_cue: str
    cue_params: List[int]
    action_flags: int
    f38: int
    f3a: int
    cue2: str
    f4c: int
    f50: int
    cue3: str
    f64: int
    f68: int
    target_atr: str
    cache_flags: int

    def to_metadata(self) -> Dict[str, Any]:
        cues = [
            c for c in (self.sound_cue, self.cue2, self.cue3, self.target_atr) if c
        ]
        return {
            "image": self.image,
            "duration_ms": self.duration_ms,
            "hold_delay_ms": self.hold_delay_ms,
            "offset": [self.dx, self.dy],
            "sound_cue": self.sound_cue or None,
            "cue_params": self.cue_params,
            "action_flags": self.action_flags,
            "f38": self.f38,
            "f3a": self.f3a,
            "cue2": self.cue2 or None,
            "f4c": self.f4c,
            "f50": self.f50,
            "cue3": self.cue3 or None,
            "f64": self.f64,
            "f68": self.f68,
            "target_atr": self.target_atr or None,
            "cache_flags": self.cache_flags,
            "active_cues": cues,
        }


@dataclass
class AtrSequence:
    """Animation trajectory sequence (`CTrajectoryPath`)."""

    index: int
    origin_x: int
    origin_y: int
    anchor_mode_raw: int
    auto_anchor: bool
    state_b: int
    state_c: int
    step_duration_ms: int
    cache_invalidation_raw: int
    cache_invalidation: bool
    entries: List[AtrSequenceEntry]

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "origin": [self.origin_x, self.origin_y],
            "anchor_mode_raw": self.anchor_mode_raw,
            "auto_anchor": self.auto_anchor,
            "state_b": self.state_b,
            "state_c": self.state_c,
            "step_duration_ms": self.step_duration_ms,
            "cache_invalidation_raw": self.cache_invalidation_raw,
            "cache_invalidation": self.cache_invalidation,
            "entry_count": len(self.entries),
            "entries": [e.to_metadata() for e in self.entries],
        }


@dataclass
class AtrFile:
    """Parsed `ATR11` sprite descriptor (`CTrajectoryActor`)."""

    filename: str
    version: str
    name: str
    bitmap: str
    parent: str
    asset_id: int
    flags: int
    width: int
    height: int
    declared_image_count: int
    images: List[AtrImageRecord]
    sequences: List[AtrSequence]
    trailing_bytes: int

    @property
    def flag_movement_callback(self) -> bool:
        return bool(self.flags & 0x01)

    @property
    def flag_load_time_decompressed(self) -> bool:
        return bool(self.flags & 0x04)

    @property
    def flag_no_scratch_buffer(self) -> bool:
        return bool(self.flags & 0x10)

    @property
    def is_dummy(self) -> bool:
        return self.bitmap.lower() == "dummy.bmp"


# ============================================================================
# ATR11 Parser & Custom 12-Bit Transparency-Segregated LZW Decoder
# ============================================================================


def parse_atr_entry(r: BinaryReader) -> AtrSequenceEntry:
    """Parse one 112-byte (0x70) sequence entry (`CTrajectoryFrame`)."""
    return AtrSequenceEntry(
        image=r.u32(),
        duration_ms=r.u16(),
        hold_delay_ms=r.u16(),
        dx=r.i32(),
        dy=r.i32(),
        sound_cue=r.cstr(16),
        cue_params=[r.u16() for _ in range(4)],
        action_flags=r.u32(),
        f38=r.u16(),
        f3a=r.u16(),
        cue2=r.cstr(16),
        f4c=r.u16(),
        f50=r.u32(),
        cue3=r.cstr(16),
        f64=r.u16(),
        f68=r.u32(),
        target_atr=r.cstr(16),
        cache_flags=r.u32(),
    )


def parse_atr_image(r: BinaryReader, index: int) -> AtrImageRecord:
    """Parse one `IMAGE` record (`CTrajectoryNode::ReadFrameBitmap`)."""
    x1, y1, x2, y2 = r.i32(), r.i32(), r.i32(), r.i32()
    f14 = r.u32()
    raw_hx, raw_hy = r.u32(), r.u32()
    hot_x = -1 if raw_hx == 0xFFFFFFFF else struct.unpack("<i", struct.pack("<I", raw_hx))[0]
    hot_y = -1 if raw_hy == 0xFFFFFFFF else struct.unpack("<i", struct.pack("<I", raw_hy))[0]
    comp_size = r.u32()
    data_off = r.pos
    comp_data = r.take(comp_size)
    return AtrImageRecord(
        index=index,
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        f14=f14,
        hot_x=hot_x,
        hot_y=hot_y,
        compressed_size=comp_size,
        data_offset=data_off,
        compressed_data=comp_data,
    )


def parse_atr(data: bytes, filename: str = "<memory>") -> AtrFile:
    """Parse an `ATR11` binary blob into an `AtrFile` structure."""
    r = BinaryReader(data)
    magic = r.take(3)
    if magic != ATR_MAGIC:
        raise ValueError(f"Invalid ATR magic {magic!r} (expected b'ATR')")
    version = r.take(2).decode("latin-1", errors="replace")
    name = r.cstr(16)
    bitmap = r.cstr(16)
    parent = r.cstr(16)
    asset_id = r.u32()
    flags = r.u32()
    width = r.u32()
    height = r.u32()
    n_images = r.u32()

    images: List[AtrImageRecord] = []
    if not parent:
        for i in range(1, n_images + 1):
            images.append(parse_atr_image(r, i))

    n_seq = r.u32()
    sequences: List[AtrSequence] = []
    for seq_idx in range(n_seq):
        ox, oy = r.i32(), r.i32()
        anchor_raw = r.u16()
        state_b, state_c = r.u16(), r.u16()
        step_duration_ms = r.u32()
        cache_inv_raw = r.u32()
        n_entries = r.u32()
        entries = [parse_atr_entry(r) for _ in range(n_entries)]
        sequences.append(
            AtrSequence(
                index=seq_idx,
                origin_x=ox,
                origin_y=oy,
                anchor_mode_raw=anchor_raw,
                auto_anchor=(anchor_raw == 0),
                state_b=state_b,
                state_c=state_c,
                step_duration_ms=step_duration_ms,
                cache_invalidation_raw=cache_inv_raw,
                cache_invalidation=(cache_inv_raw != 0),
                entries=entries,
            )
        )

    return AtrFile(
        filename=filename,
        version=version,
        name=name,
        bitmap=bitmap,
        parent=parent,
        asset_id=asset_id,
        flags=flags,
        width=width,
        height=height,
        declared_image_count=n_images,
        images=images,
        sequences=sequences,
        trailing_bytes=r.remaining,
    )


def lzw_decode(
    data: bytes,
    transparent_key: int = TRANSPARENT_KEY,
    limit: Optional[int] = None,
) -> bytes:
    """
    Decode Knowledge Adventure's custom 12-bit MSB-first transparency-segregated
    LZW stream (`LZW_DecompressBlock` @ 0x0048B510).

    Note: Standard GIF/TIFF LZW libraries cannot decode this stream because:
      1. Dictionary additions only happen when `(prev[0] == 0xFF) == (cur[0] == 0xFF)`
         (transparent runs are segregated from opaque pixels).
      2. Code `4000` resets the dictionary while retaining the active `prev` entry,
         `next + 1` is dynamic EOI, and the dictionary caps at `3000`.
    """
    total_bits = len(data) * 8
    num_codes = total_bits // 12
    if num_codes == 0:
        return b""

    bit_int = int.from_bytes(data, "big")
    codes = [(bit_int >> (total_bits - 12 * (i + 1))) & 0xFFF for i in range(num_codes)]

    def init_dict() -> Tuple[Dict[int, bytes], int]:
        return {i: bytes([i]) for i in range(256)}, 256

    table, nxt = init_dict()
    out = bytearray()
    prev = codes[0]

    for cur in codes[1:]:
        if limit is not None and len(out) >= limit:
            break
        if cur == nxt:
            p = table[prev]
            out += p
            if nxt != LZW_MAX_DICT_CODE:
                table[nxt] = p + p[:1]
                nxt += 1
            prev = cur
        elif cur == nxt + 1:
            break
        elif cur == LZW_RESET_CODE:
            keep = table[prev]
            table, nxt = init_dict()
            table[prev] = keep
        else:
            p = table[prev]
            out += p
            if cur not in table:
                raise ValueError(f"Corrupt LZW code {cur} (next={nxt})")
            c = table[cur]
            if (p[0] == transparent_key) == (c[0] == transparent_key) and nxt != LZW_MAX_DICT_CODE:
                table[nxt] = p + c[:1]
                nxt += 1
            prev = cur

    out += table[prev]
    return bytes(out)


# ============================================================================
# BWRF10 (.RSC) Archive & ISO (`pycdlib`) Discovery
# ============================================================================


@dataclass
class RscEntry:
    name: str
    offset: int
    size: int


def parse_rsc_directory(data: bytes) -> Tuple[str, List[RscEntry]]:
    """Parse a `BWRF10` resource archive header. Returns `(version_str, entries)`."""
    if len(data) < 10 or data[:4] != RSC_MAGIC:
        raise ValueError("Not a valid BWRF resource bundle (missing 'BWRF' magic)")
    version = data[4:6].decode("latin-1", errors="replace")
    (count,) = struct.unpack_from("<I", data, 6)
    entries: List[RscEntry] = []
    pos = 10
    for _ in range(count):
        if pos + 20 > len(data):
            raise ValueError("Truncated BWRF directory table")
        raw_name = data[pos : pos + 12]
        name = raw_name.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
        offset, size = struct.unpack_from("<II", data, pos + 12)
        entries.append(RscEntry(name=name, offset=offset, size=size))
        pos += 20
    return version, entries


@dataclass
class VirtualBundle:
    """Logical group of ATR actors and palette bitmaps from an RSC, ISO, or folder."""

    name: str
    source_description: str
    atr_blobs: Dict[str, Tuple[str, bytes]] = field(default_factory=dict)
    bitmap_blobs: Dict[str, Tuple[str, bytes]] = field(default_factory=dict)
    other_files: List[str] = field(default_factory=list)


def load_bundle_from_rsc_bytes(
    rsc_name: str, data: bytes, source_desc: str
) -> VirtualBundle:
    """Unpack a `BWRF10` `.RSC` byte buffer into a `VirtualBundle`."""
    bundle_id = Path(rsc_name).stem.upper()
    _, entries = parse_rsc_directory(data)
    bundle = VirtualBundle(name=bundle_id, source_description=source_desc)
    for entry in entries:
        blob = data[entry.offset : entry.offset + entry.size]
        ext = Path(entry.name).suffix.lower()
        stem_lower = Path(entry.name).stem.lower()
        if ext == ".atr":
            bundle.atr_blobs[stem_lower] = (entry.name, blob)
        elif ext in (".bmp", ".png"):
            bundle.bitmap_blobs[stem_lower] = (entry.name, blob)
        else:
            bundle.other_files.append(entry.name)
    return bundle


def discover_bundles_from_iso(
    iso_path: Path, bundle_filter: Optional[str] = None
) -> List[VirtualBundle]:
    """
    Read `.RSC` bundles (and any loose `.ATR`/`.BMP` files) from an ISO image
    using `pycdlib`.
    """
    if pycdlib is None:
        raise RuntimeError(
            "pycdlib is required to read .ISO images. Install it with: pip install pycdlib"
        )

    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    try:
        path_key = "iso_path"
        try:
            iso.get_record(iso_path="/")
        except Exception:
            if iso.has_udf():
                path_key = "udf_path"
            elif iso.has_joliet():
                path_key = "joliet_path"
            elif iso.has_rock_ridge():
                path_key = "rr_path"

        rsc_candidates: Dict[str, str] = {}  # bundle_stem_upper -> iso_path
        loose_atrs: Dict[str, List[Tuple[str, str]]] = {}
        loose_bmps: Dict[str, List[Tuple[str, str]]] = {}

        for root, _, files in iso.walk(**{path_key: "/"}):
            for fname in files:
                full_iso_path = posixpath.join(root, fname)
                clean_name = fname.split(";", 1)[0].rstrip(".")
                ext = Path(clean_name).suffix.lower()
                stem_upper = Path(clean_name).stem.upper()

                if ext == ".rsc":
                    if bundle_filter and not stem_upper.startswith(bundle_filter.upper()):
                        continue
                    # Prefer 32BIT over 16BIT if both exist on the disc
                    existing = rsc_candidates.get(stem_upper)
                    if existing is None or "32BIT" in full_iso_path.upper():
                        rsc_candidates[stem_upper] = full_iso_path
                elif ext == ".atr":
                    parent_dir = posixpath.basename(root) or "DISC_ROOT"
                    loose_atrs.setdefault(parent_dir.upper(), []).append(
                        (clean_name, full_iso_path)
                    )
                elif ext in (".bmp", ".png"):
                    parent_dir = posixpath.basename(root) or "DISC_ROOT"
                    loose_bmps.setdefault(parent_dir.upper(), []).append(
                        (clean_name, full_iso_path)
                    )

        def read_iso_file(p: str) -> bytes:
            buf = io.BytesIO()
            iso.get_file_from_iso_fp(buf, **{path_key: p})
            return buf.getvalue()

        bundles: Dict[str, VirtualBundle] = {}
        for stem_upper, full_iso_path in sorted(rsc_candidates.items()):
            blob = read_iso_file(full_iso_path)
            if blob[:4] == RSC_MAGIC:
                clean_disp = full_iso_path.split(";", 1)[0]
                vb = load_bundle_from_rsc_bytes(
                    f"{stem_upper}.RSC", blob, f"{iso_path.name}:{clean_disp}"
                )
                if vb.atr_blobs:
                    bundles[vb.name] = vb

        for folder_name, atr_items in sorted(loose_atrs.items()):
            if bundle_filter and not folder_name.startswith(bundle_filter.upper()):
                continue
            if folder_name in bundles:
                continue
            vb = VirtualBundle(
                name=folder_name, source_description=f"{iso_path.name}:{folder_name}"
            )
            for clean_name, full_iso_path in atr_items:
                vb.atr_blobs[Path(clean_name).stem.lower()] = (
                    clean_name,
                    read_iso_file(full_iso_path),
                )
            for clean_name, full_iso_path in loose_bmps.get(folder_name, []):
                vb.bitmap_blobs[Path(clean_name).stem.lower()] = (
                    clean_name,
                    read_iso_file(full_iso_path),
                )
            bundles[folder_name] = vb

        return [bundles[k] for k in sorted(bundles.keys())]
    finally:
        iso.close()


def discover_bundles_from_path(
    input_path: Path, bundle_filter: Optional[str] = None
) -> List[VirtualBundle]:
    """
    Auto-detect input type (`ISO`, `.RSC`, `.ATR`, or directory of `.ATR`s /
    `.RSC`s / bundle folders) and return a list of `VirtualBundle` objects.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    if input_path.is_file():
        ext = input_path.suffix.lower()
        if ext in (".iso", ".bin", ".img"):
            return discover_bundles_from_iso(input_path, bundle_filter=bundle_filter)
        if ext == ".rsc":
            data = input_path.read_bytes()
            return [load_bundle_from_rsc_bytes(input_path.name, data, str(input_path))]
        if ext == ".atr":
            parent_dir = input_path.parent
            vb = VirtualBundle(
                name=parent_dir.name.upper() or "SINGLE",
                source_description=str(input_path),
            )
            vb.atr_blobs[input_path.stem.lower()] = (
                input_path.name,
                input_path.read_bytes(),
            )
            for sib in sorted(parent_dir.iterdir()):
                if sib.is_file():
                    sext = sib.suffix.lower()
                    if sext in (".bmp", ".png"):
                        vb.bitmap_blobs[sib.stem.lower()] = (sib.name, sib.read_bytes())
                    elif sext == ".atr" and sib.stem.lower() not in vb.atr_blobs:
                        vb.atr_blobs[sib.stem.lower()] = (sib.name, sib.read_bytes())
            return [vb]
        raise ValueError(f"Unsupported input file extension: {input_path.name}")

    bundles: Dict[str, VirtualBundle] = {}

    # 1. Check for .RSC files recursively
    rsc_files = sorted(
        f for f in input_path.rglob("*") if f.is_file() and f.suffix.lower() == ".rsc"
    )
    for rsc_file in rsc_files:
        if bundle_filter and not rsc_file.stem.upper().startswith(bundle_filter.upper()):
            continue
        try:
            data = rsc_file.read_bytes()
            if data[:4] == RSC_MAGIC:
                vb = load_bundle_from_rsc_bytes(rsc_file.name, data, str(rsc_file))
                if vb.atr_blobs:
                    bundles[vb.name] = vb
        except Exception as exc:
            print(f"[!] Warning: skipping {rsc_file}: {exc}", file=sys.stderr)

    # 2. Check for directories containing .ATR files
    atr_dirs: Dict[Path, List[Path]] = {}
    for f in sorted(input_path.rglob("*")):
        if f.is_file() and f.suffix.lower() == ".atr":
            atr_dirs.setdefault(f.parent, []).append(f)

    for folder, atr_files in sorted(atr_dirs.items()):
        bundle_name = folder.name.upper()
        if bundle_filter and not bundle_name.startswith(bundle_filter.upper()):
            continue
        if bundle_name in bundles:
            continue
        vb = VirtualBundle(name=bundle_name, source_description=str(folder))
        for af in atr_files:
            vb.atr_blobs[af.stem.lower()] = (af.name, af.read_bytes())
        for bf in sorted(folder.iterdir()):
            if bf.is_file() and bf.suffix.lower() in (".bmp", ".png"):
                vb.bitmap_blobs[bf.stem.lower()] = (bf.name, bf.read_bytes())
        bundles[bundle_name] = vb

    return [bundles[k] for k in sorted(bundles.keys())]


# ============================================================================
# Image Decoding, Shelf Packing, Actor Sheets, Composites, and Animations
# ============================================================================


def extract_palette_and_image(
    blob: bytes,
) -> Tuple[Optional[List[int]], Optional[Image.Image]]:
    """Load an 8-bit palette (768 ints `[R,G,B,...]`) and PIL Image from BMP/PNG bytes."""
    try:
        img = Image.open(io.BytesIO(blob))
        img.load()
        return img.getpalette(), img
    except Exception:
        return None, None


def decode_frame_images(
    im_rec: AtrImageRecord,
    palette: Optional[List[int]],
    transparent_key: int = TRANSPARENT_KEY,
) -> Tuple[Image.Image, Image.Image]:
    """
    Decode an `AtrImageRecord` LZW stream into both an 8-bit indexed (`"P"`)
    PIL Image and a 32-bit `"RGBA"` PIL Image with index `0xFF` transparent.
    """
    w, h = im_rec.width, im_rec.height
    if w <= 0 or h <= 0 or w > 4096 or h > 4096:
        raise ValueError(f"Invalid frame dimensions {w}x{h}")

    expected = w * h
    px = lzw_decode(im_rec.compressed_data, transparent_key=transparent_key)
    if len(px) > expected:
        px = px[:expected]
    elif len(px) < expected:
        px = px + (bytes([transparent_key]) * (expected - len(px)))

    img_p = Image.frombytes("P", (w, h), px)
    if palette:
        img_p.putpalette(palette)
    else:
        img_p.putpalette([i for i in range(256) for _ in range(3)])

    rgba = img_p.convert("RGBA")
    alpha_bytes = bytes(0 if b == transparent_key else 255 for b in px)
    alpha = Image.frombytes("L", (w, h), alpha_bytes)
    rgba.putalpha(alpha)
    return img_p, rgba


def pack_shelf(
    items: List[Dict[str, Any]], max_width: int = 2048, padding: int = 4
) -> Tuple[int, int, List[Dict[str, Any]]]:
    """Shelf bin-packer for building actor sprite sheets and bundle atlases."""
    if not items:
        return 1, 1, []

    total_area = sum((it["w"] + padding) * (it["h"] + padding) for it in items)
    max_item_w = max(it["w"] + padding * 2 for it in items)
    target_w = max(
        max_item_w,
        min(max_width, int(math.sqrt(total_area) * 1.25) + padding * 2),
    )

    cur_x = padding
    cur_y = padding
    row_h = 0
    used_w = 0
    placements: List[Dict[str, Any]] = []

    for it in items:
        w, h = it["w"], it["h"]
        if cur_x + w + padding > target_w and cur_x > padding:
            cur_x = padding
            cur_y += row_h + padding
            row_h = 0

        placements.append({**it, "sheet_x": cur_x, "sheet_y": cur_y})
        cur_x += w + padding
        row_h = max(row_h, h)
        used_w = max(used_w, cur_x)

    sheet_w = max(1, used_w + padding)
    sheet_h = max(1, cur_y + row_h + padding)
    return sheet_w, sheet_h, placements


def render_actor_sheet(
    decoded_frames: List[Dict[str, Any]], max_width: int = 1024, padding: int = 4
) -> Tuple[Optional[Image.Image], List[Dict[str, Any]]]:
    """Build a packed RGBA sprite sheet for a single ATR actor."""
    if not decoded_frames:
        return None, []
    sheet_w, sheet_h, placements = pack_shelf(
        decoded_frames, max_width=max_width, padding=padding
    )
    sheet = Image.new("RGBA", (sheet_w, sheet_h), (0, 0, 0, 0))
    coords: List[Dict[str, Any]] = []
    for p in placements:
        sheet.paste(p["rgba"], (p["sheet_x"], p["sheet_y"]), p["rgba"])
        coords.append(
            {
                "index": p["index"],
                "sheet_rect": [p["sheet_x"], p["sheet_y"], p["w"], p["h"]],
                "scene_rect": p["rect"],
            }
        )
    return sheet, coords


def render_sequence_gif(
    seq: AtrSequence,
    frames_by_idx: Dict[int, Dict[str, Any]],
    out_path: Path,
) -> bool:
    """
    Render an animated GIF for one `AtrSequence` using screen-aligned frame
    bounding boxes (`[x1, y1, x2, y2]`) so multi-frame animations stay registered.
    """
    valid_entries = [e for e in seq.entries if e.image in frames_by_idx]
    if not valid_entries:
        return False

    min_x = min(frames_by_idx[e.image]["rect"][0] for e in valid_entries)
    min_y = min(frames_by_idx[e.image]["rect"][1] for e in valid_entries)
    max_x = max(frames_by_idx[e.image]["rect"][2] for e in valid_entries)
    max_y = max(frames_by_idx[e.image]["rect"][3] for e in valid_entries)

    canvas_w = max(1, max_x - min_x + 1)
    canvas_h = max(1, max_y - min_y + 1)
    if canvas_w > 2048 or canvas_h > 2048:
        return False

    gif_frames: List[Image.Image] = []
    durations: List[int] = []

    for e in seq.entries:
        dur = e.duration_ms or e.hold_delay_ms or seq.step_duration_ms or 120
        dur = max(20, min(dur, 5000))
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        if e.image in frames_by_idx:
            fr = frames_by_idx[e.image]
            fx1, fy1, _, _ = fr["rect"]
            canvas.paste(fr["rgba"], (fx1 - min_x, fy1 - min_y), fr["rgba"])
        gif_frames.append(canvas)
        durations.append(dur)

    if not gif_frames:
        return False

    out_path.parent.mkdir(parents=True, exist_ok=True)
    gif_frames[0].save(
        out_path,
        format="GIF",
        save_all=True,
        append_images=gif_frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    return True


# ============================================================================
# Main Extraction Pipeline
# ============================================================================


@dataclass
class ExtractionOptions:
    output_dir: Path = Path("output")
    export_frames: bool = True
    export_actor_sheets: bool = True
    export_animations: bool = True
    export_bundle_atlas: bool = True
    export_scene_composite: bool = True
    export_metadata: bool = True
    include_dummies: bool = False
    max_atlas_width: int = 2048
    bundle_filter: Optional[str] = None
    actor_filter: Optional[str] = None
    inspect_only: bool = False


def resolve_palette(
    bitmap_name: str,
    current_bundle: VirtualBundle,
    all_bundles: List[VirtualBundle],
    palette_cache: Dict[Tuple[str, str], Tuple[Optional[List[int]], Optional[str]]],
) -> Tuple[Optional[List[int]], Optional[str]]:
    """Find the 256-color palette for `bitmap_name` across the current or sibling bundles."""
    stem = Path(bitmap_name).stem.lower()
    cache_key = (current_bundle.name, stem)
    if cache_key in palette_cache:
        return palette_cache[cache_key]

    for b in [current_bundle] + all_bundles:
        if stem in b.bitmap_blobs:
            fn, blob = b.bitmap_blobs[stem]
            pal, _ = extract_palette_and_image(blob)
            if pal:
                src_desc = f"{b.name}/{fn}"
                palette_cache[cache_key] = (pal, src_desc)
                return pal, src_desc

    palette_cache[cache_key] = (None, None)
    return None, None


def process_bundle(
    bundle: VirtualBundle,
    all_bundles: List[VirtualBundle],
    opts: ExtractionOptions,
    palette_cache: Dict[Tuple[str, str], Tuple[Optional[List[int]], Optional[str]]],
) -> Dict[str, Any]:
    """Process all ATR actors inside a `VirtualBundle` and write requested outputs."""
    bundle_out = opts.output_dir / bundle.name
    if not opts.inspect_only:
        bundle_out.mkdir(parents=True, exist_ok=True)

    # 1. Parse all ATR headers/records in the bundle
    parsed_atrs: Dict[str, Tuple[str, Optional[AtrFile], Optional[str]]] = {}
    for actor_key, (fn, blob) in sorted(bundle.atr_blobs.items()):
        try:
            atr = parse_atr(blob, filename=fn)
            parsed_atrs[actor_key] = (fn, atr, None)
        except Exception as exc:
            parsed_atrs[actor_key] = (fn, None, str(exc))

    def matches_actor(actor_key: str, atr_obj: Optional[AtrFile]) -> bool:
        if not opts.actor_filter:
            return True
        q = opts.actor_filter.lower()
        return q in actor_key.lower() or (
            atr_obj is not None and q in atr_obj.name.lower()
        )

    # Determine which actors actually need pixel decoding
    needed_frame_owners: set[str] = set()
    for actor_key, (fn, atr, _) in parsed_atrs.items():
        if atr is None or not matches_actor(actor_key, atr):
            continue
        if atr.parent:
            needed_frame_owners.add(Path(atr.parent).stem.lower())
        else:
            needed_frame_owners.add(actor_key)

    # 2. Decode owned frame images for needed parent/standalone ATRs
    actor_decoded_frames: Dict[str, Dict[int, Dict[str, Any]]] = {}
    actor_palette_sources: Dict[str, Optional[str]] = {}
    total_frames_decoded = 0
    total_frame_errors = 0

    for actor_key, (fn, atr, _) in parsed_atrs.items():
        if atr is None or not atr.images or actor_key not in needed_frame_owners:
            continue
        pal, pal_src = resolve_palette(atr.bitmap, bundle, all_bundles, palette_cache)
        actor_palette_sources[actor_key] = pal_src
        frames_map: Dict[int, Dict[str, Any]] = {}
        for im_rec in atr.images:
            try:
                if opts.inspect_only:
                    # Fast LZW validation without PIL image construction in inspect mode
                    expected = im_rec.width * im_rec.height
                    lzw_decode(im_rec.compressed_data, limit=expected)
                    img_p, img_rgba = None, None
                else:
                    img_p, img_rgba = decode_frame_images(im_rec, pal)
                frames_map[im_rec.index] = {
                    "index": im_rec.index,
                    "rect": [im_rec.x1, im_rec.y1, im_rec.x2, im_rec.y2],
                    "w": im_rec.width,
                    "h": im_rec.height,
                    "hotspot": [im_rec.hot_x, im_rec.hot_y],
                    "f14": im_rec.f14,
                    "compressed_size": im_rec.compressed_size,
                    "img_p": img_p,
                    "rgba": img_rgba,
                    "error": None,
                }
                total_frames_decoded += 1
            except Exception as exc:
                frames_map[im_rec.index] = {
                    "index": im_rec.index,
                    "rect": [im_rec.x1, im_rec.y1, im_rec.x2, im_rec.y2],
                    "w": im_rec.width,
                    "h": im_rec.height,
                    "hotspot": [im_rec.hot_x, im_rec.hot_y],
                    "f14": im_rec.f14,
                    "compressed_size": im_rec.compressed_size,
                    "img_p": None,
                    "rgba": None,
                    "error": str(exc),
                }
                total_frame_errors += 1
        actor_decoded_frames[actor_key] = frames_map

    # 3. Export per-actor outputs (frames, actor sheets, sequence GIFs, metadata.json)
    bundle_manifest_actors: Dict[str, Any] = {}
    all_bundle_atlas_frames: List[Dict[str, Any]] = []
    sheets_written = 0
    gifs_written = 0

    for actor_key, (fn, atr, parse_err) in sorted(parsed_atrs.items()):
        if not matches_actor(actor_key, atr):
            continue

        actor_dir = bundle_out / actor_key

        if parse_err or atr is None:
            err_meta = {"file": fn, "actor_key": actor_key, "error": parse_err}
            bundle_manifest_actors[actor_key] = err_meta
            if not opts.inspect_only and opts.export_metadata:
                actor_dir.mkdir(parents=True, exist_ok=True)
                (actor_dir / "metadata.json").write_text(
                    json.dumps(err_meta, indent=2), encoding="utf-8"
                )
            continue

        parent_key = Path(atr.parent).stem.lower() if atr.parent else None
        own_frames_map = actor_decoded_frames.get(actor_key, {})
        effective_frames_map = (
            actor_decoded_frames.get(parent_key, {}) if parent_key else own_frames_map
        )
        parent_resolved = bool(parent_key and parent_key in actor_decoded_frames)

        if not opts.inspect_only:
            actor_dir.mkdir(parents=True, exist_ok=True)

        # 3a. Save individual frame PNGs
        frame_meta_list: List[Dict[str, Any]] = []
        valid_own_frames: List[Dict[str, Any]] = []
        for idx, fr in sorted(own_frames_map.items()):
            f_entry = {
                "index": idx,
                "rect": fr["rect"],
                "width": fr["w"],
                "height": fr["h"],
                "hotspot": fr["hotspot"],
                "f14": fr["f14"],
                "compressed_size": fr["compressed_size"],
            }
            if fr["error"]:
                f_entry["error"] = fr["error"]
            elif fr["rgba"] is not None:
                valid_own_frames.append(fr)
                if not opts.inspect_only and opts.export_frames:
                    frames_dir = actor_dir / "frames"
                    frames_dir.mkdir(parents=True, exist_ok=True)
                    frame_rel = f"frames/{idx:03d}.png"
                    fr["rgba"].save(
                        actor_dir / frame_rel, format="PNG", optimize=True
                    )
                    f_entry["file"] = frame_rel

                if not atr.is_dummy or opts.include_dummies:
                    all_bundle_atlas_frames.append(
                        {
                            "actor_key": actor_key,
                            "actor_name": atr.name,
                            "atr_file": fn,
                            "bitmap": atr.bitmap,
                            "frame_index": idx,
                            "w": fr["w"],
                            "h": fr["h"],
                            "rect": fr["rect"],
                            "hotspot": fr["hotspot"],
                            "rgba": fr["rgba"],
                        }
                    )
            frame_meta_list.append(f_entry)

        # 3b. Save per-actor sprite sheet PNG
        sheet_file_rel: Optional[str] = None
        sheet_placements_meta: List[Dict[str, Any]] = []
        frames_for_sheet = valid_own_frames
        if not frames_for_sheet and parent_resolved:
            used_indices = sorted(
                {
                    e.image
                    for s in atr.sequences
                    for e in s.entries
                    if e.image in effective_frames_map
                    and effective_frames_map[e.image]["rgba"] is not None
                }
            )
            frames_for_sheet = [effective_frames_map[i] for i in used_indices]

        if not opts.inspect_only and opts.export_actor_sheets and frames_for_sheet:
            sheet_img, sheet_placements_meta = render_actor_sheet(frames_for_sheet)
            if sheet_img is not None:
                sheet_file_rel = "sheet.png"
                sheet_img.save(actor_dir / sheet_file_rel, format="PNG", optimize=True)
                sheets_written += 1

        # 3c. Save per-sequence animated GIFs
        seq_meta_list: List[Dict[str, Any]] = []
        valid_effective_map = {
            k: v for k, v in effective_frames_map.items() if v.get("rgba") is not None
        }
        for seq in atr.sequences:
            s_meta = seq.to_metadata()
            if (
                not opts.inspect_only
                and opts.export_animations
                and valid_effective_map
                and (not atr.is_dummy or opts.include_dummies)
            ):
                gif_rel = f"animations/seq_{seq.index:02d}.gif"
                if render_sequence_gif(seq, valid_effective_map, actor_dir / gif_rel):
                    s_meta["animation_gif"] = gif_rel
                    gifs_written += 1
            seq_meta_list.append(s_meta)

        actor_metadata = {
            "bundle": bundle.name,
            "file": fn,
            "actor_key": actor_key,
            "version": atr.version,
            "name": atr.name,
            "bitmap": atr.bitmap,
            "palette_source": actor_palette_sources.get(
                parent_key if parent_key else actor_key
            ),
            "parent": atr.parent or None,
            "parent_resolved": parent_resolved if atr.parent else None,
            "asset_id": atr.asset_id,
            "flags": {
                "raw": atr.flags,
                "movement_callback": atr.flag_movement_callback,
                "load_time_decompressed": atr.flag_load_time_decompressed,
                "no_scratch_buffer": atr.flag_no_scratch_buffer,
            },
            "is_dummy": atr.is_dummy,
            "max_width": atr.width,
            "max_height": atr.height,
            "declared_image_count": atr.declared_image_count,
            "trailing_bytes": atr.trailing_bytes,
            "actor_sheet": sheet_file_rel,
            "actor_sheet_layout": sheet_placements_meta if sheet_file_rel else None,
            "frames": frame_meta_list,
            "sequences": seq_meta_list,
        }

        bundle_manifest_actors[actor_key] = actor_metadata
        if not opts.inspect_only and opts.export_metadata:
            (actor_dir / "metadata.json").write_text(
                json.dumps(actor_metadata, indent=2), encoding="utf-8"
            )

    # 4. Build bundle-wide sprite atlas & scene composite
    atlas_rel: Optional[str] = None
    composite_rel: Optional[str] = None
    bg_rel: Optional[str] = None

    if not opts.inspect_only and all_bundle_atlas_frames and not opts.actor_filter:
        if opts.export_bundle_atlas:
            atlas_w, atlas_h, placements = pack_shelf(
                all_bundle_atlas_frames, max_width=opts.max_atlas_width, padding=4
            )
            atlas_img = Image.new("RGBA", (atlas_w, atlas_h), (0, 0, 0, 0))
            atlas_entries = []
            for p in placements:
                atlas_img.paste(p["rgba"], (p["sheet_x"], p["sheet_y"]), p["rgba"])
                atlas_entries.append(
                    {
                        "actor_key": p["actor_key"],
                        "actor_name": p["actor_name"],
                        "atr_file": p["atr_file"],
                        "bitmap": p["bitmap"],
                        "frame_index": p["frame_index"],
                        "atlas_rect": [p["sheet_x"], p["sheet_y"], p["w"], p["h"]],
                        "scene_rect": p["rect"],
                        "hotspot": p["hotspot"],
                    }
                )
            atlas_rel = f"{bundle.name}_atlas.png"
            atlas_img.save(bundle_out / atlas_rel, format="PNG", optimize=True)
            if opts.export_metadata:
                (bundle_out / f"{bundle.name}_atlas.json").write_text(
                    json.dumps(
                        {
                            "bundle": bundle.name,
                            "atlas_image": atlas_rel,
                            "atlas_size": [atlas_w, atlas_h],
                            "frame_count": len(atlas_entries),
                            "frames": atlas_entries,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

        if opts.export_scene_composite:
            bg_canvas: Optional[Image.Image] = None
            for stem, (bfn, bblob) in sorted(bundle.bitmap_blobs.items()):
                if stem == "dummy":
                    continue
                _, cand_img = extract_palette_and_image(bblob)
                if cand_img is not None:
                    if cand_img.size == (640, 480) or bg_canvas is None:
                        bg_canvas = cand_img.convert("RGBA")
                        if cand_img.size == (640, 480):
                            bg_rel = f"{bundle.name}_background.png"
                            cand_img.save(
                                bundle_out / bg_rel, format="PNG", optimize=True
                            )
                            break

            if bg_canvas is None:
                bg_canvas = Image.new("RGBA", (640, 480), (24, 28, 36, 255))

            seen_actors = set()
            for item in all_bundle_atlas_frames:
                if item["actor_key"] in seen_actors:
                    continue
                seen_actors.add(item["actor_key"])
                x1, y1, _, _ = item["rect"]
                if 0 <= x1 < bg_canvas.width and 0 <= y1 < bg_canvas.height:
                    bg_canvas.paste(item["rgba"], (x1, y1), item["rgba"])

            composite_rel = f"{bundle.name}_scene_composite.png"
            bg_canvas.save(bundle_out / composite_rel, format="PNG", optimize=True)

    bundle_summary = {
        "bundle": bundle.name,
        "source": bundle.source_description,
        "transparent_index": TRANSPARENT_KEY,
        "actor_count": len(bundle_manifest_actors),
        "frames_decoded": total_frames_decoded,
        "frame_errors": total_frame_errors,
        "actor_sheets_written": sheets_written,
        "animations_written": gifs_written,
        "bundle_atlas": atlas_rel,
        "scene_composite": composite_rel,
        "background_bitmap": bg_rel,
        "actors": bundle_manifest_actors,
    }

    if not opts.inspect_only and opts.export_metadata:
        (bundle_out / "manifest.json").write_text(
            json.dumps(bundle_summary, indent=2), encoding="utf-8"
        )

    return bundle_summary


# ============================================================================
# CLI Interface
# ============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atr-extractor",
        description=(
            "Extract ATR11 sprite frames, actor sprite sheets, sequence animations, "
            "bundle atlases, scene composites, and JSON metadata from an ISO disc "
            "image, RSC bundle, or directory of ATR files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python atr_extractor.py --input=image.iso --output=output/\n"
            "  python atr_extractor.py --input=path/to/atrs --output=output/\n"
            "  python atr_extractor.py --input=image.iso --bundle=AG001 --no-animations\n"
            "  python atr_extractor.py --input=path/to/AGM001.ATR --inspect\n"
        ),
    )
    parser.add_argument(
        "-i",
        "--input",
        required=True,
        help="Path to an .ISO/.BIN image, .RSC archive, .ATR file, or directory containing ATR/RSC files.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="output",
        help="Destination directory for extracted files (default: output/).",
    )
    parser.add_argument(
        "--bundle",
        default=None,
        help="Optional bundle name filter (e.g., AG001).",
    )
    parser.add_argument(
        "--actor",
        default=None,
        help="Optional actor name/file stem filter (e.g., AGM001).",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Inspect and validate parsed ATR metadata without writing image files.",
    )

    bool_action = argparse.BooleanOptionalAction
    parser.add_argument(
        "--frames",
        action=bool_action,
        default=True,
        help="Export individual frame PNGs (default: enabled).",
    )
    parser.add_argument(
        "--actor-sheets",
        action=bool_action,
        default=True,
        help="Export per-actor sprite sheet PNGs (default: enabled).",
    )
    parser.add_argument(
        "--animations",
        action=bool_action,
        default=True,
        help="Export animated GIFs for each sequence (default: enabled).",
    )
    parser.add_argument(
        "--bundle-atlas",
        action=bool_action,
        default=True,
        help="Export bundle-wide packed sprite atlas PNG + JSON (default: enabled).",
    )
    parser.add_argument(
        "--scene-composite",
        action=bool_action,
        default=True,
        help="Export bundle room scene composite PNG (default: enabled).",
    )
    parser.add_argument(
        "--metadata",
        action=bool_action,
        default=True,
        help="Export per-actor metadata.json, bundle manifest.json, and root index.json (default: enabled).",
    )
    parser.add_argument(
        "--include-dummies",
        action=bool_action,
        default=False,
        help="Include invisible dummy.bmp hitbox sprites in atlases and animations (default: disabled).",
    )
    parser.add_argument(
        "--max-atlas-width",
        type=int,
        default=2048,
        help="Maximum width in pixels for packed bundle atlases (default: 2048).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()

    opts = ExtractionOptions(
        output_dir=output_dir,
        export_frames=args.frames,
        export_actor_sheets=args.actor_sheets,
        export_animations=args.animations,
        export_bundle_atlas=args.bundle_atlas,
        export_scene_composite=args.scene_composite,
        export_metadata=args.metadata,
        include_dummies=args.include_dummies,
        max_atlas_width=args.max_atlas_width,
        bundle_filter=args.bundle,
        actor_filter=args.actor,
        inspect_only=args.inspect,
    )

    try:
        bundles = discover_bundles_from_path(
            input_path, bundle_filter=opts.bundle_filter
        )
    except Exception as exc:
        print(f"[-] Error reading input '{input_path}': {exc}", file=sys.stderr)
        return 1

    if not bundles:
        print(f"[-] No ATR or RSC assets found in {input_path}.", file=sys.stderr)
        return 1

    print(
        f"[*] Discovered {len(bundles)} bundle(s) from {input_path.name}"
        + (" [INSPECT ONLY]" if opts.inspect_only else f" -> {output_dir}")
    )

    palette_cache: Dict[Tuple[str, str], Tuple[Optional[List[int]], Optional[str]]] = {}
    summary_bundles: List[Dict[str, Any]] = []
    totals = {
        "bundles": 0,
        "actors": 0,
        "frames": 0,
        "errors": 0,
        "sheets": 0,
        "animations": 0,
    }

    for b in bundles:
        summary = process_bundle(b, bundles, opts, palette_cache)
        if summary["actor_count"] == 0:
            continue
        totals["bundles"] += 1
        totals["actors"] += summary["actor_count"]
        totals["frames"] += summary["frames_decoded"]
        totals["errors"] += summary["frame_errors"]
        totals["sheets"] += summary["actor_sheets_written"]
        totals["animations"] += summary["animations_written"]

        print(
            f"  [+] {b.name:<8}: {summary['actor_count']:>3} actors, "
            f"{summary['frames_decoded']:>4} frames, "
            f"{summary['actor_sheets_written']:>3} sheets, "
            f"{summary['animations_written']:>3} animations"
            + (f" ({summary['frame_errors']} errors)" if summary["frame_errors"] else ""),
            flush=True,
        )
        summary_bundles.append(
            {
                "bundle": summary["bundle"],
                "source": summary["source"],
                "actor_count": summary["actor_count"],
                "frames_decoded": summary["frames_decoded"],
                "frame_errors": summary["frame_errors"],
                "actor_sheets_written": summary["actor_sheets_written"],
                "animations_written": summary["animations_written"],
                "bundle_atlas": summary["bundle_atlas"],
                "scene_composite": summary["scene_composite"],
                "manifest": f"{b.name}/manifest.json",
            }
        )

    if not opts.inspect_only and opts.export_metadata:
        output_dir.mkdir(parents=True, exist_ok=True)
        index_doc = {
            "generator": f"atr-extractor {__version__}",
            "input": str(input_path),
            "totals": totals,
            "bundles": summary_bundles,
        }
        (output_dir / "index.json").write_text(
            json.dumps(index_doc, indent=2), encoding="utf-8"
        )

    print(
        f"\n[+] Complete: {totals['bundles']} bundle(s), "
        f"{totals['actors']:,} actor(s), "
        f"{totals['frames']:,} frame(s), "
        f"{totals['sheets']:,} actor sheet(s), "
        f"{totals['animations']:,} animation GIF(s), "
        f"{totals['errors']} error(s)."
    )
    return 0 if totals["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
