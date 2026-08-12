"""Tag auto-coloring by weighted hue-range partitioning.

Every node owns a slice of the color wheel and splits it among its
children **proportionally to subtree size**, so hue variety flows to where
the tags actually are — at any depth. A lone "Email" root can't hog a third
of the wheel while the main tree's 20 branches fight over the rest: the big
subtree gets almost everything, and its children come out visibly distinct.
"""


import colorsys

# Depth-0 values, matching what assign_palette gives a root.
ROOT_SAT = 58.0
ROOT_LIGHT = 40.0
# One step down the tree, from the same formulas assign_palette uses.
DEPTH_LIGHT_STEP = 8.0
DEPTH_SAT_STEP = 4.0
MAX_LIGHT = 76.0
MIN_SAT = 42.0
# How far siblings are pushed either side of the parent's hue. Small, so a
# child still reads as belonging to its parent's family.
SIBLING_HUE_STEP = 12.0


def hsl_to_hex(h: float, s: float, light: float) -> str:
    # round, not truncate: int() loses up to a whole step per channel, and a
    # child colour derived from a parent's hex would drift darker each
    # generation rather than round-tripping.
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, light / 100, s / 100)
    return "#{:02x}{:02x}{:02x}".format(
        *(min(255, max(0, round(c * 255))) for c in (r, g, b))
    )


def hex_to_hsl(value: str) -> tuple[float, float, float] | None:
    raw = (value or "").lstrip("#")
    if len(raw) != 6:
        return None
    try:
        r, g, b = (int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    h, light, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, s * 100, light * 100


def child_hsl(parent_hsl: tuple[float, float, float], sibling_index: int):
    """A shade for a new child, derived from its parent alone.

    assign_palette partitions the whole wheel, so recomputing it to colour one
    new tag would shift every other tag — and silently overwrite any colour
    picked by hand. This stays local: the same one-step-lighter, one-step-less
    saturated relationship that palette gives a child, with siblings fanned
    out either side of the parent's hue so they stay distinguishable.
    """
    parent_h, parent_s, parent_l = parent_hsl
    step = (sibling_index + 1) // 2 * SIBLING_HUE_STEP
    offset = step if sibling_index % 2 else -step
    return (
        (parent_h + offset) % 360,
        max(parent_s - DEPTH_SAT_STEP, MIN_SAT),
        min(parent_l + DEPTH_LIGHT_STEP, MAX_LIGHT),
    )


def next_root_hue(existing: list[float]) -> float:
    """The hue furthest from every hue already in use — the midpoint of the
    widest gap around the wheel."""
    hues = sorted(h % 360 for h in existing)
    if not hues:
        return 0.0
    widest, best = -1.0, hues[0]
    for i, hue in enumerate(hues):
        nxt = hues[(i + 1) % len(hues)]
        gap = (nxt - hue) % 360 or 360.0
        if gap > widest:
            widest, best = gap, (hue + gap / 2) % 360
    return best


def assign_palette(nodes: list[tuple]) -> dict:
    """nodes: (id, parent_id, sort_key) → {id: (hue, sat, light)}."""
    by_parent: dict = {}
    for node_id, parent_id, sort_key in sorted(nodes, key=lambda n: n[2]):
        by_parent.setdefault(parent_id, []).append(node_id)

    # Subtree weights: 1 + descendants, computed bottom-up.
    weight: dict = {}

    def weigh(node_id) -> int:
        total = 1
        for child in by_parent.get(node_id, []):
            total += weigh(child)
        weight[node_id] = total
        return total

    for root in by_parent.get(None, []):
        weigh(root)

    out: dict = {}

    def walk(ids, lo, hi, depth):
        span = hi - lo
        total_weight = sum(weight[i] for i in ids)
        cursor = lo
        for i, node_id in enumerate(ids):
            share = span * weight[node_id] / total_weight
            a, b = cursor, cursor + share
            cursor = b
            light = min(40 + depth * 8 + ((i % 3) * 3 if depth >= 2 else 0), 76)
            sat = max(58 - depth * 4, 42)
            out[node_id] = ((a + b) / 2, sat, light)
            children = by_parent.get(node_id, [])
            if children:
                # A single-child chain keeps the whole slice.
                walk(children, a, b, depth + 1)

    walk(by_parent.get(None, []), 0.0, 360.0, 0)
    return out
