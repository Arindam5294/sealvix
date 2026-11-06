def add_item(name, items=[]):
    """Return a new cart with `name` added. Each call without `items` starts an empty cart."""
    items.append(name)
    return items
