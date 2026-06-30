from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from build_knowledge_base import collect_adf


class CollectAdfCredentialsTest(unittest.TestCase):
    def test_requires_password_environment_variable_without_prompt(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            args = SimpleNamespace(
                module_dir=None,
                output_dir=str(Path(temporary_dir) / "adf"),
                username="integrador.bi",
                username_env="FUSION_USERNAME",
                password_env="CUSTOM_FUSION_PASSWORD",
                bearer_token_env="FUSION_BEARER_TOKEN",
                anonymous=False,
            )

            with patch.dict(
                "os.environ",
                {
                    "FUSION_USERNAME": "",
                    "CUSTOM_FUSION_PASSWORD": "",
                    "FUSION_BEARER_TOKEN": "",
                },
                clear=False,
            ):
                with self.assertRaises(SystemExit) as raised:
                    collect_adf(args)

        self.assertEqual(
            str(raised.exception),
            "Senha ausente. Defina a variável de ambiente "
            "CUSTOM_FUSION_PASSWORD.",
        )


if __name__ == "__main__":
    unittest.main()
