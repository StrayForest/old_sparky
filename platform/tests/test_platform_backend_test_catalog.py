from __future__ import annotations

import unittest

from tools import platform_test_catalog as catalog


class PlatformBackendTestCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = catalog.discover_test_cases()

    def test_catalog_is_snapshot_complete_and_unique(self) -> None:
        self.assertEqual(catalog.catalog_issues(self.cases), [])
        self.assertEqual(catalog.intentional_skip_issues(self.cases), [])
        ids = [case.test_id for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        snapshot = catalog.registry_payload()["snapshot"]
        for field, expected in catalog.EXPECTED_SNAPSHOT.items():
            if field in snapshot:
                self.assertEqual(snapshot[field], expected)

    def test_declared_module_owners_are_disjoint(self) -> None:
        declared = (
            set(catalog.EXTERNAL_MODULE_OWNERS),
            set(catalog.PERFORMANCE_MODULES),
            set(catalog.PRIVILEGED_MODULES),
            set(catalog.TOOL_MODULES),
            set(catalog.UNIT_MODULES),
        )
        self.assertEqual(sum(map(len, declared)), len(set().union(*declared)))

    def test_backend_aggregate_is_the_disjoint_union_of_contours(self) -> None:
        backend_ids = {case.test_id for case in catalog.backend_cases(self.cases)}
        contour_ids = [
            {case.test_id for case in catalog.cases_for_contour(contour, self.cases)}
            for contour in catalog.BACKEND_CONTOURS
        ]
        self.assertEqual(backend_ids, set().union(*contour_ids))
        for index, current in enumerate(contour_ids):
            for other in contour_ids[index + 1 :]:
                self.assertTrue(current.isdisjoint(other))

    def test_external_owners_are_explicit_and_excluded_from_backend(self) -> None:
        external_ids = {
            case.test_id
            for case in self.cases
            if case.module in catalog.EXTERNAL_MODULE_OWNERS
        }
        all_ids = {case.test_id for case in self.cases}
        backend_ids = {case.test_id for case in catalog.backend_cases(self.cases)}
        self.assertTrue(external_ids)
        self.assertTrue(external_ids.isdisjoint(backend_ids))
        self.assertEqual(all_ids, backend_ids | external_ids)
        self.assertEqual(
            {
                case.contour
                for case in self.cases
                if case.module in catalog.EXTERNAL_MODULE_OWNERS
            },
            set(catalog.EXTERNAL_MODULE_OWNERS.values()),
        )

    def test_each_contour_has_machine_readable_owner_and_timeout(self) -> None:
        payload = catalog.registry_payload()
        contours = {item["id"] for item in payload["contours"]}
        self.assertEqual(contours, set(catalog.BACKEND_CONTOURS))
        for item in payload["contours"]:
            self.assertTrue(item["runner"])
            self.assertIn(item["timeout_class"], {"short", "long"})
            self.assertGreater(item["timeout_seconds"], 0)

    def test_focus_selector_can_be_resolved_without_importing_the_database(self) -> None:
        selected = catalog.cases_for_contour(
            "backend-unit",
            self.cases,
        )
        self.assertTrue(selected)
        self.assertTrue(all(case.contour == "backend-unit" for case in selected))

    def test_catalog_matches_unittest_discovery_exactly(self) -> None:
        def flatten(suite: unittest.TestSuite):
            for item in suite:
                if isinstance(item, unittest.TestSuite):
                    yield from flatten(item)
                else:
                    yield item

        discovered_ids = {
            f"tests.{test.id()}"
            for test in flatten(unittest.TestLoader().discover("tests"))
        }
        self.assertEqual(
            discovered_ids,
            {case.test_id for case in self.cases},
        )


if __name__ == "__main__":
    unittest.main()
