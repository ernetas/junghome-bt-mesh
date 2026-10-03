"""Property tests (Hypothesis) of `ProjectFile`: serialise → parse → serialise is a fixed point.

Any of the three fixture exports, edited by any sequence of the room / scene / device-name / subscription
mutators (names of any Unicode text), the app write-backs of review-4 F4-6 (a member's `sceneInfo` values, a new
node's actuator / button-layout rows, a removed node's rows dropped), rendered in either flavour and in any layout the apps write (indented or
compact, `": "` / `" : "` / `":"`, the inner payload wrapped or not, the header keys in any order), parses back
to the same tree, `meta` block, views and style — and renders to the very same bytes again. The in-memory views
the mutators keep up to date (`cdb.groups`, `cdb.scenes`, the scene / room names, subscriptions) are exactly
what a fresh load of the written file derives.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from jhmesh import vendor_models as V
from jhmesh.export import SHARE_KEYS, Layout, ProjectFile, Style, scene_infos

from .conftest import CDB_PATH, FIXTURES

SOURCES = {
    path.name: path.read_bytes()
    for path in (
        CDB_PATH,
        FIXTURES / "JungHome.json",
        FIXTURES / "JungHome-android.json",
    )
}

names = st.text(min_size=1, max_size=24).filter(lambda s: s.strip())
layouts = st.one_of(
    st.builds(
        Layout,
        st.sampled_from((1, 2, 4)),
        st.sampled_from((": ", " : ", ":")),
        st.just(","),
    ),
    st.builds(
        Layout,
        st.none(),
        st.sampled_from((": ", " : ", ":")),
        st.sampled_from((",", ", ")),
    ),
)
actions = st.one_of(
    st.builds(V.Action, st.just(V.ACTION_SWITCH), on=st.booleans()),
    st.builds(
        V.Action,
        st.just(V.ACTION_LIGHTNESS_CT),
        lightness=st.integers(0, V.LIGHTNESS_MAX),
        temperature_k=st.integers(800, 20000),
    ),
    st.builds(
        V.Action,
        st.just(V.ACTION_BLINDS),
        blind=st.integers(V.LEVEL_MIN, V.LEVEL_MAX),
        slat=st.integers(V.LEVEL_MIN, V.LEVEL_MAX),
    ),
    st.builds(
        V.Action,
        st.just(V.ACTION_TEMPERATURE),
        temperature_c=st.integers(500, 3000).map(lambda c: c / 100),
    ),
)
styles = st.builds(
    Style, layouts, layouts, st.booleans(), st.permutations(SHARE_KEYS).map(tuple)
)


def views(pf: ProjectFile) -> dict[str, Any]:
    """What a load derives from the tree, and the names the mutators read back."""
    cdb = pf.cdb
    return {
        "groups": cdb.groups,
        "scenes": cdb.scenes,
        "scene_names": cdb.scene_names,
        "rooms": pf.user_groups(),
        "all_scene_names": pf.scene_names(),
        "subscriptions": {
            (e.address, m["modelId"]): e.subscriptions(m["modelId"])
            for n in cdb.nodes
            for e in n.elements
            for m in e.raw_models
        },
        "publications": {
            (e.address, m["modelId"]): pf.publication(e, m["modelId"])
            for n in cdb.nodes
            for e in n.elements
            for m in e.raw_models
        },
    }


def mutate(pf: ProjectFile, data: st.DataObject) -> None:
    """A few edits the integration makes, each refused edit (a duplicate name, say) simply left out."""
    for op in data.draw(
        st.lists(
            st.sampled_from(
                (
                    "add_group",
                    "rename_group",
                    "remove_group",
                    "add_scene",
                    "rename_scene",
                    "remove_scene",
                    "name",
                    "sub",
                    "scene_info",
                    "property_rows",
                    "exclude",
                )
            ),
            max_size=6,
        )
    ):
        rooms, scenes = sorted(pf.user_groups()), sorted(pf.cdb.scenes)
        # the nodes of the moment: an exclusion builds the CDB anew
        elements = [e for n in pf.cdb.nodes for e in n.elements]
        try:
            if op == "add_group":
                pf.add_group(data.draw(names))
            elif op == "rename_group" and rooms:
                pf.rename_group(data.draw(st.sampled_from(rooms)), data.draw(names))
            elif op == "remove_group" and rooms:
                pf.remove_group(data.draw(st.sampled_from(rooms)))
            elif op == "add_scene":
                members = data.draw(
                    st.lists(st.sampled_from([e.address for e in elements]), max_size=4)
                )
                pf.add_scene(data.draw(names), addresses=members)
            elif op == "rename_scene" and scenes:
                pf.rename_scene(data.draw(st.sampled_from(scenes)), data.draw(names))
            elif op == "remove_scene" and scenes:
                pf.remove_scene(data.draw(st.sampled_from(scenes)))
            elif op == "name":
                node = data.draw(st.sampled_from(pf.cdb.nodes))
                pf.set_device_name(
                    node,
                    data.draw(st.lists(st.integers(0, 3), max_size=2)),
                    data.draw(names),
                )
            elif op == "sub" and pf.cdb.groups:
                element = data.draw(
                    st.sampled_from([e for e in elements if e.raw_models])
                )
                model = data.draw(
                    st.sampled_from([m["modelId"] for m in element.raw_models])
                )
                group = data.draw(st.sampled_from(sorted(pf.cdb.groups)))
                if data.draw(st.booleans()):
                    pf.subscribe(element, model, group)
                else:
                    pf.unsubscribe(element, model, group)
            elif op == "scene_info" and scenes:
                element = data.draw(st.sampled_from(elements))
                pf.set_scene_info(
                    data.draw(st.sampled_from(scenes)),
                    element.node,
                    element.location,
                    scene_infos(data.draw(actions)),
                )
            elif op == "property_rows":
                template, node = (
                    data.draw(st.sampled_from(pf.cdb.nodes)),
                    data.draw(st.sampled_from(pf.cdb.nodes)),
                )
                pf.clone_property_rows(
                    template,
                    node,
                    data.draw(st.none() | st.integers(0, 9)),
                    data.draw(st.none() | st.integers(0, 5)),
                )
            elif op == "exclude" and len(pf.cdb.nodes) > 1:
                pf.exclude_node(
                    data.draw(st.sampled_from(pf.cdb.nodes)),
                    data.draw(st.integers(0, 9)),
                )
        except ValueError:
            pass


@given(
    source=st.sampled_from(sorted(SOURCES)),
    flavour=st.sampled_from(("cdb", "share")),
    style=styles,
    data=st.data(),
)
def test_project_file_render_parse_render_is_stable(
    source: str, flavour: str, style: Style, data: st.DataObject
) -> None:
    pf = ProjectFile.loads(SOURCES[source])
    mutate(pf, data)
    pf.style = style
    text = pf.render(flavour)
    again = ProjectFile.loads(text.encode())
    assert again.flavour == flavour
    expected_style = style if flavour == "share" else Style(style.outer)
    assert again.style == expected_style
    assert again.net == json.loads(json.dumps(pf.net))
    expected, got = views(pf), views(again)
    if flavour == "share":
        assert again.meta == json.loads(json.dumps(pf.meta))
    else:  # a MeshNetwork.json has no `meta`: a load synthesises one from the CDB, names from `meta` are not in it
        del expected["all_scene_names"], got["all_scene_names"]
    assert got == expected
    assert again.render() == text
