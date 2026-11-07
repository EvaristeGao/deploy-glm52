"""Exercise real filesystem replacement before deploying refactored modules."""

import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "image_source", Path(__file__).resolve().parents[1] / "ansible/dsa-layerwise/install_source.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class TestImageSource(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "incoming"
        self.package = root / "installed/vllm_ascend"
        self.examples = root / "installed/examples/disaggregated_prefill_v1"
        self.put(self.package / "__init__.py", "old = True")
        self.put(self.package / "obsolete.py", "old = True")
        self.put(self.package / "old_package/__init__.py", "old = True")
        self.put(self.package / "__pycache__/obsolete.pyc", "stale bytecode")
        for name in installer.SDK_METADATA:
            self.put(self.package / name, "pinned metadata")
        for name in installer.SDK_DIRECTORIES:
            self.put(self.package / name / "native.data", "pinned native data")
        self.put(self.package / "vllm_ascend_C.fake.so", "pinned extension")
        self.put(self.package / "libvllm_ascend_kernels.so", "pinned library")
        (self.package / "lib64/library.so").symlink_to("../libvllm_ascend_kernels.so")
        self.put(self.package / "_cann_ops_custom/operator.py", "pinned operator source")
        self.put(self.examples / "obsolete_proxy.py", "old proxy")
        self.put(self.source / "vllm_ascend/__init__.py", "new = True")
        self.put(self.source / "vllm_ascend/new_module.py", "new = True")
        self.put(self.source / "vllm_ascend/_cann_ops_custom/.gitkeep", "tracked placeholder")
        self.put(self.source / "examples/disaggregated_prefill_v1/new_proxy.py", "new proxy")

    @staticmethod
    def put(path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    def test_replacement_removes_deleted_modules_and_keeps_complete_native_kit(self):
        before = installer.native_hashes(self.package, installer.sdk_names(self.package))
        result = installer.install_source(self.source, self.package, self.examples)
        self.assertEqual(result, before)
        self.assertEqual(installer.native_hashes(self.package, installer.sdk_names(self.package)), before)
        self.assertTrue((self.package / "lib64/library.so").is_symlink())
        self.assertIsNone(importlib.machinery.PathFinder.find_spec("obsolete", [str(self.package)]))
        self.assertIsNone(importlib.machinery.PathFinder.find_spec("old_package", [str(self.package)]))
        self.assertFalse((self.package / "__pycache__").exists())
        self.assertFalse((self.examples / "obsolete_proxy.py").exists())
        self.assertTrue((self.examples / "new_proxy.py").is_file())

    def test_second_snapshot_cannot_resurrect_a_module_from_first_build(self):
        installer.install_source(self.source, self.package, self.examples)
        (self.source / "vllm_ascend/new_module.py").unlink()
        installer.install_source(self.source, self.package, self.examples)
        self.assertFalse((self.package / "new_module.py").exists())
        self.assertTrue((self.package / "vllm_ascend_C.fake.so").is_file())

    def test_metadata_collision_fails_before_deleting_any_old_source(self):
        self.put(self.source / "vllm_ascend/_build_info.py", "wrong hardware")
        with self.assertRaisesRegex(ValueError, "pinned native"):
            installer.install_source(self.source, self.package, self.examples)
        self.assertTrue((self.package / "obsolete.py").is_file())
        self.assertEqual((self.package / "_build_info.py").read_text(), "pinned metadata")

    def test_unclassified_nested_native_library_fails_before_deletion(self):
        self.put(self.package / "ops/unclassified.so", "must not lose this")
        with self.assertRaisesRegex(ValueError, "Unclassified native"):
            installer.install_source(self.source, self.package, self.examples)
        self.assertTrue((self.package / "obsolete.py").is_file())
        self.assertTrue((self.package / "ops/unclassified.so").is_file())

    def test_native_operator_source_cannot_be_overridden_by_the_overlay(self):
        self.put(self.source / "vllm_ascend/_cann_ops_custom/operator.py", "changed operator")
        with self.assertRaisesRegex(ValueError, "pinned native"):
            installer.install_source(self.source, self.package, self.examples)
        self.assertEqual((self.package / "_cann_ops_custom/operator.py").read_text(), "pinned operator source")

    def test_incomplete_sdk_fails_closed(self):
        (self.package / "_build_info.py").unlink()
        with self.assertRaisesRegex(ValueError, "complete pinned"):
            installer.install_source(self.source, self.package, self.examples)
        self.assertTrue((self.package / "obsolete.py").exists())


if __name__ == "__main__":
    unittest.main()
