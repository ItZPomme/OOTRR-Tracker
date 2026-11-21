"""PopTracker pack (ootrr_tracker): pack consistency, and the Lua logic against World / search.State on
random settings when the optional `lupa` module (Lua in Python) is installed."""

import json
import random
import unittest
from collections import Counter

from ootrr_tracker import poptracker as pt  # first: puts the OOTRR checkout on sys.path
from ootrr import settings as settings_mod
from ootrr.search import State
from ootrr.starting import resolve
from ootrr.world import World

try:
    from lupa import LuaRuntime
except ImportError:  # optional: pip install lupa
    LuaRuntime = None

GW = None
FILES = None


def pack():
    global GW, FILES
    if GW is None:
        GW = pt.GenericWorld()
        FILES = pt.build_pack(GW)
    return GW, FILES


class PackTest(unittest.TestCase):
    def test_pack_is_consistent(self):
        gw, files = pack()
        items = json.loads(files["items/items.json"]) + json.loads(files["items/settings.json"])
        codes = set()
        for item in items:
            for img in [item.get("img")] + [s["img"] for s in item.get("stages", [])]:
                if img:
                    self.assertIn(img, files)
            codes.add(item.get("codes") or item["stages"][0]["codes"])
        layout = json.dumps(json.loads(files["layouts/tracker.json"]))
        for code in codes:
            self.assertIn(f'"{code}"', layout)
        sections = [s["name"] for loc in json.loads(files["locations/locations.json"]) for s in loc["sections"]]
        self.assertEqual(sections, [loc.name for loc in gw.locations])
        self.assertEqual(len(set(sections)), len(sections))
        # every location of a world with every check on is in the pack
        world = World(settings_mod.build(dict(pt.MAX_SETTINGS, shuffle_skulltulas="all", shuffle_shops="4")))
        self.assertLessEqual(set(world.locations), set(sections))


def random_settings(rng, table):
    while True:
        cfg = {}
        for name, kind, values in table:
            if kind == "bool":
                cfg[name] = rng.random() < 0.5
            elif kind == "enum":
                cfg[name] = rng.choice(values)
        for name, kind, _ in table:
            if kind == "int":
                lo, hi = settings_mod.INT_RANGES.get(name, (0, {"stones": 3, "medallions": 6, "dungeons": 9}.get(
                    cfg.get("bridge"), 20)))
                cfg[name] = rng.randint(lo, hi)
        try:
            return cfg, settings_mod.build(resolve(settings_mod.build(cfg), 0))
        except settings_mod.SettingsError:
            continue


@unittest.skipIf(LuaRuntime is None, "lupa not installed")
class LuaLogicTest(unittest.TestCase):
    """With random settings, the generated Lua shows the same checks as World and finds the same ones reachable
    as search.State (child using every item)."""

    @staticmethod
    def oracle(world, inv):
        st = State(world)
        st.items, st.child_items, st.can_return = Counter(inv), Counter(inv), True
        while True:
            st.expand()
            new = [ev for ev in world.events if not st.items[ev.name] and st._event_ready(ev)]
            if not new:
                return st
            for ev in new:
                st.items[ev.name] += 1
                st.child_items[ev.name] += 1

    def test_same_as_python(self):
        gw, files = pack()
        maxima = pt.item_maxima(gw)
        rng = random.Random(11)
        lua = LuaRuntime()
        inv: dict[str, int] = {}
        cfg: dict = {}

        def find(_self, code):
            if code.startswith(pt.SETTING_PREFIX):
                name = code[len(pt.SETTING_PREFIX):]
                v = cfg[name]
                stage = values[name].index(v) if name in values else 0
                return lua.table_from({"Active": v is True, "AcquiredCount": v if type(v) is int else 0,
                                       "CurrentStage": stage})
            n = inv.get(code, 0)
            return lua.table_from({"Active": n > 0, "CurrentStage": n, "AcquiredCount": n})

        lua.globals().Tracker = lua.table_from({"FindObjectForCode": find})
        lua.execute(files["scripts/logic.lua"].decode())
        table = [(s[1], s[2], list(s[3].values()) if s[3] else None) for s in lua.globals().SETTING_ITEMS.values()]
        values = {name: vals for name, kind, vals in table if kind == "enum"}
        for _ in range(25):
            raw, settings = random_settings(rng, table)
            cfg.clear()
            cfg.update(raw)
            world = World(settings)
            shown = {gw.locations[i - 1].name for i in range(1, len(gw.locations) + 1)
                     if lua.globals().ootrr_vis(str(i))}
            expected = {l.name for l in world.locations.values() if l.shuffle and l.region != World.ROOT}
            self.assertEqual(shown, expected, raw)
            index = {loc.name: i for i, loc in enumerate(gw.locations, 1)}
            for _ in range(15):
                density = rng.random()
                inv.clear()
                inv.update({c: rng.randint(0, n) for c, n in maxima.items() if rng.random() < density})
                gated = {k: v for k, v in inv.items() if v and world.items and k in world.logic_items
                         and not (k in gw.gates and ((gw.gates[k] == "hearts" and settings["bridge"] != "hearts")
                                                     or (gw.gates[k] == "trade"
                                                         and not settings["adult_trade_sequence"])))}
                st = self.oracle(world, gated)
                for name in expected:
                    self.assertEqual(st.can_reach(world.locations[name]),
                                     lua.globals().ootrr_loc(str(index[name])), (raw, name, inv))


if __name__ == "__main__":
    unittest.main()
