import ast
import pathlib
import sys
import unittest

class ConstraintTests(unittest.TestCase):
    def test_imports_are_stdlib_or_local(self):
        root = pathlib.Path(__file__).parents[1]
        allowed_local = {"rebate_rail", "demo", "app"}
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import): names = [x.name.split('.')[0] for x in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0: names = [(node.module or '').split('.')[0]]
                else: continue
                for name in names:
                    self.assertTrue(name in sys.stdlib_module_names or name in allowed_local,
                                    f"non-stdlib import {name} in {path}")

    def test_line_budget(self):
        root = pathlib.Path(__file__).parents[1]
        new_paths = {"app.py", "rebate_rail/parse.py", "rebate_rail/workspace.py", "rebate_rail/plan.py",
                     "tests/__init__.py", "tests/test_app.py", "tests/test_parse.py", "tests/test_plan.py", "tests/test_review_fixes.py"}
        counts = {path.relative_to(root).as_posix(): len(path.read_text(encoding="utf-8").splitlines()) for path in root.rglob("*.py")}
        # New usable-app files have their own 3,200-line allowance; existing files,
        # including this test, retain the original 1,600-line allowance.
        self.assertLessEqual(sum(count for path, count in counts.items() if path in new_paths), 3200)
        self.assertLessEqual(sum(count for path, count in counts.items() if path not in new_paths), 1600)

if __name__ == "__main__": unittest.main()
