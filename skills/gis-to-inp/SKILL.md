---
name: gis-to-inp
description: Convert GIS shapefiles (Manholes + Links) to SWMM INP model files with lossless round-trip support
---

## GIS to INP Conversion

Convert GIS shapefiles (Manholes.shp + Links.shp) into a SWMM `.inp` model file.
Supports lossless round-trip restoration when metadata from a previous `inp_to_gis`
export is available.

### Conversion Modes

1. **Lossless Restore** (`--no_preserve_all_sections` NOT set, `--rebuild_core_from_gis` NOT set)
   — If metadata JSON exists alongside the shapefiles, the original INP is restored
   byte-for-byte from the cached full text.

2. **Preserve Non-Core, Rebuild Core** (`--no_preserve_all_sections` NOT set, `--rebuild_core_from_gis` set)
   — Keeps all non-network sections (options, time-series, etc.) from metadata and
   rebuilds the core network sections ([JUNCTIONS], [OUTFALLS], [CONDUITS], etc.)
   from the current GIS geometry.

3. **GIS-Only Minimal** (`--no_preserve_all_sections` set)
   — Creates a minimal INP containing only the network sections derived from GIS,
   with no metadata required. Useful when starting from scratch.

### Usage

```bash
python skills/gis-to-inp/Scripts/gis_to_inp_cli.py \
  --gis_path <path to GIS folder or Manholes.shp> \
  --output_inp <output .inp path> \
  --links_path <optional Links.shp path> \
  --default_roughness 0.013 \
  --rebuild_core_from_gis
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| --gis_path | Directory containing Manholes.shp (or path to Manholes.shp directly) | (required) |
| --output_inp | Output path for the generated .inp file | (required) |
| --links_path | Explicit path to Links.shp (auto-resolved from gis_path if omitted) | None |
| --default_roughness | Manning's roughness for conduits when not in GIS attributes | 0.013 |
| --no_preserve_all_sections | Flag to disable metadata-based preservation (creates minimal INP) | False (preserve is on) |
| --rebuild_core_from_gis | Flag to rebuild core network sections from GIS instead of restoring from metadata | False |

### Output

Prints a summary including:
- Conversion mode used (`lossless_restore`, `preserve_non_core_rebuild_core`, or `gis_only_minimal`)
- Output INP file path
- Whether lossless metadata was used
- Network statistics: node count, junction count, outfall count, link count
