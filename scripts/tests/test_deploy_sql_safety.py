import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class DeploySqlSafetyTests(unittest.TestCase):
    def test_backup_passes_dynamic_sql_as_a_positional_argument(self) -> None:
        source = (REPO_ROOT / "deploy" / "backup.sh").read_text(encoding="utf-8")

        self.assertIn("sh \"${sql}\"", source)
        self.assertNotIn('-e \\"${sql}\\"', source)

    def test_restore_passes_database_and_sql_as_positional_arguments(self) -> None:
        source = (REPO_ROOT / "deploy" / "restore.sh").read_text(encoding="utf-8")

        self.assertIn('sh "${database}" "${statement}"', source)
        self.assertNotIn('${database} -e \\"${statement}\\"', source)


if __name__ == "__main__":
    unittest.main()
