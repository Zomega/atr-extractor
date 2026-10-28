# ATR Extractor (`atr-extractor`)

[![AI Influence Level: AIL4](<https://img.shields.io/badge/AI_Influence_Level-AIL4_(AI_Created%2C_Human_Basic_Idea)-4f8cff>)](https://danielmiessler.com/blog/ai-influence-level-ail)

A standalone, cross-platform command-line tool and Python library for extracting, decoding, and visualizing **`.ATR` (`ATR11`)** sprite descriptors and **`.RSC` (`BWRF10`)** resource bundles used in 1990s Knowledge Adventure games (originally reverse-engineered from _JumpStart Adventures 3rd Grade: Mystery Mountain_).

> [!NOTE]
> **AI Disclosure — [AI Influence Level (AIL)](https://danielmiessler.com/blog/ai-influence-level-ail): `AIL4` (AI Created, Human Basic Idea)**
> This tool, its reverse-engineered format specifications, and its documentation were written by/in Antigravity CLI under human direction, architectural review, and requirements definition.

---

## Quickstart

### 1. Installation

Requires **Python 3.9+**, **Pillow** (image processing), and **pycdlib** (cross-platform ISO reading):

```bash
pip install -r requirements.txt
```

Or install as a CLI package:

```bash
pip install .
```

### 2. Basic Usage

**Extract everything directly from a disc ISO image:**

```bash
python atr_extractor.py --input=image.iso --output=output/
```

**Extract from a directory containing `.ATR` files (and `.BMP` / `.PNG` palettes) or `.RSC` archives:**

```bash
python atr_extractor.py --input=path/to/atrs --output=output/
```

**Extract a single bundle or actor:**

```bash
python atr_extractor.py --input=image.iso --bundle=AG001 --output=output/
python atr_extractor.py --input=image.iso --bundle=AG001 --actor=AGM001 --output=output/
```

**Toggle specific outputs using feature flags:**

```bash
# Generate only individual frames, actor sheets, and JSON metadata (skip GIFs and bundle atlases)
python atr_extractor.py --input=image.iso --no-animations --no-bundle-atlas --no-scene-composite
```

---

## Command-Line Options

| Flag                                         | Default                | Description                                                                                             |
| :------------------------------------------- | :--------------------- | :------------------------------------------------------------------------------------------------------ |
| `-i`, `--input <PATH>`                       | _(required)_           | Input `.iso`/`.bin` disc image, `.rsc` bundle, `.atr` file, or directory containing `.atr`/`.rsc` files |
| `-o`, `--output <DIR>`                       | `output/`              | Destination directory for all extracted assets                                                          |
| `--bundle <NAME>`                            | _all_                  | Filter extraction to a specific bundle name or prefix (e.g., `AG001`)                                   |
| `--actor <NAME>`                             | _all_                  | Filter extraction to a specific actor name or filename stem (e.g., `AGM001`)                            |
| `--frames` / `--no-frames`                   | `--frames`             | Export individual frame PNGs under `<bundle>/<actor>/frames/`                                           |
| `--actor-sheets` / `--no-actor-sheets`       | `--actor-sheets`       | Export packed sprite sheet PNG per actor (`<bundle>/<actor>/sheet.png`)                                 |
| `--animations` / `--no-animations`           | `--animations`         | Export animated GIFs per sequence (`<bundle>/<actor>/animations/seq_XX.gif`)                            |
| `--bundle-atlas` / `--no-bundle-atlas`       | `--bundle-atlas`       | Export bundle-wide packed sprite atlas (`<bundle>/<bundle>_atlas.png` + `.json`)                        |
| `--scene-composite` / `--no-scene-composite` | `--scene-composite`    | Export room scene composite (`<bundle>/<bundle>_scene_composite.png`)                                   |
| `--metadata` / `--no-metadata`               | `--metadata`           | Export JSON metadata (`metadata.json`, `manifest.json`, `index.json`)                                   |
| `--include-dummies` / `--no-include-dummies` | `--no-include-dummies` | Include invisible `dummy.bmp` hitbox sprites in visual atlases and GIFs                                 |
| `--max-atlas-width <PX>`                     | `2048`                 | Maximum pixel width for packed bundle atlases                                                           |
| `--inspect`                                  | `False`                | Parse and validate inputs without writing files to disk                                                 |

---

## Output Directory Layout

By default, outputs are organized hierarchically by bundle and actor inside `output/`:

```text
output/
├── index.json                         # Top-level summary across all extracted bundles
└── AG001/                             # Bundle directory (from AG001.RSC or folder name)
    ├── manifest.json                  # Full bundle manifest & actor index
    ├── AG001_background.png           # 640x480 room background & palette source
    ├── AG001_atlas.png                # Packed RGBA sprite atlas for the entire bundle
    ├── AG001_atlas.json               # Atlas coordinates & scene rects for all frames
    ├── AG001_scene_composite.png      # Actors composited at their default scene coordinates
    └── agm001/                        # Per-actor directory
        ├── metadata.json              # Full ATR11 header, flags, frame rects, sheet layout & sequences
        ├── sheet.png                  # Packed sprite sheet for this actor
        ├── frames/                    # Individual decoded RGBA frames
        │   ├── 001.png
        │   ├── 002.png
        │   └── ...
        └── animations/                # Screen-registered animated GIFs per sequence
            ├── seq_00.gif
            └── ...
```

---

## Format Documentation

See [**`docs/ATR_FORMAT.md`**](docs/ATR_FORMAT.md) for the complete reverse-engineered specification of:

- The ISO 9660 $\rightarrow$ `BWRF10` (`.RSC`) $\rightarrow$ `ATR11` (`.ATR`) + `.BMP` asset pipeline
- Byte-level header, `IMAGE` (`CTrajectoryNode`), `SEQUENCE` (`CTrajectoryPath`), and 112-byte `ENTRY` (`CTrajectoryFrame`) structures
- Parent/child `.ATR` frame sharing and runtime origin anchoring
- The custom 12-bit MSB-first transparency-segregated LZW compression algorithm
