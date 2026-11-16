"""Studio-prop census of the model pack converter (tools/modelpack.py map_prop_models, T3 lane PROP).

Run: python3 -I companion/test_modelpack_props.py        (no game files needed: the entity lumps are synthetic text)
Covers: the Xen flora fixed models (CXenSpore pModelNames by skin, CXenHair), Opposing Force item_generic (the map `model` key)
and op4mortar (models/mortar.mdl), cycler / monster_generic ordering (map count, then name; generic actors after the rest),
the name sanitiser (no path escape, no sub-directories) and the gearbox PACK_REV bump that makes the companion rebuild the
gearbox pack once.
"""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tools"))
import modelpack          # noqa: E402


def ent(cls, **kv):
    body = '"classname" "%s"\n' % cls
    for k, v in kv.items():
        body += '"%s" "%s"\n' % (k, v)
    return "{\n" + body + "}\n"


class PropCensus(unittest.TestCase):
    def census(self, maps):
        saved = modelpack.map_entity_lumps
        modelpack.map_entity_lumps = lambda root, mod: iter(maps)
        try:
            return modelpack.map_prop_models("unused", "gearbox")
        finally:
            modelpack.map_entity_lumps = saved

    def test_fixed_classes(self):
        out = self.census([ent("xen_spore_small") + ent("xen_spore_medium") + ent("xen_spore_large") + ent("xen_hair")
                           + ent("op4mortar") + ent("cycler_prdroid")])
        self.assertEqual(sorted(out), sorted(["fungus(small)", "fungus", "fungus(large)", "hair", "mortar", "prdroid"]))

    def test_item_generic_model_key(self):
        maps = [ent("item_generic", model="models/plant_palm.mdl", sequencename="idle", body="2")
                + ent("item_generic", model="models/Plant_Palm.MDL"),
                ent("item_generic", model="models/egg.mdl", sequencename="null"),
                ent("item_generic", model="models/plant_palm.mdl")]
        out = self.census(maps)
        self.assertEqual(out, ["plant_palm", "egg"])          # counted once per map: palm on 2 maps, egg on 1

    def test_item_generic_bad_names_dropped(self):
        maps = [ent("item_generic", model="models/../hl.mdl") + ent("item_generic", model="models/sub/x.mdl")
                + ent("item_generic", model="sprites/x.spr") + ent("item_generic") + ent("item_generic", model="models/ok_1.mdl")]
        self.assertEqual(self.census(maps), ["ok_1"])

    def test_generic_actors_after_props(self):
        maps = [ent("monster_generic", model="models/mini_osprey.mdl") + ent("monster_generic", model="models/egg.mdl"),
                ent("monster_generic", model="models/mini_osprey.mdl"), ent("item_generic", model="models/egg.mdl")]
        out = self.census(maps)
        self.assertEqual(out, ["egg", "mini_osprey"])        # a prop class model never drops behind a generic actor

    def test_mortar_only_from_its_class(self):
        self.assertEqual(modelpack.PROP_FIXED["op4mortar"], "models/mortar.mdl")
        self.assertIn("item_generic", modelpack.PROP_MODEL_KEY)
        self.assertGreaterEqual(modelpack.PROFILES["gearbox"].get("prop_max", modelpack.PROP_MAX_MODELS), 28)   # gearbox maps name 28 non-monster prop models
        self.assertEqual(modelpack.PROFILES["bshift"].get("prop_max", modelpack.PROP_MAX_MODELS), 24)    # other packs unchanged

    def test_gearbox_rev_bumped(self):
        self.assertGreaterEqual(modelpack.PACK_REV["gearbox"], 21)
        self.assertTrue(modelpack.version_for_mod("gearbox").endswith("-gearbox-r%d" % modelpack.PACK_REV["gearbox"]))


if __name__ == "__main__":
    unittest.main()
