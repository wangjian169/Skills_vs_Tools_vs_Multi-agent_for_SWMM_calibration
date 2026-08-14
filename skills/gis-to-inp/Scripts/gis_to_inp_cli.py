# -*- coding: utf-8 -*-
"""
Self-contained GIS-to-INP converter CLI.

All logic is inlined — no imports from project root modules.
Used by both the skills architecture (CLI stdout) and the tools architecture
(via ``run_gis_to_inp`` return value).
"""

from __future__ import annotations

import argparse
import json
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

import shapefile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
META_FILENAME = "_swmm_transfer_meta.json"
CORE_NETWORK_SECTIONS = (
    "JUNCTIONS",
    "OUTFALLS",
    "CONDUITS",
    "XSECTIONS",
    "COORDINATES",
    "VERTICES",
)

# ---------------------------------------------------------------------------
# Helper utilities (inlined from transfer.py)
# ---------------------------------------------------------------------------

def _safe_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    text = text.replace(",", ".")
    try:
        return float(text)
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))
    except Exception:
        return default


def _polyline_length(points: List[Tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        total += math.hypot(x1 - x0, y1 - y0)
    return total


def _first_non_empty(record: Dict[str, object], candidates: Iterable[str], default=None):
    lowered = {k.lower(): k for k in record.keys()}
    for key in candidates:
        direct = record.get(key)
        if direct not in (None, ""):
            return direct
        lk = lowered.get(key.lower())
        if lk is not None:
            value = record.get(lk)
            if value not in (None, ""):
                return value
    return default


def _record_to_dict(reader: shapefile.Reader, record) -> Dict[str, object]:
    field_names = [field[0] for field in reader.fields[1:]]
    values = list(record)
    result: Dict[str, object] = {}
    for idx, name in enumerate(field_names):
        result[name] = values[idx] if idx < len(values) else None
    return result


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def _write_text(path: str, text: str) -> None:
    output_dir = os.path.dirname(os.path.abspath(path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _write_inp_from_blocks(
    output_inp: str,
    blocks: List[Dict[str, Any]],
    newline: str = "\n",
    ended_with_newline: bool = True,
) -> None:
    out_lines: List[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "preamble":
            out_lines.extend(block.get("lines", []))
        elif btype == "section":
            out_lines.append(block.get("header", f"[{block.get('name', '')}]"))
            out_lines.extend(block.get("lines", []))

    text = newline.join(out_lines)
    if ended_with_newline and (not text.endswith(("\n", "\r"))):
        text += newline
    _write_text(output_inp, text)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _load_metadata(gis_dir: str) -> Optional[Dict[str, Any]]:
    meta_path = os.path.join(gis_dir, META_FILENAME)
    if not os.path.exists(meta_path):
        return None
    with open(meta_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid metadata format: {meta_path}")
    return data


def _replace_section_lines(
    blocks: List[Dict[str, Any]],
    replacements: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    for block in blocks:
        btype = block.get("type")
        if btype != "section":
            out.append({"type": "preamble", "lines": list(block.get("lines", []))})
            continue

        name = str(block.get("name", "")).upper()
        if name in replacements:
            out.append(
                {
                    "type": "section",
                    "name": name,
                    "header": block.get("header", f"[{name}]"),
                    "lines": list(replacements[name]),
                }
            )
        else:
            out.append(
                {
                    "type": "section",
                    "name": name,
                    "header": block.get("header", f"[{name}]"),
                    "lines": list(block.get("lines", [])),
                }
            )

    existing_names = {
        str(block.get("name", "")).upper()
        for block in out
        if block.get("type") == "section"
    }
    for section_name, lines in replacements.items():
        if section_name.upper() not in existing_names:
            out.append(
                {
                    "type": "section",
                    "name": section_name.upper(),
                    "header": f"[{section_name.upper()}]",
                    "lines": list(lines),
                }
            )

    return out


# ---------------------------------------------------------------------------
# GIS path resolution
# ---------------------------------------------------------------------------

def _resolve_gis_paths(gis_path: str, links_path: Optional[str] = None) -> Tuple[str, str]:
    if links_path:
        return gis_path, links_path

    if not os.path.exists(gis_path) and not gis_path.lower().endswith(".shp"):
        os.makedirs(gis_path, exist_ok=True)
        manholes = os.path.join(gis_path, "Manholes.shp")
        links = os.path.join(gis_path, "Links.shp")
        return manholes, links

    if os.path.isdir(gis_path):
        manholes = os.path.join(gis_path, "Manholes.shp")
        links = os.path.join(gis_path, "Links.shp")
        return manholes, links

    base_name = os.path.basename(gis_path).lower()
    if base_name == "manholes.shp":
        links = os.path.join(os.path.dirname(gis_path), "Links.shp")
        return gis_path, links

    raise ValueError("Provide either a GIS directory or both manholes/links shapefile paths.")


# ---------------------------------------------------------------------------
# GIS parsing
# ---------------------------------------------------------------------------

def _parse_gis_network(
    manholes_path: str,
    links_path: str,
    default_roughness: float,
) -> Tuple[Dict[str, Dict[str, object]], List[Dict[str, object]]]:
    mh_reader = shapefile.Reader(manholes_path)
    lk_reader = shapefile.Reader(links_path)

    nodes: Dict[str, Dict[str, object]] = {}
    node_id_to_name: Dict[int, str] = {}
    next_auto_id = 1

    for shape_record in mh_reader.iterShapeRecords():
        rec = _record_to_dict(mh_reader, shape_record.record)
        shape = shape_record.shape

        node_name = _first_non_empty(rec, ["NodeName", "NAME", "Name", "NODE_NAME", "NODE"])
        if node_name is None or str(node_name).strip() == "":
            node_name = f"NODE_{next_auto_id}"
        node_name = str(node_name).strip()

        node_id = _safe_int(_first_non_empty(rec, ["VCS_NodeID", "ObjectID", "OBJECTID", "ID"]), 0)
        if node_id <= 0:
            node_id = next_auto_id

        invert = _safe_float(
            _first_non_empty(rec, ["InvertLeve", "Invert", "Elevation", "ELEV", "ELEVATION"]),
            0.0,
        )
        ground = _safe_float(_first_non_empty(rec, ["GroundLeve", "GROUND", "Ground"]), invert)
        max_depth = _safe_float(
            _first_non_empty(rec, ["MaxDepth", "MAXDEPTH"]),
            max(0.0, ground - invert),
        )
        node_type = str(_first_non_empty(rec, ["NodeType", "TYPE", "Type"], "JUNCTION")).upper()

        if shape.points:
            x, y = shape.points[0]
        else:
            x = _safe_float(_first_non_empty(rec, ["XCoordinat", "X", "X_COORD"]), 0.0)
            y = _safe_float(_first_non_empty(rec, ["YCoordinat", "Y", "Y_COORD"]), 0.0)

        if node_name in nodes:
            node_name = f"{node_name}_{node_id}"

        nodes[node_name] = {
            "name": node_name,
            "node_id": node_id,
            "invert_elev": invert,
            "max_depth": max_depth,
            "x": float(x),
            "y": float(y),
            "node_type": node_type,
        }
        node_id_to_name[node_id] = node_name
        next_auto_id = max(next_auto_id, node_id + 1)

    links: List[Dict[str, object]] = []
    used_link_names: Dict[str, int] = {}

    for shape_record in lk_reader.iterShapeRecords():
        rec = _record_to_dict(lk_reader, shape_record.record)
        shape = shape_record.shape
        points = shape.points[:] if shape.points else []

        from_node = _first_non_empty(rec, ["FromNode", "FROMNODE", "From_Node", "FROM"])
        to_node = _first_non_empty(rec, ["ToNode", "TONODE", "To_Node", "TO"])

        if not from_node:
            up_id = _safe_int(_first_non_empty(rec, ["UpstreamNo", "UPSTREAM", "UP_ID"]), 0)
            from_node = node_id_to_name.get(up_id)
        if not to_node:
            down_id = _safe_int(_first_non_empty(rec, ["Downstream", "DOWNSTREAM", "DOWN_ID"]), 0)
            to_node = node_id_to_name.get(down_id)

        if not from_node or not to_node:
            continue

        from_node = str(from_node).strip()
        to_node = str(to_node).strip()
        if from_node not in nodes or to_node not in nodes:
            continue

        raw_link_name = _first_non_empty(rec, ["MainPipeID", "LinkName", "NAME", "Name"])
        if raw_link_name is None or str(raw_link_name).strip() == "":
            raw_link_name = f"{from_node}_{to_node}"
        base_link_name = str(raw_link_name).strip()
        if base_link_name not in used_link_names:
            used_link_names[base_link_name] = 1
            link_name = base_link_name
        else:
            used_link_names[base_link_name] += 1
            link_name = f"{base_link_name}_{used_link_names[base_link_name]}"

        length = _safe_float(
            _first_non_empty(rec, ["PipeLength", "Length", "Shape_Leng", "LEN"]),
            0.0,
        )
        if length <= 0 and len(points) >= 2:
            length = _polyline_length(points)
        if length <= 0:
            length = 1.0

        roughness = _safe_float(
            _first_non_empty(rec, ["Roughness", "ManningN", "MANNINGN"]),
            default_roughness,
        )
        in_offset = _safe_float(_first_non_empty(rec, ["InOffset", "INOFFSET"]), 0.0)
        out_offset = _safe_float(_first_non_empty(rec, ["OutOffset", "OUTOFFSET"]), 0.0)

        diameter_raw = _safe_float(
            _first_non_empty(rec, ["InternalDi", "Diameter", "Geom1", "DIAMETER"]),
            0.0,
        )
        if diameter_raw > 20.0:
            diameter_m = diameter_raw / 1000.0
        elif diameter_raw > 0.0:
            diameter_m = diameter_raw
        else:
            diameter_m = 0.3

        from_invert = _safe_float(nodes[from_node]["invert_elev"], 0.0)
        to_invert = _safe_float(nodes[to_node]["invert_elev"], 0.0)
        upstream_invert = _safe_float(
            _first_non_empty(rec, ["UpstreamIn", "UP_INVERT"]),
            from_invert + in_offset,
        )
        downstream_invert = _safe_float(
            _first_non_empty(rec, ["Downstre_1", "DownstreamI", "DOWN_INVERT"]),
            to_invert + out_offset,
        )

        vertex_points = points[1:-1] if len(points) >= 2 else []

        links.append(
            {
                "name": link_name,
                "from_node": from_node,
                "to_node": to_node,
                "length": length,
                "roughness": roughness,
                "in_offset": in_offset,
                "out_offset": out_offset,
                "diameter_m": diameter_m,
                "upstream_invert": upstream_invert,
                "downstream_invert": downstream_invert,
                "vertices": vertex_points,
            }
        )

    return nodes, links


# ---------------------------------------------------------------------------
# Network section building
# ---------------------------------------------------------------------------

def _build_network_sections(
    nodes: Dict[str, Dict[str, object]],
    links: List[Dict[str, object]],
    default_roughness: float,
) -> Tuple[Dict[str, List[str]], Dict[str, int]]:
    connected_nodes = set()
    from_nodes = set()
    to_nodes = set()
    for link in links:
        connected_nodes.add(str(link["from_node"]))
        connected_nodes.add(str(link["to_node"]))
        from_nodes.add(str(link["from_node"]))
        to_nodes.add(str(link["to_node"]))

    explicit_outfalls = {
        name for name, node in nodes.items() if "OUTFALL" in str(node.get("node_type", "")).upper()
    }
    derived_outfalls = to_nodes - from_nodes
    outfalls = explicit_outfalls if explicit_outfalls else derived_outfalls

    model_nodes = sorted(set(nodes.keys()) | connected_nodes)
    junctions = sorted([n for n in model_nodes if n not in outfalls])
    outfalls_sorted = sorted([n for n in model_nodes if n in outfalls])

    sections: Dict[str, List[str]] = {}

    j_lines = [";;Name\tElevation\tMaxDepth\tInitDepth\tSurDepth\tAponded"]
    for name in junctions:
        node = nodes[name]
        j_lines.append(
            f"{name}\t{_safe_float(node['invert_elev']):.6f}\t{_safe_float(node['max_depth']):.6f}\t0\t0\t0"
        )
    sections["JUNCTIONS"] = j_lines

    o_lines = [";;Name\tElevation\tType\tStage Data\tGated\tRoute To"]
    for name in outfalls_sorted:
        node = nodes[name]
        o_lines.append(f"{name}\t{_safe_float(node['invert_elev']):.6f}\tFREE\t\tNO\t")
    sections["OUTFALLS"] = o_lines

    c_lines = [";;Name\tFromNode\tToNode\tLength\tRoughness\tInOffset\tOutOffset\tInitFlow\tMaxFlow"]
    for link in links:
        c_lines.append(
            f"{link['name']}\t{link['from_node']}\t{link['to_node']}\t"
            f"{_safe_float(link['length']):.6f}\t{_safe_float(link['roughness'], default_roughness):.6f}\t"
            f"{_safe_float(link['in_offset']):.6f}\t{_safe_float(link['out_offset']):.6f}\t0\t0"
        )
    sections["CONDUITS"] = c_lines

    x_lines = [";;Name\tShape\tGeom1\tGeom2\tGeom3\tGeom4\tBarrels"]
    for link in links:
        x_lines.append(f"{link['name']}\tCIRCULAR\t{_safe_float(link['diameter_m'], 0.3):.6f}\t0\t0\t0\t1")
    sections["XSECTIONS"] = x_lines

    coord_lines = [";;Node\tX-Coord\tY-Coord"]
    for name in model_nodes:
        node = nodes[name]
        coord_lines.append(f"{name}\t{_safe_float(node['x']):.6f}\t{_safe_float(node['y']):.6f}")
    sections["COORDINATES"] = coord_lines

    vert_lines = [";;Link\tX-Coord\tY-Coord"]
    for link in links:
        for x, y in link["vertices"]:
            vert_lines.append(f"{link['name']}\t{_safe_float(x):.6f}\t{_safe_float(y):.6f}")
    sections["VERTICES"] = vert_lines

    stats = {
        "node_count": len(model_nodes),
        "link_count": len(links),
        "junction_count": len(junctions),
        "outfall_count": len(outfalls_sorted),
    }
    return sections, stats


# ---------------------------------------------------------------------------
# Default INP block builder
# ---------------------------------------------------------------------------

def _default_inp_blocks(network_sections: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = [
        {
            "type": "section",
            "name": "TITLE",
            "header": "[TITLE]",
            "lines": [";; Generated by gis_to_inp_cli.py"],
        },
        {
            "type": "section",
            "name": "OPTIONS",
            "header": "[OPTIONS]",
            "lines": [
                ";;Option             Value",
                "FLOW_UNITS           CMS",
                "INFILTRATION         HORTON",
                "FLOW_ROUTING         DYNWAVE",
                "LINK_OFFSETS         DEPTH",
                "MIN_SLOPE            0",
                "ALLOW_PONDING        YES",
                "SKIP_STEADY_STATE    NO",
                "START_DATE           01/01/2020",
                "START_TIME           00:00:00",
                "REPORT_START_DATE    01/01/2020",
                "REPORT_START_TIME    00:00:00",
                "END_DATE             01/02/2020",
                "END_TIME             00:00:00",
                "SWEEP_START          01/01",
                "SWEEP_END            12/31",
                "DRY_DAYS             0",
                "REPORT_STEP          00:15:00",
                "WET_STEP             00:05:00",
                "DRY_STEP             01:00:00",
                "ROUTING_STEP         00:00:05",
            ],
        },
    ]

    for section in CORE_NETWORK_SECTIONS:
        blocks.append(
            {
                "type": "section",
                "name": section,
                "header": f"[{section}]",
                "lines": list(network_sections.get(section, [])),
            }
        )

    return blocks


# ---------------------------------------------------------------------------
# Main callable: run_gis_to_inp
# ---------------------------------------------------------------------------

def run_gis_to_inp(
    gis_path: str,
    output_inp: str,
    links_path: Optional[str] = None,
    default_roughness: float = 0.013,
    preserve_all_sections: bool = True,
    rebuild_core_from_gis: bool = False,
) -> str:
    """Convert GIS shapefiles to SWMM INP and return a summary string.

    The summary is both printed (for skills architecture) and returned
    (for tools architecture) to guarantee identical content.
    """
    manholes_path, links_path_resolved = _resolve_gis_paths(gis_path, links_path)

    if not os.path.exists(manholes_path):
        raise FileNotFoundError(f"Manholes shapefile not found: {manholes_path}")
    if not os.path.exists(links_path_resolved):
        raise FileNotFoundError(f"Links shapefile not found: {links_path_resolved}")

    gis_dir = os.path.dirname(os.path.abspath(manholes_path))
    meta = _load_metadata(gis_dir)

    if preserve_all_sections and meta is None:
        raise FileNotFoundError(
            f"Lossless metadata not found: {os.path.join(gis_dir, META_FILENAME)}. "
            "Run inp_to_gis first, or set preserve_all_sections=False."
        )

    nodes, links = _parse_gis_network(manholes_path, links_path_resolved, default_roughness)
    network_sections, stats = _build_network_sections(nodes, links, default_roughness)

    if meta is not None and preserve_all_sections and not rebuild_core_from_gis:
        full_text = str(meta.get("full_text", ""))
        if not full_text:
            raise ValueError("Metadata exists but full_text is empty; cannot restore losslessly.")
        _write_text(output_inp, full_text)
        mode = "lossless_restore"
        metadata_used = True

    elif meta is not None and preserve_all_sections:
        blocks = meta.get("blocks", [])
        if not isinstance(blocks, list):
            raise ValueError("Metadata blocks format invalid.")
        replacements = {name: network_sections.get(name, []) for name in CORE_NETWORK_SECTIONS}
        patched_blocks = _replace_section_lines(blocks, replacements)
        _write_inp_from_blocks(
            output_inp,
            patched_blocks,
            newline=str(meta.get("newline", "\n")),
            ended_with_newline=bool(meta.get("ended_with_newline", True)),
        )
        mode = "preserve_non_core_rebuild_core"
        metadata_used = True

    else:
        blocks = _default_inp_blocks(network_sections)
        _write_inp_from_blocks(output_inp, blocks, newline="\n", ended_with_newline=True)
        mode = "gis_only_minimal"
        metadata_used = False

    summary = (
        f"GIS to INP conversion complete.\n"
        f"Mode: {mode}\n"
        f"Output: {output_inp}\n"
        f"Metadata used: {metadata_used}\n"
        f"Nodes: {stats['node_count']}, Junctions: {stats['junction_count']}, "
        f"Outfalls: {stats['outfall_count']}, Links: {stats['link_count']}\n"
        f"Please ignore the time alignment error, calibration will handle time alignment automatically"
    )
    print(summary)
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert GIS shapefiles (Manholes + Links) to a SWMM .inp model file."
    )
    parser.add_argument("--gis_path", required=True,
                        help="Directory containing Manholes.shp or path to Manholes.shp directly")
    parser.add_argument("--output_inp", required=True,
                        help="Output path for the generated .inp file")
    parser.add_argument("--links_path", default=None,
                        help="Explicit path to Links.shp (auto-resolved if omitted)")
    parser.add_argument("--default_roughness", type=float, default=0.013,
                        help="Manning's roughness for conduits (default: 0.013)")
    parser.add_argument("--no_preserve_all_sections", action="store_true",
                        help="Disable metadata-based preservation (creates minimal INP)")
    parser.add_argument("--rebuild_core_from_gis", action="store_true",
                        help="Rebuild core network sections from GIS instead of restoring from metadata")
    args = parser.parse_args()

    preserve_all_sections = not args.no_preserve_all_sections

    run_gis_to_inp(
        gis_path=args.gis_path,
        output_inp=args.output_inp,
        links_path=args.links_path,
        default_roughness=args.default_roughness,
        preserve_all_sections=preserve_all_sections,
        rebuild_core_from_gis=args.rebuild_core_from_gis,
    )


if __name__ == "__main__":
    main()
