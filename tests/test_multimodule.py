from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from build_knowledge_base import _discover_module_dirs, _module_paths
from gerar_skills import inferir_id_modulo, normalizar_url_modulo
from oracle_knowledge.common import write_json
from oracle_knowledge.linker.knowledge_linker import build_graph
from oracle_knowledge.search.hybrid_search import HybridSearch


class ModuleCliHelpersTest(unittest.TestCase):
    def test_normalizes_index_url_and_infers_id(self):
        url = (
            "https://docs.oracle.com/en/cloud/saas/"
            "project-management/26b/oedpp/index.html"
        )
        self.assertEqual(
            normalizar_url_modulo(url),
            "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/",
        )
        self.assertEqual(inferir_id_modulo(url), "project_management_oedpp")

    def test_discovers_separate_module_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ppm = root / "ppm"
            common = root / "common"
            write_json(_module_paths(ppm)["metadata"], {"module_id": "ppm"})
            write_json(_module_paths(common)["physical"], {"skills_catalog": []})
            discovered = _discover_module_dirs([], str(root))
            self.assertEqual(discovered, sorted([ppm.resolve(), common.resolve()]))


class MultiModuleGraphTest(unittest.TestCase):
    def _manifest(self, module_id, table_name, relationships=None):
        return {
            "metadata": {
                "module_id": module_id,
                "module_name": module_id.upper(),
                "release_version": "26B",
            },
            "skills_catalog": [
                {
                    "module_id": module_id,
                    "module_name": module_id.upper(),
                    "sub_module": module_id.upper(),
                    "components": [
                        {
                            "table_name": table_name,
                            "description": f"Tabela {table_name}",
                            "primary_key": [f"{table_name}_ID"],
                            "fields_to_extract": [f"{table_name}_ID"],
                            "columns": [
                                {
                                    "name": f"{table_name}_ID",
                                    "datatype": "NUMBER",
                                    "nullable": False,
                                }
                            ],
                            "relationships": relationships
                            or {"outgoing": [], "incoming": []},
                            "source_url": f"https://example.test/{module_id}/{table_name}.html",
                        }
                    ],
                }
            ],
        }

    def test_resolves_fk_across_manifests_loaded_in_different_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ppm_path = root / "ppm.json"
            common_path = root / "common.json"
            write_json(
                ppm_path,
                self._manifest(
                    "ppm",
                    "PJF_PROJECTS_ALL_B",
                    {
                        "outgoing": [
                            {
                                "source_table": "PJF_PROJECTS_ALL_B",
                                "source_column": "BUSINESS_UNIT_ID",
                                "target_table": "FUN_ALL_BUSINESS_UNITS_V",
                                "target_column": None,
                            }
                        ],
                        "incoming": [],
                    },
                ),
            )
            write_json(
                common_path,
                self._manifest("common", "FUN_ALL_BUSINESS_UNITS_V"),
            )

            graph = build_graph(physical_manifest=[str(ppm_path), str(common_path)])
            foreign_keys = [
                edge for edge in graph["edges"] if edge["type"] == "foreign_key_to"
            ]
            self.assertEqual(len(foreign_keys), 1)
            self.assertEqual(graph["stats"]["source_files"], 2)
            self.assertIn("ppm", graph["stats"]["nodes_by_module"])
            self.assertIn("common", graph["stats"]["nodes_by_module"])

            search = HybridSearch(graph)
            results = search.search("PJF_PROJECTS_ALL_B FUN_ALL_BUSINESS_UNITS_V", limit=10)
            modules = {module for row in results for module in row.get("modules", [])}
            self.assertIn("ppm", modules)
            self.assertIn("common", modules)

            common_only = search.search(
                "FUN_ALL_BUSINESS_UNITS_V",
                limit=10,
                module_ids={"common"},
            )
            self.assertTrue(common_only)
            self.assertTrue(
                all("common" in row.get("modules", []) for row in common_only)
            )


if __name__ == "__main__":
    unittest.main()
