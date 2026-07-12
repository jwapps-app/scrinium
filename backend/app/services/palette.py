"""Tag auto-coloring by weighted hue-range partitioning.

Every node owns a slice of the color wheel and splits it among its
children **proportionally to subtree size**, so hue variety flows to where
the tags actually are — at any depth. A lone "Email" root can't hog a third
of the wheel while the main tree's 20 branches fight over the rest: the big
subtree gets almost everything, and its children come out visibly distinct.
"""


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
