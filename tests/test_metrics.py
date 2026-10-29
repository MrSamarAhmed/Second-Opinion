import unittest

from citytaste.metrics import Tile, compare, load_tiles, top_n


def tiles(ranks: dict) -> dict:
    return {g: Tile(g, float(r), 0.0, 0.0) for g, r in ranks.items()}


def payload(rows):
    return {"results": {"heatmap": rows}}


def row(gh, rank, lat=53.0, lon=-2.0):
    return {"location": {"geohash": gh, "latitude": lat, "longitude": lon},
            "query": {"affinity_rank": rank}}


class TestLoading(unittest.TestCase):
    def test_duplicate_identifier_raises(self):
        with self.assertRaises(ValueError):
            load_tiles(payload([row("a", 0.1), row("a", 0.2)]))

    def test_missing_rank_dropped_and_invalid_identifier_dropped(self):
        data = payload([row("a", 0.1), {"location": {"geohash": "b"}, "query": {}},
                        {"location": {}, "query": {"affinity_rank": 0.5}}, row("c", 0.3)])
        self.assertEqual(set(load_tiles(data)), {"a", "c"})

    def test_non_numeric_rank_dropped(self):
        data = payload([row("a", "0.9"), row("b", 0.2)])
        self.assertEqual(set(load_tiles(data)), {"b"})


class TestSelection(unittest.TestCase):
    def test_top_n_is_rank_ordered(self):
        t = tiles({"a": 0.1, "b": 0.9, "c": 0.5})
        self.assertEqual(top_n(t, 2), {"b", "c"})

    def test_top_n_ties_broken_by_geohash(self):
        t = tiles({"b": 0.5, "a": 0.5, "c": 0.1})
        self.assertEqual(top_n(t, 1), {"a"})

    def test_top_n_rejects_zero(self):
        with self.assertRaises(ValueError):
            top_n(tiles({"a": 0.5}), 0)


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.U = {f"g{i}" for i in range(10)}

    def test_identical_sets(self):
        a = tiles({g: 1 - i / 10 for i, g in enumerate(sorted(self.U))})
        b = dict(a)
        c = compare(a, b, 3, "global", self.U)
        self.assertEqual(c.overlap_coefficient, 1.0)
        self.assertAlmostEqual(c.expected_overlap, 3 / 10)
        self.assertAlmostEqual(c.lift, 10 / 3)

    def test_disjoint_sets(self):
        a = tiles({f"a{i}": 1 - i / 10 for i in range(4)})
        b = tiles({f"b{i}": 1 - i / 10 for i in range(4)})
        c = compare(a, b, 2, "global", set(a) | set(b))
        self.assertEqual(c.overlap_coefficient, 0.0)
        self.assertEqual(c.expected_overlap, 0.0)
        self.assertIsNone(c.lift)

    def test_partial_overlap(self):
        a = tiles({"x": 1.0, "y": 0.9, "z": 0.8})
        b = tiles({"x": 1.0, "q": 0.9, "r": 0.8})
        c = compare(a, b, 2, "global", set(a) | set(b))
        self.assertEqual(c.shared_selected, 1)
        self.assertEqual(c.overlap_coefficient, 0.5)

    def test_different_map_sizes_change_expectation(self):
        a = tiles({f"g{i}": 1 - i / 100 for i in range(20)})
        b = {g: Tile(g, 1.0, 0, 0) for g in list(a)[:5]}
        c = compare(a, b, 3, "global", set(a) | set(b))
        # |U_a & U_b| = 5, |U_a| = 20, |U_b| = 5 -> E_O = 5*3/(20*5) = 0.15
        self.assertAlmostEqual(c.expected_overlap, 0.15)

    def test_pair_method_differs_from_global(self):
        a = tiles({"x": 1.0, "y": 0.9, "z": 0.8, "w": 0.1})
        b = tiles({"x": 1.0, "z": 0.9, "q": 0.8})
        g = compare(a, b, 2, "global", set(a) | set(b))
        p = compare(a, b, 2, "pair", set(a) | set(b))
        self.assertEqual(g.overlap_coefficient, 0.5)   # {x,y} vs {x,z}
        self.assertEqual(p.overlap_coefficient, 1.0)   # restricted to {x,z}: both pick {x,z}
        self.assertNotEqual(g.expected_overlap, p.expected_overlap)

    def test_pair_method_excluded_when_shared_below_n(self):
        a = tiles({"x": 1.0, "y": 0.9})
        b = tiles({"x": 1.0, "q": 0.9})
        c = compare(a, b, 3, "pair", set(a) | set(b))
        self.assertTrue(c.excluded)
        self.assertEqual(c.overlap_coefficient, 0.0)

    def test_every_cutoff_runs(self):
        a = tiles({f"g{i}": 1 - i / 50 for i in range(50)})
        b = tiles({f"g{i}": (i % 7) / 10 for i in range(50)})
        for n in (10, 20, 30):
            for method in ("global", "pair"):
                c = compare(a, b, n, method, set(a) | set(b))
                self.assertEqual(c.n, n)
                self.assertGreaterEqual(c.overlap_coefficient, 0.0)
                self.assertLessEqual(c.overlap_coefficient, 1.0)


if __name__ == "__main__":
    unittest.main()
