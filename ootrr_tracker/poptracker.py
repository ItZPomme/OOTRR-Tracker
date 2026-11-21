"""Builds the PopTracker pack (map tracker) of OoTRR, from the randomizer's own data and logic.

    python -m ootrr_tracker              # output/OoTRR-tracker/ and output/OoTRR-tracker.zip
    python -m ootrr_tracker --install    # also copies the zip into Documents/PopTracker/packs
    python -m ootrr_tracker --ootrr PATH # OOTRR checkout to read (default: $OOTRR_PATH, then ../OOTRR)

One pack for every seed: the settings are items of the tracker ("Settings" tab: click to change a value), read
by the logic at run time, like the native generator does (`native_logic`). The pack holds:
  - items: every logic item (toggles, progressive chains, counters for bottles and keys) and the settings that
    change the logic or the list of checks;
  - one schematic map (`tracker_map.json`, next to this file: a box per region, lines for the exits) with a pin per region and
    one section per location; a section is shown only when its location is shuffled with the chosen settings
    (same rules as `World._add_location`), and the start locations are hidden;
  - `scripts/logic.lua`: the rules of `data/**.json` translated to Lua (settings, dungeon keys and spawn exits
    included), and the same child / adult region search as `search.State` (fixpoint over exits and events),
    run again when an item or a setting changes.

No game file is used: the map and the icons are drawn here (5x7 pixel font, PNG written with zlib).
Known difference with the generator: the child is assumed to use every marked item (`State` keeps the items found
as adult away from the child until he can go back in time, which only matters with free_time_travel off).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import use_ootrr

use_ootrr()  # the OOTRR checkout (already chosen by --ootrr when run from __main__)

from ootrr import __version__, keys as keys_mod, settings as settings_mod
from ootrr.settings import GAME_LOCKED, INT_RANGES, SETTINGS
from ootrr.starting import adult_default
from ootrr.world import (DATA_DIR, GERUDO_CARD_LOCATION, LINKS_POCKET, OPEN_DOOR_START, SKIP_CHILD_ZELDA, World,
                     _read_json)
from ootrr.tools.make_icon import png

MAP_FILE = Path(__file__).with_name("tracker_map.json")

PACK_NAME = "OoTRR-tracker"
MAP_NAME = "Hyrule"
SETTING_PREFIX = "setting_"
# settings that change the list of checks (World._add_location, start locations); the settings named in the rules
# are added to them
STRUCTURAL = ("bridge", "starting_age", "open_door_of_time", "skip_child_zelda", "skip_cutscenes", "shuffle_rewards",
              "maps_compasses", "shuffle_hearts", "shuffle_scrubs", "shuffle_shops", "shuffle_skulltulas",
              "shuffle_ocarinas", "shuffle_songs", "shuffle_gifts", "gerudo_card", "adult_trade_sequence",
              "small_keys", "boss_keys")
DERIVED = ("spawn_point",)  # computed from starting_age, like starting.resolve
HIDDEN_VALUES = {"starting_age": ("random",)}  # the player picks the age the seed gave
BRIDGE_COUNT_MAX = 20
# logic items that only count with a setting (World: hearts with bridge = hearts, trade items with the trade quest)
GATES = {"Heart_Piece": "hearts", "Heart_Container": "hearts"}
# settings that make the item pool / every location exist, to read the items and their maxima
MAX_SETTINGS = {"bridge": "hearts", "adult_trade_sequence": True, "small_keys": "random", "boss_keys": "random"}


# ----------------------------------------------------------------------------------------------- Lua helpers
def lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def lua_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return lua_str(str(v))


def S(name: str) -> str:
    return f"S[{lua_str(name)}]"


def l_and(parts: list[str]) -> str:
    if "false" in parts:
        return "false"
    parts = [p for p in parts if p != "true"]
    if not parts:
        return "true"
    return parts[0] if len(parts) == 1 else "(" + " and ".join(parts) + ")"


def l_or(parts: list[str]) -> str:
    if "true" in parts:
        return "true"
    parts = [p for p in parts if p != "false"]
    if not parts:
        return "false"
    return parts[0] if len(parts) == 1 else "(" + " or ".join(parts) + ")"


def l_not(e: str) -> str:
    return {"true": "false", "false": "true"}.get(e, f"(not {e})")


def fn(body: str) -> str:
    return "function(age) return " + body + " end"


class LuaRules:
    """Translates rule sources to Lua expressions of `age`, with the name resolution of `rules.RuleCompiler`
    (helpers, then settings, then items / events). Settings stay variables (`S[...]`), like `native_logic`."""

    def __init__(self, items, events, helpers: dict[str, str], usable: dict[str, dict]) -> None:
        self.items, self.events, self.helpers, self.usable = set(items), set(events), helpers, usable
        self.used_settings: set[str] = set()

    def expr(self, src: str | bool) -> str:
        if isinstance(src, bool):
            return "true" if src else "false"
        text = str(src).strip()
        return self._bool(ast.parse(text, mode="eval").body) if text else "true"

    def _setting(self, name: str) -> str:
        if name not in SETTINGS:
            raise ValueError(f"unknown setting {name}")
        self.used_settings.add(name)
        return S(name)

    def _thing(self, name: str, n: int = 1) -> str:
        if name in self.events:
            return f"ev({lua_str(name)})" if n <= 1 else "false"
        if name not in self.items:
            raise ValueError(f"unknown item {name}")
        return f"has({lua_str(name)}, {n})" if n > 1 else f"has({lua_str(name)})"

    def _name(self, name: str) -> str:
        if name in ("True", "true"):
            return "true"
        if name in ("False", "false"):
            return "false"
        if name == "is_child":
            return '(age == "child")'
        if name == "is_adult":
            return '(age == "adult")'
        if name in self.helpers:
            return f"H[{lua_str(name)}](age)"
        if name in SETTINGS:
            return self._setting(name)
        return self._thing(name)

    @staticmethod
    def _arg(node: ast.expr) -> str:
        if not isinstance(node, ast.Name):
            raise ValueError("name expected")
        return node.id

    def _call(self, node: ast.Call) -> str:
        fname, args = self._arg(node.func), node.args
        if fname == "has":
            return self._thing(self._arg(args[0]), args[1].value)
        if fname == "can_use":
            item = self._arg(args[0])
            info = self.usable.get(item, {})
            parts = [f'(age == "{info["age"]}")'] if info.get("age") else []
            parts.append(self._thing(item))
            parts += [self._name(n) for n in info.get("needs", [])]
            return l_and(parts)
        if fname == "can_play":
            return l_and([self._thing("Ocarina"), self._thing(self._arg(args[0]))])
        if fname == "setting_is":
            return f"({self._setting(self._arg(args[0]))} == {lua_value(args[1].value)})"
        raise ValueError(f"unknown function {fname}")

    def _num(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
            return str(node.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "count":
                terms = [f"({self._thing(self._arg(a))} and 1 or 0)" for a in node.args]
                return "(" + " + ".join(terms) + ")" if terms else "0"
            if node.func.id == "hearts":
                return "hearts()"
            if node.func.id == "setting_value":
                return self._setting(self._arg(node.args[0]))
        raise ValueError("number expected")

    def _bool(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return "true" if node.value else "false"
        if isinstance(node, ast.Name):
            return self._name(node.id)
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, ast.Compare):
            ops = {ast.GtE: ">=", ast.Gt: ">", ast.LtE: "<=", ast.Lt: "<", ast.Eq: "==", ast.NotEq: "~="}
            return f"({self._num(node.left)} {ops[type(node.ops[0])]} {self._num(node.comparators[0])})"
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return l_not(self._bool(node.operand))
        if isinstance(node, ast.BoolOp):
            parts = [self._bool(v) for v in node.values]
            return l_and(parts) if isinstance(node.op, ast.And) else l_or(parts)
        raise ValueError(f"unsupported element {type(node).__name__}")


# ----------------------------------------------------------------------------------------------- generic world
@dataclass
class GLoc:
    name: str
    region: str
    body: dict
    dungeon: str | None
    key_kind: str | None = None  # key chest added by data/keys.json: "small" | "boss"
    kd: int | None = None  # index of the key dungeon of its region
    key_spot: bool = False  # small key spot when small_keys = own_dungeon (World._is_key_spot)
    boss: bool = False  # behind the boss door


class GenericWorld:
    """The world data with the settings left open: every region, exit, event and location, as World would load
    them for some settings (dungeon key data always added)."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        data_dir = Path(data_dir)
        # the world with every item and location in the logic: items, levels, chains and maxima (also checks the data)
        self.max_world = World(settings_mod.build(MAX_SETTINGS), data_dir)
        regions_raw: dict[str, dict] = {}
        extra: dict[str, dict] = {}
        for path in sorted((data_dir / "world").glob("*.json")):
            content = _read_json(path)
            regions_raw.update(content.get("regions", {}))
            extra.update({n: b for n, b in content.get("locations", {}).items() if not n.startswith("_")})
        self.kds = list(keys_mod.load(data_dir).values())
        key_rooms = keys_mod.load_rooms(data_dir)
        key_region = {r: i for i, kd in enumerate(self.kds) for r in kd.regions}
        key_events: set[str] = set()
        key_chests: dict[str, str] = {}
        for kd in self.kds:
            for rname, events in kd.events.items():
                regions_raw[rname] = {**regions_raw[rname], "events": {**regions_raw[rname].get("events", {}), **events}}
                key_events |= set(events)
            for i, chest in enumerate(kd.small):
                extra[kd.small_location(i)] = {"region": kd.region, "rule": kd.rule, "hashes": [chest["hash"]],
                                               "vanilla": "", "key_room": chest["room"]}
                key_chests[kd.small_location(i)] = keys_mod.SMALL
            if kd.boss_key:
                extra[kd.boss_location] = {"region": kd.region, "rule": kd.rule, "hashes": [kd.boss_key["hash"]],
                                           "vanilla": "", "key_room": kd.boss_key["room"]}
                key_chests[kd.boss_location] = keys_mod.BOSS
        from ootrr.starting import add_spawn_exits
        regions_raw = add_spawn_exits(regions_raw)

        self.dungeon = {name: body.get("dungeon") for name, body in regions_raw.items()}
        self.exits = [(r, t, src) for r, body in regions_raw.items() for t, src in body.get("exits", {}).items()]
        self.events = []  # (name, region, rule, key dungeon or None, boss)
        for r, body in regions_raw.items():
            for e, src in body.get("events", {}).items():
                k = key_region.get(r) if e not in key_events else None
                self.events.append((e, r, src, k, k is not None and e in self.kds[k].boss_events))
        self.locations: list[GLoc] = []
        bodies = [(n, r, b) for r, body in regions_raw.items() for n, b in body.get("locations", {}).items()]
        bodies += [(n, b.get("region", ""), b) for n, b in extra.items()]
        for name, region, body in bodies:
            if isinstance(body, str):
                body = {"rule": body}
            k = key_region.get(region)
            loc = GLoc(name, region, body, self.dungeon[region], key_chests.get(name), k)
            if k is not None:
                kd = self.kds[k]
                loc.boss = name in kd.boss
                room = body.get("key_room", key_rooms.get(name))
                src = str(body.get("rule", "True"))
                loc.key_spot = (region == kd.region and room is not None and room in kd.key_rooms
                                and bool(body.get("hashes")) and body.get("kind", "general") == "general"
                                and not any(body.get(f) for f in ("pickup", "scrub", "skulltula", "shop", "gift",
                                                                  "ocarina"))
                                and (src.strip() == "True" or keys_mod.conjuncts(src) <= keys_mod.conjuncts(kd.rule)))
            self.locations.append(loc)
        order = {r: i for i, r in enumerate(regions_raw)}
        self.locations.sort(key=lambda loc: order[loc.region])  # one block per region (pin visibility)
        mw = self.max_world
        self.logic_items = mw.logic_items
        event_names = {e[0] for e in self.events}
        usable = {n: b["use"] for n, b in self.logic_items.items() if b.get("use")}
        self.rules = LuaRules(self.logic_items, event_names, mw.compiler.helper_src, usable)
        # trade-only logic items: every actor that gives them is a trade item
        givers: dict[str, list[bool]] = {}
        for name, body in _read_json(data_dir / "items.json")["actors"].items():
            if body.get("logic"):
                givers.setdefault(body["logic"], []).append(bool(body.get("trade")))
        self.gates = dict(GATES, **{logic: "trade" for logic, trade in givers.items() if all(trade)})

    # ------------------------------------------------------------------ visibility (World._add_location)
    def shuffle_expr(self, loc: GLoc) -> str:
        """Lua expression: is the location a check (shuffled, not a start location) with the settings S."""
        b = loc.body
        if loc.key_kind:
            return f'({S(keys_mod.SETTING[loc.key_kind])} ~= "vanilla")'
        e = "true" if b.get("shuffle", bool(b.get("hashes"))) else "false"
        if b.get("dungeon_item"):
            e = l_and([f'({S("maps_compasses")} ~= "vanilla")', e])
        pickup = b.get("pickup")
        if pickup == "item":
            e = S("shuffle_gifts")
        elif pickup:
            e = l_or([f'({S("shuffle_hearts")} == "all")',
                      f'({S("shuffle_hearts")} == "freestanding")' if pickup == "freestanding" else "false"])
        if b.get("scrub"):
            e = S("shuffle_scrubs")
        if b.get("skulltula"):
            e = l_or([f'({S("shuffle_skulltulas")} == "all")',
                      f'({S("shuffle_skulltulas")} == "overworld")' if loc.dungeon is None else "false"])
        if b.get("shop"):
            e = f"(shops() >= {int(b['shop'])})"
        if b.get("ocarina"):
            e = S("shuffle_ocarinas")
        if b.get("kind") == "song":
            e = f'({S("shuffle_songs")} == "random")'
        if b.get("gift"):
            e = S("shuffle_gifts")
        if loc.name == GERUDO_CARD_LOCATION:
            e = l_and([e, f'({S("gerudo_card")} ~= "vanilla")'])
        start = []
        if loc.name in OPEN_DOOR_START:
            start.append(S("open_door_of_time"))
        if loc.name in SKIP_CHILD_ZELDA:
            start.append(l_and([l_not(S("open_door_of_time")), S("skip_child_zelda")]))
        if loc.name == LINKS_POCKET:
            start.append("skip_rauru()")
        if start:
            e = l_and([e, l_not(l_or(start))])
        return e

    def rule_expr(self, src, k: int | None, spot: bool, boss: bool) -> str:
        e = self.rules.expr(src)
        if k is None:
            return e
        return l_and([e, f"key_ok({k + 1}, {'true' if spot else 'false'}, {'true' if boss else 'false'}, age)"])


LUA_RUNTIME = r"""
local AGES = {"child", "adult"}
local C = {}            -- item counts, read once per update
local EV = {}           -- events reached
local REACH = {child = {}, adult = {}}
local VIS = {}          -- location index -> shown
local last_key = nil
S = {}                  -- settings

local function count_of(code)
  local kind = KIND[code]
  local o = Tracker:FindObjectForCode(code)
  if not o then return 0 end
  if kind == "consumable" then return o.AcquiredCount end
  if kind == "progressive" then return o.CurrentStage end
  return o.Active and 1 or 0
end

function has(code, n) return (C[code] or 0) >= (n or 1) end
function ev(name) return EV[name] == true end
function hearts() return 3 + (C["Heart_Container"] or 0) + math.floor((C["Heart_Piece"] or 0) / 4) end
function shops() return tonumber(S["shuffle_shops"]) or 0 end
function skip_rauru() return S["skip_cutscenes"] == "safe" or S["shuffle_rewards"] ~= "vanilla" end

local SHUFFLED_KEYS = {own_dungeon = true, any_dungeon = true, random = true}
-- extra rule of a dungeon with keys (World._key_rule): k = KD index, spot = own_dungeon key spot, boss = behind
-- the boss door
function key_ok(k, spot, boss, age)
  local d = KD[k]
  local sm = S["small_keys"]
  if SHUFFLED_KEYS[sm] then
    if spot and sm == "own_dungeon" then
      if not d.rule(age) then return false end
    else
      local n = d.small + ((sm == "any_dungeon" or sm == "random") and d.free or 0)
      if not has(d.sk, n) then return false end
    end
  end
  if boss and d.bk and SHUFFLED_KEYS[S["boss_keys"]] then return has(d.bk) end
  return true
end

local function read_settings(parts)
  for k, v in pairs(SETTING_DEFAULTS) do S[k] = v end
  for _, s in ipairs(SETTING_ITEMS) do
    local o = Tracker:FindObjectForCode("setting_" .. s[1])
    if o then
      if s[2] == "bool" then S[s[1]] = o.Active and true or false
      elseif s[2] == "int" then S[s[1]] = o.AcquiredCount
      else S[s[1]] = s[3][o.CurrentStage + 1] or s[3][1] end
    end
    parts[#parts + 1] = tostring(S[s[1]])
  end
  -- starting.resolve: the adult start opens the Door of Time, frees time travel and spawns in the Temple of Time
  if S["starting_age"] == "adult" then
    S["open_door_of_time"] = true
    S["free_time_travel"] = true
    S["spawn_point"] = ADULT_SPAWN
  else
    S["spawn_point"] = 0
  end
end

local function update()
  local parts = {}
  read_settings(parts)
  for _, code in ipairs(CODES) do
    local n = count_of(code)
    local gate = GATE[code]
    if (gate == "hearts" and S["bridge"] ~= "hearts") or (gate == "trade" and not S["adult_trade_sequence"]) then
      n = 0
    end
    C[code] = n
    parts[#parts + 1] = n
  end
  local key = table.concat(parts, ",")
  if key == last_key then return end
  last_key = key
  for i, l in ipairs(LOCS) do VIS[i] = l[3]() and true or false end
  EV = {}
  REACH = {child = {Root = true}, adult = {Root = true}}
  local changed = true
  while changed do
    changed = false
    for _, e in ipairs(EXITS) do
      for _, age in ipairs(AGES) do
        local r = REACH[age]
        if r[e[1]] and not r[e[2]] and e[3](age) then
          r[e[2]] = true
          changed = true
        end
      end
    end
    for _, e in ipairs(EVENTS) do
      if not EV[e[1]] then
        for _, age in ipairs(AGES) do
          if REACH[age][e[2]] and e[3](age) then
            EV[e[1]] = true
            changed = true
            break
          end
        end
      end
    end
  end
end

-- access rule of a section: "$ootrr_loc|<index>"
function ootrr_loc(index)
  update()
  local l = LOCS[tonumber(index)]
  for _, age in ipairs(AGES) do
    if REACH[age][l[1]] and l[2](age) then return true end
  end
  return false
end

-- visibility rule of a section: "$ootrr_vis|<index>" (the location is a check with these settings)
function ootrr_vis(index)
  update()
  return VIS[tonumber(index)] == true
end

-- visibility rule of a map pin: "$ootrr_region_vis|<first index>|<last index>"
function ootrr_region_vis(first, last)
  update()
  for i = tonumber(first), tonumber(last) do
    if VIS[i] then return true end
  end
  return false
end
"""


def build_logic_lua(gw: GenericWorld, kinds: dict[str, str], settings: list[tuple]) -> str:
    """settings: (name, kind, values) of the setting items."""
    tr = gw.rules
    out = ["-- OoTRR logic, generated by ootrr_tracker from ootrr/data (do not edit: regenerate the pack)", ""]
    out.append("KIND = {" + ", ".join(f"[{lua_str(c)}] = {lua_str(k)}" for c, k in kinds.items()) + "}")
    out.append("CODES = {" + ", ".join(lua_str(c) for c in kinds) + "}")
    out.append("GATE = {" + ", ".join(f"[{lua_str(c)}] = {lua_str(g)}" for c, g in gw.gates.items()) + "}")
    out.append(f"ADULT_SPAWN = {adult_default()}")
    out.append("SETTING_DEFAULTS = {" + ", ".join(
        f"[{lua_str(n)}] = {lua_value(d)}" for n, (d, _, _) in SETTINGS.items()) + "}")
    out.append("SETTING_ITEMS = {")
    for name, kind, values in settings:
        vals = "{" + ", ".join(lua_value(v) for v in values) + "}" if values else "nil"
        out.append(f"  {{{lua_str(name)}, {lua_str(kind)}, {vals}}},")
    out.append("}")
    out.append("H = {}")
    for name, src in tr.helpers.items():
        out.append(f"H[{lua_str(name)}] = {fn(tr.expr(src))}")
    out.append("KD = {")
    for kd in gw.kds:
        bk = lua_str(keys_mod.logic_name(keys_mod.BOSS, kd.name)) if kd.boss_key else "nil"
        out.append(f"  {{sk = {lua_str(keys_mod.logic_name(keys_mod.SMALL, kd.name))}, small = {len(kd.small)}, "
                   f"free = {len(kd.freestanding)}, bk = {bk}, rule = {fn(tr.expr(kd.rule))}}}, -- {kd.name}")
    out.append("}")
    out.append("EXITS = {")
    for src, dst, rule in gw.exits:
        body = tr.expr(rule)
        if body != "false":
            out.append(f"  {{{lua_str(src)}, {lua_str(dst)}, {fn(body)}}},")
    out.append("}")
    out.append("EVENTS = {")
    for name, region, rule, k, boss in gw.events:
        body = gw.rule_expr(rule, k, False, boss)
        if body != "false":
            out.append(f"  {{{lua_str(name)}, {lua_str(region)}, {fn(body)}}},")
    out.append("}")
    out.append("LOCS = {")
    for i, loc in enumerate(gw.locations, 1):
        rule = gw.rule_expr(loc.body.get("rule", "True"), loc.kd, loc.key_spot, loc.boss)
        out.append(f"  {{{lua_str(loc.region)}, {fn(rule)}, function() return {gw.shuffle_expr(loc)} end}},"
                   f" -- {i} {loc.name}")
    out.append("}")
    return "\n".join(out) + "\n" + LUA_RUNTIME


# ----------------------------------------------------------------------------------------------- drawing
FONT = {  # 5x7, one hex value per row (bit 4 = left column)
    "A": "0E11111F111111", "B": "1E11111E11111E", "C": "0E11101010110E", "D": "1C12111111121C",
    "E": "1F10101E10101F", "F": "1F10101E101010", "G": "0E11101711110F", "H": "1111111F111111",
    "I": "0E04040404040E", "J": "0702020202120C", "K": "11121418141211", "L": "1010101010101F",
    "M": "111B1515111111", "N": "11111915131111", "O": "0E11111111110E", "P": "1E11111E101010",
    "Q": "0E11111115120D", "R": "1E11111E141211", "S": "0F10100E01011E", "T": "1F040404040404",
    "U": "1111111111110E", "V": "11111111110A04", "W": "1111111515150A",
    "X": "11110A040A1111", "Y": "1111110A040404", "Z": "1F01020408101F",
    "0": "0E11131519110E", "1": "040C040404040E", "2": "0E11010204081F",
    "3": "1F02040201110E", "4": "02060A121F0202", "5": "1F101E0101110E", "6": "0608101E11110E",
    "7": "1F010204080808", "8": "0E11110E11110E", "9": "0E11110F01020C",
    " ": "00000000000000", "-": "0000001F000000", ".": "00000000000C0C", "'": "0C040800000000",
    "/": "00010204081000", "(": "02040808080402", ")": "08040202020408", "+": "0004041F040400",
    "&": "0C12140815120D", "?": "0E110102040004",
}


def hexrgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


class Canvas:
    def __init__(self, w: int, h: int, bg: str) -> None:
        self.w, self.h = w, h
        self.px = bytearray(bytes((*hexrgb(bg), 255)) * (w * h))

    def rect(self, x0: int, y0: int, x1: int, y1: int, color: str) -> None:
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(self.w, x1), min(self.h, y1)
        if x1 <= x0:
            return
        row = bytes((*hexrgb(color), 255)) * (x1 - x0)
        for y in range(y0, y1):
            i = (y * self.w + x0) * 4
            self.px[i:i + len(row)] = row

    def box(self, x0: int, y0: int, x1: int, y1: int, fill: str, border: str, t: int = 2) -> None:
        self.rect(x0, y0, x1, y1, border)
        self.rect(x0 + t, y0 + t, x1 - t, y1 - t, fill)

    def line(self, x0: int, y0: int, x1: int, y1: int, color: str, t: int = 3) -> None:
        n = max(abs(x1 - x0), abs(y1 - y0), 1)
        h = t // 2
        for k in range(n + 1):
            x, y = x0 + (x1 - x0) * k // n, y0 + (y1 - y0) * k // n
            self.rect(x - h, y - h, x - h + t, y - h + t, color)

    def text(self, x: int, y: int, s: str, color: str, scale: int = 2) -> None:
        for ch in s.upper():
            rows = FONT.get(ch, FONT["?"])
            for r in range(7):
                bits = int(rows[r * 2:r * 2 + 2], 16)
                for c in range(5):
                    if bits & (0x10 >> c):
                        self.rect(x + c * scale, y + r * scale, x + (c + 1) * scale, y + (r + 1) * scale, color)
            x += 6 * scale

    @staticmethod
    def text_width(s: str, scale: int = 2) -> int:
        return len(s) * 6 * scale - scale

    def png(self) -> bytes:
        stride = self.w * 4
        return png([bytes(self.px[y * stride:(y + 1) * stride]) for y in range(self.h)])


def wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines


# ----------------------------------------------------------------------------------------------- items
SHORT = {  # icon text (one or two lines of up to 5 characters)
    "Progressive_Hookshot": ["HOOK"], "Bomb_Bag": ["BOMB", "BAG"], "Bow": ["BOW"], "Slingshot": ["SLING"],
    "Progressive_Strength": ["STR"], "Progressive_Scale": ["SCALE"], "Magic_Meter": ["MAGIC"],
    "Ocarina": ["OCA"], "Bottle": ["BTL"], "Rutos_Letter": ["RUTO", "LTR"], "Boomerang": ["BOOM"],
    "Lens_of_Truth": ["LENS"], "Megaton_Hammer": ["HAMMR"], "Iron_Boots": ["IRON", "BOOTS"],
    "Hover_Boots": ["HOVER", "BOOTS"], "Mirror_Shield": ["MIRR", "SHLD"], "Goron_Tunic": ["GORON", "TUNIC"],
    "Zora_Tunic": ["ZORA", "TUNIC"], "Kokiri_Sword": ["KOK", "SWORD"], "Dins_Fire": ["DIN"],
    "Fire_Arrows": ["FIRE", "ARROW"], "Ice_Arrows": ["ICE", "ARROW"], "Light_Arrows": ["LIGHT", "ARROW"],
    "Gerudo_Membership_Card": ["GERU", "CARD"], "Zeldas_Letter": ["ZELDA", "LTR"], "Pocket_Egg": ["EGG"],
    "Zeldas_Lullaby": ["ZL"], "Eponas_Song": ["EPONA"], "Sarias_Song": ["SARIA"], "Suns_Song": ["SUN"],
    "Song_of_Time": ["TIME"], "Song_of_Storms": ["STORM"], "Minuet_of_Forest": ["MIN"],
    "Bolero_of_Fire": ["BOL"], "Serenade_of_Water": ["SER"], "Requiem_of_Spirit": ["REQ"],
    "Nocturne_of_Shadow": ["NOC"], "Prelude_of_Light": ["PRE"], "Heart_Piece": ["HEART", "PIECE"],
    "Heart_Container": ["HEART", "CONT"], "Kokiri_Emerald": ["KOK", "EMER"], "Goron_Ruby": ["GORON", "RUBY"],
    "Zora_Sapphire": ["ZORA", "SAPH"], "Forest_Medallion": ["FORST"], "Fire_Medallion": ["FIRE"],
    "Water_Medallion": ["WATER"], "Spirit_Medallion": ["SPIRT"], "Shadow_Medallion": ["SHADW"],
    "Light_Medallion": ["LIGHT"],
}
DUNGEON_SHORT = {"Forest_Temple": "FRST", "Fire_Temple": "FIRE", "Water_Temple": "WATR", "Shadow_Temple": "SHDW",
                 "Spirit_Temple": "SPRT", "Bottom_of_the_Well": "BOTW", "Gerudo_Training_Ground": "GTG",
                 "Ganons_Castle": "GANON"}
STAGE_SHORT = {"Item_Hookshot": ["HOOK", "SHOT"], "Item_Longshot": ["LONG", "SHOT"],
               "Item_Gauntlet_Goron": ["GORON", "BRACE"], "Item_Gauntlet_Silver": ["SILV", "GAUNT"],
               "Item_Gauntlet_Gold": ["GOLD", "GAUNT"], "Item_Scale_Silver": ["SILV", "SCALE"],
               "Item_Scale_Gold": ["GOLD", "SCALE"]}
COLORS = {"equipment": ("#2c4a6e", "#8fb4e0"), "song": ("#4b2f6e", "#c3a2f0"), "reward": ("#2f5e3a", "#9fdcae"),
          "key": ("#6e5a20", "#f0d57a"), "heart": ("#6e2c38", "#f09aa8"),
          "setting": ("#3a3f4a", "#c0c6d2")}


def label(code: str) -> str:
    return code.replace("_", " ")


def actor_label(actor: str) -> str:
    name = re.sub(r"^(Item|Obj|Weapon|Armor)_", "", actor).replace("_", " ")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def category(code: str) -> str:
    if code.startswith(("Small_Key_", "Boss_Key_")):
        return "key"
    if code.startswith("Heart_"):
        return "heart"
    if code.endswith(("_Medallion", "_Emerald", "_Ruby", "_Sapphire")):
        return "reward"
    if code in SONGS:
        return "song"
    return "equipment"


SONGS = ["Zeldas_Lullaby", "Eponas_Song", "Sarias_Song", "Suns_Song", "Song_of_Time", "Song_of_Storms",
         "Minuet_of_Forest", "Bolero_of_Fire", "Serenade_of_Water", "Requiem_of_Spirit", "Nocturne_of_Shadow",
         "Prelude_of_Light"]
REWARDS = ["Kokiri_Emerald", "Goron_Ruby", "Zora_Sapphire", "Forest_Medallion", "Fire_Medallion",
           "Water_Medallion", "Spirit_Medallion", "Shadow_Medallion", "Light_Medallion"]


def short_text(code: str) -> list[str]:
    if code in SHORT:
        return SHORT[code]
    for prefix, word in (("Small_Key_", "KEY"), ("Boss_Key_", "BOSS")):
        if code.startswith(prefix):
            rest = code[len(prefix):]
            return [word, DUNGEON_SHORT.get(rest, rest[:5].upper())]
    words = code.upper().split("_")
    return [w[:5] for w in words[:2]]


def icon(lines: list[str], cat: str) -> bytes:
    fill, border = COLORS[cat]
    c = Canvas(64, 64, "#000000")
    c.px = bytearray(64 * 64 * 4)  # transparent
    c.box(2, 2, 62, 62, fill, border, 3)
    lh = 16
    y = 32 - (len(lines) * lh - 2) // 2
    for line in lines:
        c.text(32 - c.text_width(line) // 2, y, line, "#ffffff")
        y += lh
    return c.png()


def item_maxima(gw: GenericWorld) -> dict[str, int]:
    """Highest count of each logic item: every location's vanilla item collected like `World.collect` does
    (levels keep the maximum), small keys = chest keys + ground keys (any_dungeon / random)."""
    world = gw.max_world
    inv: Counter[str] = Counter()
    for loc in world.locations.values():
        world.collect(inv, loc.vanilla)
    for kd in gw.kds:
        inv[keys_mod.logic_name(keys_mod.SMALL, kd.name)] = len(kd.small) + len(kd.freestanding)
    return {k: v for k, v in inv.items() if k in world.logic_items and v > 0}


def build_items(gw: GenericWorld, maxima: dict[str, int], images: dict[str, bytes]) -> tuple[list, dict[str, str]]:
    world = gw.max_world
    stages: dict[str, list[tuple[int, str]]] = {}
    for item in world.items.values():
        if item.logic and item.level is not None:
            stages.setdefault(item.logic, []).append((item.level, item.name))
    for chain in world.progressive:  # counted chains (Hookshot -> Longshot…): step k = k copies
        logic = {world.items[a].logic for a in chain if a in world.items}
        if len(logic) == 1 and None not in logic:
            stages.setdefault(logic.pop(), [(k, a) for k, a in enumerate(chain, 1)])
    items, kinds = [], {}
    order = sorted(maxima, key=lambda c: (["equipment", "song", "reward", "heart", "key"].index(category(c)),
                                          (SONGS + REWARDS).index(c) if c in SONGS + REWARDS else 0, c))
    for code in order:
        n, cat = maxima[code], category(code)
        img = f"images/items/{code}.png"
        images[img] = icon(short_text(code), cat)
        if code in stages and n > 1:
            best: dict[int, str] = {}
            for level, actor in sorted(set(stages[code])):
                best.setdefault(level, actor)
            st = [{"name": f"{label(code)} (none)", "img": img, "img_mods": "@disabled", "codes": code}]
            for level in range(1, n + 1):
                actor = best.get(level, f"{code}_{level}")
                simg = f"images/items/{code}_{level}.png"
                images[simg] = icon(STAGE_SHORT.get(actor, short_text(code) + [str(level)])[:2], cat)
                st.append({"name": actor_label(actor), "img": simg, "codes": f"{code},{code}_{level}"})
            items.append({"name": label(code), "type": "progressive", "loop": False, "allow_disabled": False,
                          "stages": st})
            kinds[code] = "progressive"
        elif n > 1:
            items.append({"name": label(code), "type": "consumable", "img": img, "codes": code,
                          "min_quantity": 0, "max_quantity": n, "increment": 1, "decrement": 1})
            kinds[code] = "consumable"
        else:
            items.append({"name": label(code), "type": "toggle", "img": img, "codes": code})
            kinds[code] = "toggle"
    return items, kinds


# ----------------------------------------------------------------------------------------------- settings items
SETTING_SHORT = {
    "bridge": "BRIDG", "bridge_count": "BRIDG", "open_forest": "FORST", "open_door_of_time": ["DOOR", "TIME"],
    "open_kakariko": ["KAK", "GATE"], "maps_compasses": "MAPS", "shuffle_hearts": "HEART",
    "shuffle_scrubs": ["SCRUB", "SHUF"], "shuffle_shops": "SHOPS", "shuffle_skulltulas": "SKULL",
    "skip_cutscenes": "CUTSC", "shuffle_rewards": "REWRD", "free_time_travel": ["FREE", "TIME"],
    "skip_child_zelda": ["SKIP", "ZELDA"], "logic_lens_required": ["LENS", "REQ"],
    "shuffle_ocarinas": ["OCA", "SHUF"], "shuffle_gifts": ["GIFTS", "SHUF"], "adult_trade_sequence": ["TRADE", "QUEST"],
    "starting_age": "AGE", "shuffle_songs": "SONGS", "cucco_count": "CUCCO", "big_poe_count": "POES",
    "complete_mask_quest": ["MASK", "DONE"], "fast_bunny_hood": ["BUNNY", "FAST"], "blue_fire_arrows": ["BLUE", "FIRE"],
    "small_keys": "KEYS", "boss_keys": "BOSS", "gerudo_card": "CARD", "gerudo_fortress": "FORT", "logic": "LOGIC",
}
VALUE_SHORT = {"closed": "CLOSE", "closed_deku": "DEKU", "vanilla": "VANIL", "medallions": "MEDAL",
               "dungeons": "DUNG", "start_with": "START", "shuffle": "SHUF", "shuffled": "SHUF", "removed": "NONE",
               "freestanding": "FREE", "overworld": "OVERW", "own_dungeon": "OWN", "any_dungeon": "ANY",
               "random": "RAND", "normal": "NORM", "glitchless": "GLTCH"}


def setting_items(used: set[str], images: dict[str, bytes]) -> tuple[list, list[tuple], dict[str, object]]:
    """Tracker items of the settings in `used` (SETTINGS order): (items, [(name, kind, values)], defaults)."""
    items, table, defaults = [], [], {}
    for name, (default, allowed, desc) in SETTINGS.items():
        if name not in used or name in DERIVED or name in GAME_LOCKED:  # locked: default value only
            continue
        code = SETTING_PREFIX + name
        short = SETTING_SHORT.get(name, name[:5].upper())
        head = short if isinstance(short, list) else [short]
        title = name.replace("_", " ")
        if allowed is bool:
            img = f"images/settings/{name}.png"
            images[img] = icon(head, "setting")
            items.append({"name": f"{title} (on / off)", "type": "toggle", "img": img, "codes": code})
            table.append((name, "bool", None))
        elif allowed is int:
            lo, hi = INT_RANGES.get(name, (0, BRIDGE_COUNT_MAX))
            img = f"images/settings/{name}.png"
            images[img] = icon([head[0], "#"], "setting")
            items.append({"name": title, "type": "consumable", "img": img, "codes": code,
                          "min_quantity": lo, "max_quantity": hi, "increment": 1, "decrement": 1})
            table.append((name, "int", None))
        else:
            values = [v for v in allowed if v not in HIDDEN_VALUES.get(name, ())]
            stages = []
            for v in values:
                img = f"images/settings/{name}_{v}.png"
                images[img] = icon([head[0], VALUE_SHORT.get(v, str(v).upper()[:5])], "setting")
                stages.append({"name": f"{title}: {v}", "img": img, "codes": code})
            items.append({"name": title, "type": "progressive", "loop": True, "allow_disabled": False,
                          "stages": stages})
            table.append((name, "enum", values))
        defaults[name] = default
    return items, table, defaults


# ----------------------------------------------------------------------------------------------- layouts
def item_rows(codes: list[str], width: int = 8) -> list[list[str]]:
    return [codes[i:i + width] for i in range(0, len(codes), width)]


def build_layouts(kinds: dict[str, str], settings: list[tuple]) -> dict:
    codes = list(kinds)
    equip = [c for c in codes if category(c) in ("equipment", "heart")]
    songs = [c for c in SONGS if c in kinds]
    rewards = [c for c in REWARDS if c in kinds]
    keys: list[str] = []
    for c in codes:
        if c.startswith("Small_Key_"):
            boss = "Boss_Key_" + c[len("Small_Key_"):]
            keys += [c] + ([boss] if boss in kinds else [])
    keys += [c for c in codes if c.startswith("Boss_Key_") and c not in keys]

    def group(header: str, rows: list[list[str]]) -> dict:
        return {"type": "group", "header": header, "content": {
            "type": "itemgrid", "item_margin": "2,2", "item_size": 40, "rows": rows}}

    items_panel = {"type": "array", "orientation": "vertical", "margin": "4,4", "content": [
        group("Items", item_rows(equip)), group("Songs", item_rows(songs, 6)),
        group("Stones & Medallions", item_rows(rewards, 9)), group("Dungeon keys", item_rows(keys, 8))]}
    settings_panel = {"type": "array", "orientation": "vertical", "margin": "4,4", "content": [
        group("Settings (click to change)", item_rows([SETTING_PREFIX + s[0] for s in settings], 6))]}
    left = {"type": "tabbed", "dock": "left", "tabs": [{"title": "Items", "content": items_panel},
                                                       {"title": "Settings", "content": settings_panel}]}
    return {
        "tracker_default": {"type": "container", "background": "#141a24", "content": {
            "type": "dock", "dropshadow": True, "content": [left, {"type": "map", "maps": [MAP_NAME]}]}},
        "tracker_broadcast": {"type": "container", "background": "#141a24", "content": items_panel},
    }


# ----------------------------------------------------------------------------------------------- map
def place_regions(gw: GenericWorld, layout: dict) -> dict[str, tuple[int, int]]:
    cells = {name: tuple(pos) for name, pos in layout["regions"].items() if name in gw.dungeon}
    skip = set(layout.get("skip", []))
    missing = [r for r in gw.dungeon if r not in cells and r not in skip]
    if missing:
        cols = max(c for c, _ in cells.values()) + 1 if cells else 8
        row = max(r for _, r in cells.values()) + 1 if cells else 0
        for i, name in enumerate(missing):
            cells[name] = (i % cols, row + i // cols)
            print(f"warning: region « {name} » missing from tracker_map.json, put at {cells[name]}",
                  file=sys.stderr)
    return cells  # type: ignore[return-value]


BOX_W, BOX_H, MARGIN, PIN_SIZE = 150, 84, 20, 24


def draw_map(gw: GenericWorld, cells: dict[str, tuple[int, int]], cell: tuple[int, int],
             counts: Counter) -> tuple[bytes, dict[str, tuple[int, int]]]:
    cw, ch = cell
    cols = max(c for c, _ in cells.values()) + 1
    rows = max(r for _, r in cells.values()) + 1
    canvas = Canvas(MARGIN * 2 + cols * cw, MARGIN * 2 + rows * ch, "#1b2230")

    def center(name: str) -> tuple[int, int]:
        c, r = cells[name]
        return MARGIN + c * cw + cw // 2, MARGIN + r * ch + ch // 2

    drawn: set[frozenset] = set()
    for a, b, _ in gw.exits:
        if a in cells and b in cells and a != b and frozenset((a, b)) not in drawn:
            drawn.add(frozenset((a, b)))
            canvas.line(*center(a), *center(b), "#4a5670")
    pins = {}
    for name in cells:
        x, y = center(name)
        fill, border = ("#4a2c3c", "#d58aa6") if gw.dungeon[name] else ("#2c3a54", "#8aa6d5")
        if not counts.get(name):
            fill, border = ("#262b36", "#5a6274")
        x0, y0 = x - BOX_W // 2, y - BOX_H // 2
        canvas.box(x0, y0, x0 + BOX_W, y0 + BOX_H, fill, border)
        ty = y0 + 6
        for line in wrap(name.upper(), 12)[:3]:
            canvas.text(x - canvas.text_width(line) // 2, ty, line, "#eef2f8")
            ty += 16
        if counts.get(name):
            pins[name] = (x, y0 + BOX_H - PIN_SIZE // 2 - 4)
    return canvas.png(), pins


# ----------------------------------------------------------------------------------------------- pack
def build_pack(gw: GenericWorld) -> dict[str, bytes]:
    """Every file of the pack: {path in the pack: content}."""
    files: dict[str, bytes] = {}
    images: dict[str, bytes] = {}
    items, kinds = build_items(gw, item_maxima(gw), images)
    build_logic_lua(gw, kinds, [])  # first pass: collects the settings named by the rules
    used = set(STRUCTURAL) | gw.rules.used_settings
    sitems, stable, sdefaults = setting_items(used, images)
    logic = build_logic_lua(gw, kinds, stable)

    layout = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    cells = place_regions(gw, layout)
    counts = Counter(loc.region for loc in gw.locations)
    map_png, pins = draw_map(gw, cells, tuple(layout["cell"]), counts)
    images["images/map.png"] = map_png

    by_region: dict[str, list[int]] = {}
    for i, loc in enumerate(gw.locations, 1):
        by_region.setdefault(loc.region, []).append(i)
    locations = []
    for region, idx in by_region.items():
        x, y = pins.get(region, (MARGIN, MARGIN))
        locations.append({
            "name": region,
            "map_locations": [{"map": MAP_NAME, "x": x, "y": y}],
            "visibility_rules": [f"$ootrr_region_vis|{idx[0]}|{idx[-1]}"],
            "sections": [{"name": gw.locations[i - 1].name, "item_count": 1,
                          "access_rules": [f"$ootrr_loc|{i}"], "visibility_rules": [f"$ootrr_vis|{i}"]}
                         for i in idx],
        })

    init = ['Tracker:AddItems("items/items.json")',
            'Tracker:AddItems("items/settings.json")',
            'ScriptHost:LoadScript("scripts/logic.lua")',
            'Tracker:AddMaps("maps/maps.json")',
            'Tracker:AddLocations("locations/locations.json")',
            'Tracker:AddLayouts("layouts/tracker.json")',
            "",
            "-- default settings (a saved state loaded later replaces them)",
            "for _, s in ipairs(SETTING_ITEMS) do",
            '  local o = Tracker:FindObjectForCode("setting_" .. s[1])',
            "  local d = SETTING_DEFAULTS[s[1]]",
            "  if o then",
            '    if s[2] == "bool" then o.Active = d',
            '    elseif s[2] == "int" then o.AcquiredCount = d',
            "    else",
            "      for i, v in ipairs(s[3]) do if v == d then o.CurrentStage = i - 1 end end",
            "    end",
            "  end",
            "end"]

    def js(obj) -> bytes:
        return (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8")

    files["manifest.json"] = js({
        "name": "OoTRR Map Tracker", "game_name": "OoTRR (Ocarina of Time, Switch 2 remake)",
        "package_uid": "ootrr_map_tracker", "package_version": __version__, "platform": "switch",
        "author": "OoTRR (generated)", "min_poptracker_version": "0.25.0",
        "variants": {"map": {"display_name": "Map tracker", "flags": []}},
    })
    files["items/items.json"] = js(items)
    files["items/settings.json"] = js(sitems)
    files["maps/maps.json"] = js([{"name": MAP_NAME, "location_size": PIN_SIZE, "location_border_thickness": 2,
                                   "img": "images/map.png"}])
    files["locations/locations.json"] = js(locations)
    files["layouts/tracker.json"] = js(build_layouts(kinds, stable))
    files["scripts/logic.lua"] = logic.encode("utf-8")
    files["scripts/init.lua"] = ("\n".join(init) + "\n").encode("utf-8")
    files.update(images)
    return files


def packs_dir() -> Path:
    return Path(os.path.expanduser("~")) / "Documents" / "PopTracker" / "packs"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PopTracker pack (map tracker) for OoTRR, for every seed and settings")
    p.add_argument("--out", default="output", help="output folder (default: output)")
    p.add_argument("--install", action="store_true", help=f"copy the zip into {packs_dir()}")
    p.add_argument("--ootrr", metavar="PATH", help="OOTRR checkout to read (default: $OOTRR_PATH, then ../OOTRR)")
    p.add_argument("--data", help=argparse.SUPPRESS)  # other data folder (tests)
    args = p.parse_args(argv)

    gw = GenericWorld(Path(args.data) if args.data else DATA_DIR)
    files = build_pack(gw)
    out = Path(args.out)
    folder = out / PACK_NAME
    if folder.exists():
        shutil.rmtree(folder)
    for name, data in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    zpath = out / f"{PACK_NAME}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files.items()):
            z.writestr(name, data)
    print(f"{len(gw.locations)} locations, pack written to {folder} and {zpath}")
    if args.install:
        dest = packs_dir()
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(zpath, dest / zpath.name)
        print(f"installed: {dest / zpath.name} (restart PopTracker or use Reload)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
