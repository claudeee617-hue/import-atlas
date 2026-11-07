"""Turn scanned files into a graph with the numbers that make it useful."""

from __future__ import annotations

from collections import Counter, deque

DEFAULT_MAX_NODES = 1500
VENDOR_PARTS = {"vendor", "_vendor", "vendored", "third_party", "thirdparty", "extern", "external"}


def _is_vendored(path: str) -> bool:
    return any(part.lower() in VENDOR_PARTS for part in path.split("/")[:-1])


def _group_depth(paths):
    """Pick the folder depth that splits the repo into meaningful groups."""
    depth = 0
    while depth < 2:
        counts = Counter(
            (p.split("/")[depth] if len(p.split("/")) > depth + 1 else "(root)") for p in paths
        )
        counts.pop("(root)", None)
        if counts and counts.most_common(1)[0][1] >= 0.8 * len(paths):
            depth += 1
        else:
            break
    return depth


def pagerank(n, out_adj, iters=40, damping=0.85):
    if n == 0:
        return []
    rank = [1.0 / n] * n
    for _ in range(iters):
        new = [(1 - damping) / n] * n
        dangling = sum(rank[i] for i in range(n) if not out_adj[i])
        for i in range(n):
            if out_adj[i]:
                share = damping * rank[i] / len(out_adj[i])
                for j in out_adj[i]:
                    new[j] += share
        extra = damping * dangling / n
        rank = [x + extra for x in new]
    top = max(rank)
    return [x / top for x in rank]


def strongly_connected(n, adj):
    """Iterative Tarjan. Returns a list of components (lists of node indexes)."""
    index = [-1] * n
    low = [0] * n
    on_stack = [False] * n
    stack, comps = [], []
    counter = 0
    for start in range(n):
        if index[start] != -1:
            continue
        work = [(start, 0)]
        while work:
            v, i = work.pop()
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack[v] = True
            descended = False
            for k in range(i, len(adj[v])):
                w = adj[v][k]
                if index[w] == -1:
                    work.append((v, k + 1))
                    work.append((w, 0))
                    descended = True
                    break
                if on_stack[w]:
                    low[v] = min(low[v], index[w])
            if descended:
                continue
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    comp.append(w)
                    if w == v:
                        break
                comps.append(comp)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[v])
    return comps


def _reach(start, adj):
    seen = {start}
    queue = deque([start])
    while queue:
        v = queue.popleft()
        for w in adj[v]:
            if w not in seen:
                seen.add(w)
                queue.append(w)
    return len(seen) - 1


def build_graph(result, max_nodes: int = DEFAULT_MAX_NODES) -> dict:
    files = result.files
    n = len(files)
    index = {f.path: i for i, f in enumerate(files)}

    edge_set = set()
    for i, f in enumerate(files):
        for target in f.imports:
            j = index.get(target)
            if j is not None and j != i:
                edge_set.add((i, j))
    edges = sorted(edge_set)
    out_adj = [[] for _ in range(n)]
    in_adj = [[] for _ in range(n)]
    for s, t in edges:
        out_adj[s].append(t)
        in_adj[t].append(s)

    rank = pagerank(n, out_adj)
    comps = [c for c in strongly_connected(n, out_adj) if len(c) > 1]
    comps.sort(key=lambda c: (-len(c), min(files[i].path for i in c)))
    cycle_of = [-1] * n
    for ci, comp in enumerate(comps):
        for i in comp:
            cycle_of[i] = ci

    ind = [len(a) for a in in_adj]
    outd = [len(a) for a in out_adj]
    sorted_in = sorted(ind)
    p90 = sorted_in[int(0.9 * (n - 1))] if n else 0
    hub_cut = max(4, p90)

    def role(i):
        if ind[i] == 0 and outd[i] == 0:
            return "orphan"
        if ind[i] == 0:
            return "entry"
        if ind[i] >= hub_cut:
            return "hub"
        if outd[i] == 0:
            return "leaf"
        return "module"

    roles = [role(i) for i in range(n)]

    # "start here": the files that matter most, plus the entry points that reach the most code
    start = []
    non_test = [i for i in range(n) if not files[i].is_test and not _is_vendored(files[i].path)]
    if not non_test:
        non_test = [i for i in range(n) if not files[i].is_test]
    core = sorted((i for i in non_test if ind[i] > 0), key=lambda i: -rank[i])[:6]
    for i in core:
        start.append({"i": i, "why": f"imported by {ind[i]} file{'s' if ind[i] != 1 else ''}"})
    entries = [i for i in non_test if roles[i] == "entry" and outd[i] >= 2]
    entries.sort(key=lambda i: (-outd[i], files[i].path))
    scored = sorted(((_reach(i, out_adj), i) for i in entries[:150]), key=lambda t: (-t[0], t[1]))
    for reach, i in scored[:3]:
        if all(s["i"] != i for s in start):
            start.append({"i": i, "why": f"entry point, reaches {reach} files"})

    # choose group depth and groups
    depth = _group_depth([f.path for f in files])

    def group_name(path):
        parts = path.split("/")
        return parts[depth] if len(parts) > depth + 1 else "(root)"

    group_counts = Counter(group_name(f.path) for f in files)
    group_order = [g for g, _ in group_counts.most_common()]
    group_idx = {g: i for i, g in enumerate(group_order)}

    langs = Counter()
    lang_loc = Counter()
    for f in files:
        langs[f.lang] += 1
        lang_loc[f.lang] += f.loc
    lang_order = [l for l, _ in langs.most_common()]
    lang_idx = {l: i for i, l in enumerate(lang_order)}

    externals = Counter()
    for f in files:
        externals.update(f.externals)

    # keep the most connected nodes when the repo is too large to draw
    keep = list(range(n))
    shown_truncated = False
    if n > max_nodes:
        keep.sort(key=lambda i: (-(ind[i] + outd[i]), -files[i].loc, files[i].path))
        keep = sorted(keep[:max_nodes])
        shown_truncated = True
    remap = {old: new for new, old in enumerate(keep)}

    nodes = []
    for old in keep:
        f = files[old]
        nodes.append({
            "p": f.path,
            "l": lang_idx[f.lang],
            "n": f.loc,
            "g": group_idx[group_name(f.path)],
            "t": 1 if f.is_test else 0,
            "r": roles[old],
            "k": round(rank[old], 4),
            "i": ind[old],
            "o": outd[old],
            "c": cycle_of[old],
            "x": f.externals[:12],
        })
    out_edges = [[remap[s], remap[t]] for s, t in edges if s in remap and t in remap]

    def to_new(lst):
        return [remap[i] for i in lst if i in remap]

    start_out = [s for s in ({"i": remap[x["i"]], "why": x["why"]} for x in start if x["i"] in remap)]
    cycles_out = [to_new(c) for c in comps[:30]]
    cycles_out = [sorted(c, key=lambda i: nodes[i]["p"]) for c in cycles_out if len(c) > 1]

    return {
        "name": result.name,
        "nodes": nodes,
        "edges": out_edges,
        "groups": [{"name": g, "count": group_counts[g]} for g in group_order],
        "langs": [{"name": l, "files": langs[l], "loc": lang_loc[l]} for l in lang_order],
        "startHere": start_out,
        "cycles": cycles_out,
        "externals": [[name, count] for name, count in externals.most_common(12)],
        "summary": {
            "files": n,
            "loc": sum(f.loc for f in files),
            "links": len(edges),
            "cycles": len(comps),
            "shown": len(nodes),
            "truncated": shown_truncated or result.truncated,
        },
    }
