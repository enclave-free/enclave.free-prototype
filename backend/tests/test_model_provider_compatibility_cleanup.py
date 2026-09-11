import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class ModelProviderCompatibilityCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_env = {
            key: os.environ.get(key)
            for key in (
                "SQLITE_PATH",
                "SECRET_KEY",
                "LLM_PROVIDER",
                "LLM_API_URL",
                "LLM_MODEL",
                "LLM_API_KEY",
                "TINFOIL_MODEL_FALLBACKS",
                "MAPLE_BASE_URL",
                "MAPLE_MODEL",
                "MAPLE_API_KEY",
            )
        }
        for key in self._orig_env:
            os.environ.pop(key, None)
        os.environ["SQLITE_PATH"] = str(Path(self.tmp.name) / "enclave.db")
        os.environ["SECRET_KEY"] = "test-secret"

        import database
        import config_loader

        self.database = importlib.reload(database)
        self.config_loader = importlib.reload(config_loader)
        self.database.init_schema()

    def tearDown(self) -> None:
        if self.database._connection is not None:
            self.database._connection.close()
            self.database._connection = None
        self.database._deployment_secret_key = None
        for key, value in self._orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_maple_environment_aliases_do_not_configure_current_model_provider(self) -> None:
        os.environ["MAPLE_BASE_URL"] = "https://maple.example.test/v1"
        os.environ["MAPLE_MODEL"] = "maple-model"
        os.environ["MAPLE_API_KEY"] = "maple-key"

        self.config_loader.invalidate_cache()

        self.assertIsNone(self.config_loader.get_config("LLM_API_URL"))
        self.assertIsNone(self.config_loader.get_config("LLM_MODEL"))
        self.assertIsNone(self.config_loader.get_config("LLM_API_KEY"))

    def test_llm_api_key_environment_configures_python_provider_auth(self) -> None:
        os.environ["LLM_API_KEY"] = "env-only-key"

        self.config_loader.invalidate_cache()

        self.assertEqual(self.config_loader.get_config("LLM_API_KEY"), "env-only-key")
        self.assertEqual(self.config_loader.get_llm_config()["api_key"], "env-only-key")

    def test_provider_uses_env_only_llm_api_key(self) -> None:
        os.environ["LLM_API_KEY"] = "env-only-key"

        from llm.sage_tinfoil import SageTinfoilProvider

        provider = SageTinfoilProvider()

        self.assertEqual(provider.api_key, "env-only-key")

    def test_provider_defaults_to_glm_without_a_fallback_model_contract(self) -> None:
        os.environ["TINFOIL_MODEL_FALLBACKS"] = "gpt-oss-120b"

        from llm.sage_tinfoil import SageTinfoilProvider

        provider = SageTinfoilProvider()

        self.assertEqual(provider.default_model, "glm-5-3-flash")
        self.assertFalse(hasattr(provider, "fallback_models"))

    def test_maple_provider_label_is_not_coerced_to_sage(self) -> None:
        os.environ["LLM_PROVIDER"] = "maple"

        self.config_loader.invalidate_cache()

        with self.assertRaises(ValueError) as ctx:
            self.config_loader.get_llm_config()

        self.assertEqual(str(ctx.exception), 'Unsupported LLM_PROVIDER "maple"; only "sage" is supported')

    def test_model_provider_factory_rejects_maple_label(self) -> None:
        import llm

        with self.assertRaises(ValueError) as ctx:
            llm.get_provider("maple")

        self.assertEqual(str(ctx.exception), 'Unsupported Model Provider "maple"; only "sage" is supported')

    def test_model_provider_compatibility_aliases_are_not_exported(self) -> None:
        import llm

        self.assertFalse(hasattr(llm, "ModelProvider"))
        self.assertFalse(hasattr(llm, "ModelProviderResponse"))


if __name__ == "__main__":
    unittest.main()
