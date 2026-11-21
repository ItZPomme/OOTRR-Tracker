# OOTRR-Tracker

A [PopTracker](https://github.com/black-sliver/PopTracker) map tracker for
[OoTRR](https://github.com/ItZPomme/OOTRR), the randomizer for *The Legend of Zelda: Ocarina of Time* (Switch 2
remake).

The pack is generated from the randomizer's own data and logic, so it follows the randomizer exactly:

- the rules of `ootrr/data/**.json` are translated to Lua, with the same child / adult region search as the
  generator (settings, dungeon keys and spawn exits included);
- a schematic map of Hyrule is drawn by the generator (a box per region, lines for the exits, a pin per region);
  region positions are in [`ootrr_tracker/tracker_map.json`](ootrr_tracker/tracker_map.json);
- icons are drawn too (5x7 pixel font). **No game file is used.**

One pack works for every seed and every settings: the settings are items of the tracker.

## Requirements

- Python 3.10+
- an OOTRR checkout (the generator imports its `ootrr` package), and its requirements
  (`python -m pip install -r requirements.txt` in OOTRR)
- optional, for the Lua logic test: `python -m pip install lupa`

Where OOTRR is looked up, in this order:

1. `--ootrr PATH` on the command line;
2. the `OOTRR_PATH` environment variable;
3. an `OOTRR` folder next to this repository;
4. an `ootrr` package that is already importable (e.g. `pip install -e` of OOTRR).

The simplest setup is to clone both repositories side by side:

```bash
git clone https://github.com/ItZPomme/OOTRR
git clone https://github.com/ItZPomme/OOTRR-Tracker
cd OOTRR-Tracker
```

## Usage

```bash
python -m ootrr_tracker                     # output/OoTRR-tracker/ and output/OoTRR-tracker.zip
python -m ootrr_tracker --install           # also copies the zip into Documents/PopTracker/packs
python -m ootrr_tracker --ootrr ../OOTRR    # read another OOTRR checkout
python -m ootrr_tracker --out DIR           # other output folder
```

`--install` copies `OoTRR-tracker.zip` into `Documents/PopTracker/packs` (restart PopTracker or use Reload).
Regenerate the pack only when the randomizer's data changes (a new OOTRR version).

## In PopTracker

1. Open the pack "OoTRR Map Tracker".
2. **Settings tab**: set the seed's settings (click a setting to change its value: toggles, choices, counts). The
   logic reads them, and a location only shows on the map when it is a check with those settings (dungeon keys,
   Skulltulas, shops, scrubs, gifts…). Start locations are hidden.
3. Click your items as you find them. Pins turn green when a check is in logic.

The settings are saved with PopTracker's state, like the items.

Known difference with the generator: the child is assumed to use every marked item. The generator keeps items
found as adult away from the child until he can travel back in time, which only matters with `free_time_travel`
off.

## Tests

```bash
python -m unittest discover -s tests
```

- pack consistency: every image is in the pack, every item is in the layout, every location of a world with every
  check on has a section;
- with `lupa` installed: on random settings, the Lua logic shows the same checks as `World` and finds the same ones
  reachable as the generator's `search.State`.

## Layout

| Path | Role |
|---|---|
| `ootrr_tracker/poptracker.py` | Pack generator: items, settings, Lua logic, map, layouts |
| `ootrr_tracker/tracker_map.json` | Map layout: grid cell size and a position per region |
| `ootrr_tracker/__init__.py` | Finds the OOTRR checkout and puts it on `sys.path` |
| `tests/test_poptracker.py` | Tests |

A region missing from `tracker_map.json` is still placed (a warning names it); add it to the layout to choose its
place.
