"""Indexed directed graph queries with deterministic, iterative traversal."""
from collections import deque


class DependencyGraph:
    def __init__(self, paths, edges):
        self.forward = {path: set() for path in paths}
        self.reverse = {path: set() for path in paths}
        for source, target in edges:
            self.forward[source].add(target)
            self.reverse[target].add(source)
        self.forward = {p: tuple(sorted(v)) for p, v in self.forward.items()}
        self.reverse = {p: tuple(sorted(v)) for p, v in self.reverse.items()}

    def impact(self, changed):
        seeds = sorted(set(changed) & self.reverse.keys())
        distance, parent = dict.fromkeys(seeds, 0), dict.fromkeys(seeds)
        queue = deque(seeds)
        while queue:
            node = queue.popleft()
            for target in self.reverse[node]:
                if target not in distance:
                    distance[target] = distance[node] + 1
                    parent[target] = node
                    queue.append(target)
        return distance, parent

    def cycles(self):
        visited, order = set(), []
        for start in sorted(self.forward):
            if start in visited:
                continue
            visited.add(start)
            stack = [(start, iter(self.forward[start]))]
            while stack:
                node, children = stack[-1]
                target = next(children, None)
                if target is None:
                    order.append(node)
                    stack.pop()
                elif target not in visited:
                    visited.add(target)
                    stack.append((target, iter(self.forward[target])))
        visited, groups = set(), []
        for start in reversed(order):
            if start in visited:
                continue
            stack, component = [start], []
            visited.add(start)
            while stack:
                node = stack.pop()
                component.append(node)
                for target in self.reverse[node]:
                    if target not in visited:
                        visited.add(target)
                        stack.append(target)
            if len(component) > 1 or start in self.forward[start]:
                groups.append(sorted(component))
        return sorted(groups)
