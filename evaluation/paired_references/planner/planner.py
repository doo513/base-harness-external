def graph(tasks):
    if type(tasks) is not list:
        raise ValueError("tasks")
    dependencies = {}
    for task in tasks:
        if type(task) is not dict or set(task) - {"id", "requires"} or type(task.get("id")) is not str or not task["id"] or task["id"] in dependencies:
            raise ValueError("task")
        requires = task.get("requires", [])
        if type(requires) is not list or any(type(i) is not str or not i for i in requires) or len(set(requires)) != len(requires):
            raise ValueError("requires")
        dependencies[task["id"]] = set(requires)
    if any(not values <= dependencies.keys() or key in values for key, values in dependencies.items()):
        raise ValueError("dependency")
    done = set()
    while len(done) < len(dependencies):
        ready = {key for key, values in dependencies.items() if key not in done and values <= done}
        if not ready:
            raise ValueError("cycle")
        done |= ready
    return dependencies


def selection(values, dependencies):
    if type(values) is not list or any(type(i) is not str for i in values) or len(set(values)) != len(values) or not set(values) <= dependencies.keys():
        raise ValueError("selection")
    return set(values)


def plan(tasks, completed=None):
    dependencies = graph(tasks)
    done = selection([] if completed is None else completed, dependencies)
    waves = []
    while len(done) < len(dependencies):
        ready = sorted(key for key, values in dependencies.items() if key not in done and values <= done)
        waves.append(ready)
        done.update(ready)
    return waves


def impact(tasks, changed):
    dependencies = graph(tasks)
    affected = selection(changed, dependencies)
    while True:
        expanded = affected | {key for key, values in dependencies.items() if values & affected}
        if expanded == affected:
            return sorted(affected)
        affected = expanded
