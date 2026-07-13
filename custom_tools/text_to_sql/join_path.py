"""Граф-алгоритм минимального join-пути (KMB Steiner-аппроксимация).

Чистая, детерминированная функция построения JOIN-набора по графу
кандидатов-рёбер. БЕЗ БД и БЕЗ LLM. Реализует KMB (Kou–Markowsky–Berman,
1981) 2-аппроксимацию Steiner-tree из дизайн-дока
``docs/plans/2026-06-29-text2sql-graph-join-containment-design.md`` (раздел 3.2).

Контракт возврата идентичен :meth:`JoinBuilder.build_joins`
(``join_builder.py:116``); направление/формат clause байт-идентичны greedy
на одношаговых случаях. Tie-break везде по ``_edge_key`` для детерминизма.

Caller (JoinBuilder) импортирует этот модуль ЛЕНИВО, поэтому top-level
``from .join_builder import ...`` здесь безопасен (цикла нет: join_builder
не импортирует join_path).
"""
import heapq
from typing import Any, Dict, List, Optional, Set, Tuple

from .dialects import quote_identifier
from .join_builder import INVERSE_JOIN_TYPES, SYMMETRIC_JOIN_TYPES


def _invert_join_type(jt: str) -> str:
    """Инвертирует join_type для reverse-edge (зеркало JoinBuilder._invert_join_type).

    LEFT<->RIGHT, симметричные не меняются, неизвестные → ValueError (fail-fast).
    """
    jt_norm = jt.upper().strip()
    if jt_norm in INVERSE_JOIN_TYPES:
        return INVERSE_JOIN_TYPES[jt_norm]
    if jt_norm in SYMMETRIC_JOIN_TYPES:
        return jt_norm
    raise ValueError(f"unsupported join_type for inversion: {jt!r}")


class JoinPathTooManyTerminals(Exception):
    """Сигнал caller'у откатиться на greedy: терминалов больше max_terminals."""


def _edge_key(e: Dict[str, Any]) -> Tuple[Any, ...]:
    """Мастер tie-break для рёбер: (weight, a, b, a_col, b_col).

    Используется ВЕЗДЕ (сортировка входа, adjacency, дедуп, MST), чтобы
    результат был детерминирован при равных весах.
    """
    return (
        e["weight"],
        e["a"],
        e["b"],
        str(e.get("constraint_id") or ""),
        _column_pairs(e),
    )


def _column_pairs(e: Dict[str, Any]) -> tuple[tuple[str, str], ...]:
    raw_pairs = e.get("column_pairs")
    if raw_pairs is None:
        return ((e["a_col"], e["b_col"]),)
    if not isinstance(raw_pairs, (list, tuple)) or not raw_pairs:
        raise ValueError("column_pairs must be a non-empty sequence")
    pairs: list[tuple[str, str]] = []
    for pair in raw_pairs:
        if not isinstance(pair, dict):
            raise ValueError("column_pairs members must be mappings")
        source = pair.get("from_column")
        target = pair.get("to_column")
        if not isinstance(source, str) or not source:
            raise ValueError("column_pairs.from_column is required")
        if not isinstance(target, str) or not target:
            raise ValueError("column_pairs.to_column is required")
        pairs.append((source, target))
    if pairs[0] != (e["a_col"], e["b_col"]):
        raise ValueError("scalar edge aliases must equal the first column pair")
    return tuple(pairs)


class _UnionFind:
    """Union-find для Kruskal."""

    def __init__(self) -> None:
        self._parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, x: str, y: str) -> bool:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return False
        # детерминированное присоединение по имени корня
        if rx < ry:
            self._parent[ry] = rx
        else:
            self._parent[rx] = ry
        return True


def _undirected_pair(e: Dict[str, Any]) -> frozenset:
    return frozenset((e["a"], e["b"]))


def _dedup_key(e: Dict[str, Any]) -> Tuple[Any, ...]:
    """Symmetric identity for one whole constraint edge."""
    endpoints = tuple(
        frozenset(((e["a"], source), (e["b"], target)))
        for source, target in _column_pairs(e)
    )
    return (_undirected_pair(e), str(e.get("constraint_id") or ""), endpoints)


def _build_adjacency(edges: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Список смежности. Каждое ребро попадает в списки обоих концов.

    ``edges`` уже отсортированы по ``_edge_key``, поэтому соседние списки
    тоже упорядочены детерминированно.
    """
    adj: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges:
        adj.setdefault(e["a"], []).append(e)
        adj.setdefault(e["b"], []).append(e)
    return adj


def _dijkstra(
    source: str,
    adj: Dict[str, List[Dict[str, Any]]],
) -> Tuple[Dict[str, float], Dict[str, List[Dict[str, Any]]]]:
    """Кратчайшие пути по weight от source ко всем достижимым узлам.

    Возвращает (dist, path_edges): ``path_edges[node]`` — список исходных
    рёбер кратчайшего пути source→node (в порядке от source). Строгое ``<``
    при релаксации; tie-break наследуется от детерминированного порядка
    adjacency (рёбра уже отсортированы по ``_edge_key``).
    """
    dist: Dict[str, float] = {source: 0.0}
    path_edges: Dict[str, List[Dict[str, Any]]] = {source: []}
    # heap-элемент: (dist, counter, node) — counter разрывает сравнение
    # при равных dist, не трогая node, и обеспечивает стабильность.
    counter = 0
    heap: List[Tuple[float, int, str]] = [(0.0, counter, source)]
    visited: Set[str] = set()

    while heap:
        d, _, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        for e in adj.get(node, []):
            other = e["b"] if e["a"] == node else e["a"]
            if other in visited:
                continue
            nd = d + e["weight"]
            if nd < dist.get(other, float("inf")):
                dist[other] = nd
                path_edges[other] = path_edges[node] + [e]
                counter += 1
                heapq.heappush(heap, (nd, counter, other))
    return dist, path_edges


def _mst(
    nodes: List[str],
    edges: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """MST (Kruskal + union-find) над заданными узлами и рёбрами.

    ``edges`` — список dict с ключами как у meta/исходных рёбер (нужны
    a, b, weight, a_col, b_col для tie-break). Tie-break: (cost, min(u,v),
    max(u,v)) реализуется сортировкой по ``_edge_key`` — она лексикографична
    по (weight, a, b, ...), а нормализация (a,b)→(min,max) обеспечивается
    каноникализацией ниже. Возвращает выбранные рёбра.
    """
    # Канонизируем направление для tie-break (min,max), сохраняя оригинал.
    def _sort_key(e: Dict[str, Any]) -> Tuple[Any, ...]:
        u, v = e["a"], e["b"]
        lo, hi = (u, v) if u <= v else (v, u)
        return (
            e["weight"],
            lo,
            hi,
            str(e.get("constraint_id") or ""),
            _column_pairs(e),
        )

    uf = _UnionFind()
    for n in nodes:
        uf.find(n)
    chosen: List[Dict[str, Any]] = []
    for e in sorted(edges, key=_sort_key):
        if uf.union(e["a"], e["b"]):
            chosen.append(e)
    return chosen


def _prune_nonterminal_leaves(
    edges: List[Dict[str, Any]],
    terminals: Set[str],
) -> List[Dict[str, Any]]:
    """Итеративная обрезка нетерминальных листьев из дерева ``edges``.

    Лист — узел степени 1. Пока есть лист не из terminals — удаляем его и
    инцидентное ребро (что может сделать листом родителя → повтор).
    """
    remaining = list(edges)
    changed = True
    while changed:
        changed = False
        degree: Dict[str, int] = {}
        for e in remaining:
            degree[e["a"]] = degree.get(e["a"], 0) + 1
            degree[e["b"]] = degree.get(e["b"], 0) + 1
        for e in list(remaining):
            for node in (e["a"], e["b"]):
                if degree.get(node, 0) == 1 and node not in terminals:
                    remaining.remove(e)
                    changed = True
                    break
            if changed:
                break
    return remaining


def _emit_clause(
    parent: str,
    e: Dict[str, Any],
    dsn: Optional[str],
) -> Tuple[str, Dict[str, Any]]:
    """Эмитит (clause, joins-entry) для ребра, подключаемого parent→child.

    Байт-идентично greedy (join_builder.py:77/91). forward: parent==a;
    reverse: parent==b → инверсия jt.
    """
    a, b = e["a"], e["b"]
    a_col, b_col = e["a_col"], e["b_col"]
    pairs = _column_pairs(e)
    jt = e["jt"]

    def Q(x: str) -> str:
        return quote_identifier(x, dsn=dsn)

    predicates = " AND ".join(
        f"{Q(a)}.{Q(source)} = {Q(b)}.{Q(target)}"
        for source, target in pairs
    )

    def entry(join_type: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "from_table": a,
            "from_column": a_col,
            "to_table": b,
            "to_column": b_col,
            "join_type": join_type,
        }
        if "column_pairs" in e:
            if e.get("constraint_id"):
                result["constraint_id"] = e["constraint_id"]
            result["column_pairs"] = [
                {"from_column": source, "to_column": target}
                for source, target in pairs
            ]
        return result

    if parent == a:
        # forward: подключаем b
        clause = f"{jt} JOIN {Q(b)} ON {predicates}"
        join_entry = entry(jt)
    else:
        # reverse: parent==b, подключаем a — инвертируем jt
        effective_jt = _invert_join_type(jt)
        clause = f"{effective_jt} JOIN {Q(a)} ON {predicates}"
        join_entry = entry(effective_jt)
    return clause, join_entry


def build_join_path(
    main_table: str,
    required_tables: Set[str],
    candidate_edges: List[Dict[str, Any]],
    *,
    max_terminals: int,
    dsn: Optional[str] = None,
) -> Dict[str, Any]:
    """Строит минимальный join-путь (KMB Steiner-аппроксимация).

    Args:
        main_table: корневая таблица (FROM), всегда терминал.
        required_tables: таблицы, которые обязаны быть связаны.
        candidate_edges: список рёбер-кандидатов; каждое —
            ``{a, b, a_col, b_col, jt, weight, source}``; ``jt`` уже
            UPPER-валиден (как после _normalize_edges).
        max_terminals: порог fail-fast. Если терминалов больше —
            :class:`JoinPathTooManyTerminals` (caller откатится на greedy).
        dsn: DSN для диалект-aware квотирования; None → ANSI.

    Returns:
        Dict по контракту :meth:`JoinBuilder.build_joins` (join_builder.py:116):
        ``success`` (bool), ``join_clauses`` (List[str]), ``joins``
        (List[dict]), ``used_tables`` (set), ``unconnected_tables`` (set).

    Raises:
        JoinPathTooManyTerminals: при ``len(required_tables) > max_terminals``
            (после тривиального short-circuit).
    """
    # Терминалы = required ∪ {main_table}.
    terminals: Set[str] = set(required_tables) | {main_table}

    # Тривиальный short-circuit: связывать нечего.
    if required_tables - {main_table} == set():
        return {
            "success": True,
            "join_clauses": [],
            "joins": [],
            "used_tables": {main_table},
            "unconnected_tables": set(),
        }

    # Fail-fast по числу терминалов (после short-circuit).
    if len(required_tables) > max_terminals:
        raise JoinPathTooManyTerminals(
            f"required_tables={len(required_tables)} > max_terminals={max_terminals}"
        )

    # Сортируем рёбра на входе по мастер tie-break — детерминизм всего ниже.
    edges = sorted(candidate_edges, key=_edge_key)
    adj = _build_adjacency(edges)

    terminal_list = sorted(terminals)

    # --- Шаг 1: метрическое замыкание G1 над терминалами ---
    # Для каждого терминала — Dijkstra; собираем мета-рёбра между парами
    # достижимых терминалов с сохранённым путём исходных рёбер.
    dist_from: Dict[str, Dict[str, float]] = {}
    path_from: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for t in terminal_list:
        d, p = _dijkstra(t, adj)
        dist_from[t] = d
        path_from[t] = p

    # Достижимые от main_table терминалы — только их можем связать.
    reachable_from_main = dist_from.get(main_table, {main_table: 0.0})
    connectable = [t for t in terminal_list if t in reachable_from_main]
    unconnectable = {t for t in terminals if t not in reachable_from_main}

    # Мета-рёбра полного графа на связной (с main) компоненте терминалов.
    meta_edges: List[Dict[str, Any]] = []
    for i in range(len(connectable)):
        for j in range(i + 1, len(connectable)):
            u, v = connectable[i], connectable[j]
            cost = dist_from[u].get(v)
            if cost is None:
                continue  # внутри компоненты main все связны, но на всякий
            meta_edges.append({
                "a": u,
                "b": v,
                "a_col": "",   # мета-ребро: реальных колонок нет, нужны для tie-break
                "b_col": "",
                "weight": cost,
                "path": path_from[u][v],
            })

    # --- Шаг 2: MST T1 замыкания ---
    t1 = _mst(connectable, meta_edges)

    # --- Шаг 3: разворот мета-рёбер в исходные + дедуп ---
    best_by_key: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for me in t1:
        for orig in me["path"]:
            key = _dedup_key(orig)
            prev = best_by_key.get(key)
            if prev is None or _edge_key(orig) < _edge_key(prev):
                best_by_key[key] = orig
    gs_edges = list(best_by_key.values())
    gs_nodes: Set[str] = set()
    for e in gs_edges:
        gs_nodes.add(e["a"])
        gs_nodes.add(e["b"])

    # --- Шаг 4: MST подграфа G_S + обрезка нетерминальных листьев ---
    ts = _mst(sorted(gs_nodes), gs_edges)
    pruned = _prune_nonterminal_leaves(ts, terminals)

    # --- Эмиссия: BFS от main_table, каждое ребро parent→child ---
    # adjacency дерева
    tree_adj: Dict[str, List[Dict[str, Any]]] = {}
    for e in pruned:
        tree_adj.setdefault(e["a"], []).append(e)
        tree_adj.setdefault(e["b"], []).append(e)

    join_clauses: List[str] = []
    join_edges: List[Dict[str, Any]] = []
    used_tables: Set[str] = {main_table}

    visited: Set[str] = {main_table}
    queue: List[str] = [main_table]
    while queue:
        parent = queue.pop(0)
        # детерминированный порядок обхода рёбер из узла
        for e in sorted(tree_adj.get(parent, []), key=_edge_key):
            child = e["b"] if e["a"] == parent else e["a"]
            if child in visited:
                continue
            clause, entry = _emit_clause(parent, e, dsn)
            join_clauses.append(clause)
            join_edges.append(entry)
            used_tables.add(child)
            visited.add(child)
            queue.append(child)

    unconnected_tables = (terminals - used_tables) | unconnectable
    # main_table сам всегда used; required недостижимые попадают сюда.
    unconnected_tables = {t for t in unconnected_tables if t != main_table}

    return {
        "success": len(unconnected_tables) == 0,
        "join_clauses": join_clauses,
        "joins": join_edges,
        "used_tables": used_tables,
        "unconnected_tables": unconnected_tables,
        "_selected_edges": pruned,
    }
