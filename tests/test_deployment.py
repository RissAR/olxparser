"""
Unit and Integration Tests for Deployment and Packaging Configuration (Step 6).
Tests:
- Dockerfile syntax, base image, environment variables, healthcheck, and entrypoint.
- docker-compose.yml service definition, volume mappings, ports, and restart policy.
- olxparser.service systemd unit configuration.
- setup_systemd.sh and docker-entrypoint.sh existence and executable flags.
- .dockerignore entries.
- Runtime database & exporter support for custom OLX_DB_PATH and OLX_EXPORTS_DIR.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from database import init_db, upsert_residential_offer
from exporter import export_residential, export_all, list_exported_files


class TestDeploymentFiles(unittest.TestCase):
    """Verifies packaging and deployment files for VDS."""

    def setUp(self):
        self.root_dir = Path(__file__).resolve().parent.parent

    def test_dockerfile_contents(self):
        dockerfile = self.root_dir / "Dockerfile"
        self.assertTrue(dockerfile.is_file(), "Dockerfile must exist in project root")

        content = dockerfile.read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim", content)
        self.assertIn("ENV PYTHONUNBUFFERED=1", content)
        self.assertIn("TZ=Europe/Kyiv", content)
        self.assertIn("EXPOSE 8000", content)
        self.assertIn("HEALTHCHECK", content)
        self.assertIn("/api/status", content)
        self.assertIn('"uvicorn"', content)
        self.assertIn('"app:app"', content)

    def test_dockerignore_contents(self):
        dockerignore = self.root_dir / ".dockerignore"
        self.assertTrue(dockerignore.is_file(), ".dockerignore must exist in project root")

        content = dockerignore.read_text(encoding="utf-8")
        self.assertIn(".venv", content)
        self.assertIn("venv/", content)
        self.assertIn(".git", content)
        self.assertIn(".gitignore", content)
        self.assertIn(".agent", content)
        self.assertIn("__pycache__/", content)
        self.assertIn("*.py[cod]", content)
        self.assertIn("*$py.class", content)
        self.assertIn("tests/", content)
        self.assertIn("data/", content)
        self.assertIn("exports/", content)
        self.assertIn("*.db", content)
        self.assertIn("*.db-shm", content)
        self.assertIn("*.db-wal", content)
        self.assertIn("*.zip", content)

    def test_docker_compose_structure(self):
        compose_file = self.root_dir / "docker-compose.yml"
        self.assertTrue(compose_file.is_file(), "docker-compose.yml must exist")

        content = compose_file.read_text(encoding="utf-8")
        self.assertIn("services:", content)
        self.assertIn("olxparser:", content)
        self.assertIn("restart: unless-stopped", content)
        # Port mapping
        self.assertIn("8000", content)
        # Volume mappings for persistence
        self.assertIn("./data:/app/data", content)
        self.assertIn("./exports:/app/exports", content)
        # Environment variables
        self.assertIn("OLX_DB_PATH", content)
        self.assertIn("OLX_EXPORTS_DIR", content)
        self.assertIn("Europe/Kyiv", content)

    def test_systemd_service_unit(self):
        service_file = self.root_dir / "olxparser.service"
        self.assertTrue(service_file.is_file(), "olxparser.service must exist")

        content = service_file.read_text(encoding="utf-8")
        self.assertIn("[Unit]", content)
        self.assertIn("[Service]", content)
        self.assertIn("[Install]", content)
        self.assertIn("Type=simple", content)
        self.assertIn("uvicorn app:app", content)
        self.assertIn("Restart=always", content)
        self.assertIn("Europe/Kyiv", content)
        self.assertIn('Environment="OLX_DB_PATH=', content)
        self.assertIn('Environment="OLX_EXPORTS_DIR=', content)
        self.assertIn("WantedBy=multi-user.target", content)

    def test_scripts_executable(self):
        entrypoint = self.root_dir / "docker-entrypoint.sh"
        self.assertTrue(entrypoint.is_file(), "docker-entrypoint.sh must exist")
        mode = entrypoint.stat().st_mode
        self.assertTrue(bool(mode & stat.S_IXUSR), "docker-entrypoint.sh must be executable")

        setup_script = self.root_dir / "setup_systemd.sh"
        self.assertTrue(setup_script.is_file(), "setup_systemd.sh must exist")
        mode = setup_script.stat().st_mode
        self.assertTrue(bool(mode & stat.S_IXUSR), "setup_systemd.sh must be executable")

        verify_script = self.root_dir / "verify.py"
        self.assertTrue(verify_script.is_file(), "verify.py must exist")
        mode = verify_script.stat().st_mode
        self.assertTrue(bool(mode & stat.S_IXUSR), "verify.py must be executable")

        benchmark_script = self.root_dir / "benchmark.py"
        self.assertTrue(benchmark_script.is_file(), "benchmark.py must exist")
        mode = benchmark_script.stat().st_mode
        self.assertTrue(bool(mode & stat.S_IXUSR), "benchmark.py must be executable")

    def test_custom_paths_environment_variables(self):
        """Verifies that DB and exports honor environment variables."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            custom_db = os.path.join(tmp_dir, "nested_data", "test_olx.db")
            custom_exp = os.path.join(tmp_dir, "nested_exports")

            # Test database initialization in custom nested directory
            init_db(db_path=custom_db)
            self.assertTrue(os.path.exists(custom_db), "Database file should be created in nested folder")

            # Insert sample offer
            sample_offer = {
                "url": "https://www.olx.ua/d/uk/obyavlenie/test-deploy-1.html",
                "seen_date": "2026-11-03",
                "price": 25000.0,
                "currency": "UAH",
                "seller": "owner",
                "street": "вулиця Хрещатик",
                "house": "1",
                "district": "Печерський",
                "rooms": 2,
                "area_total": 75.0,
                "area_kitchen": 15.0,
                "floor": 4,
                "floors_total": 7,
                "building_type": "tsar",
                "renovation": "author",
                "furniture": "full",
                "appliances": "ac,washer,boiler",
                "generator_in_building": "yes",
                "inverter_in_flat": "yes",
                "pets_allowed": "yes",
                "kids_allowed": "yes",
                "commission_pct": 0.0,
                "utilities_included": "no",
            }
            upsert_residential_offer(sample_offer, db_path=custom_db)

            # Test export with OLX_EXPORTS_DIR
            orig_env = os.environ.get("OLX_EXPORTS_DIR")
            try:
                os.environ["OLX_EXPORTS_DIR"] = custom_exp
                res_info = export_residential(db_path=custom_db)
                self.assertTrue(os.path.exists(res_info["path"]))
                self.assertTrue(res_info["path"].startswith(custom_exp))
                self.assertEqual(res_info["rows_count"], 1)

                all_res = export_all(db_path=custom_db)
                self.assertTrue(os.path.exists(all_res["residential"]["path"]))
                self.assertTrue(os.path.exists(all_res["commercial"]["path"]))
            finally:
                if orig_env is None:
                    os.environ.pop("OLX_EXPORTS_DIR", None)
                else:
                    os.environ["OLX_EXPORTS_DIR"] = orig_env


if __name__ == "__main__":
    unittest.main()
