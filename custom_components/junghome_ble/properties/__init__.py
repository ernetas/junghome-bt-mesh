"""The config entities' mesh side, apart from their Home Assistant entity classes (`config_entities.py`).

`targets.py` decides which entities exist: the property each one shows, the element it is addressed to and the
device it belongs to. `reader.py` reads and writes the values and keeps them in the hub's state cache through the
status handlers. Not to be confused with `jhmesh.properties`, the catalogue both build on.
"""
