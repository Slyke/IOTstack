import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from deps.compose_environment import loadDotEnv, setDotEnvValue
from deps.service_catalog import ServiceCatalog, ServiceManager, hostTimezone, readServices, writeYaml
from deps.stack_builder import buildStack


class ServiceManagementTests(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.addCleanup(self.directory.cleanup)
    self.root = pathlib.Path(self.directory.name)
    (self.root / ".templates").mkdir()
    writeYaml(self.root / ".templates/docker-compose-base.yml", {"services": {}, "networks": {"default": {}}})
    self.environment = patch.dict(os.environ, {}, clear=True)
    self.environment.start()
    self.addCleanup(self.environment.stop)

  def template(self, name="app", dependencies=(), definitions=None, environment=()):
    directory = self.root / ".templates" / name
    directory.mkdir(exist_ok=True)
    writeYaml(directory / "service.yml", definitions or {name: {"image": "example:test"}})
    manifest = {"service": name, "description": name, "dependencies": list(dependencies),
                "environment": list(environment), "install": [
                  {"template": "service.yml", "service": "service.yml", "tracked": True}
                ]}
    (directory / "menu-config.json").write_text(json.dumps(manifest))
    return directory

  def original(self, services):
    writeYaml(self.root / "docker-compose.yml", {"services": services})
    return (self.root / "docker-compose.yml").read_bytes()

  def test_migration_preserves_unresolved_variables_companions_and_custom_services(self):
    self.template(definitions={"app": {"image": "new"}, "app_db": {"image": "db"}})
    saved = {"app": {"image": "old", "environment": {"SECRET": "${SECRET:?required}"}},
             "app_db": {"image": "db:old"}, "custom": {"image": "custom"}}
    original = self.original(saved)
    manager = ServiceManager(self.root)
    self.assertEqual(["app"], manager.active())
    self.assertEqual(saved, manager.definitions())
    manager.install(["app"])
    restored = ServiceManager(self.root)
    self.assertEqual(saved, restored.definitions())
    self.assertEqual(original, (self.root / "docker-compose.yml").read_bytes())

  def test_migration_preserves_custom_compose_services_when_old_menu_cache_exists(self):
    self.template()
    self.original({"app": {"image": "app"}, "custom": {"image": "custom"}})
    writeYaml(self.root / "services/docker-compose.save.yml", {"services": {"app": {"image": "saved"}}})
    self.assertEqual({"app", "custom"}, set(ServiceManager(self.root).definitions()))

  def test_partial_install_does_not_lose_other_migrated_settings(self):
    self.template("app")
    self.template("other")
    saved = {"app": {"image": "app:old"}, "other": {"image": "other:old"}}
    self.original(saved)
    manager = ServiceManager(self.root)
    manager.install(["app"])
    self.assertEqual(saved, ServiceManager(self.root).definitions())

  def test_invalid_migration_does_not_write_state(self):
    self.template()
    for contents in ("services: [bad]", "services: {app: [", "# empty"):
      (self.root / "docker-compose.yml").write_text(contents)
      with self.assertRaises(Exception):
        ServiceManager(self.root)
      self.assertFalse((self.root / "services/.menu-state.json").exists())
      self.assertEqual(contents, (self.root / "docker-compose.yml").read_text())

  def test_dependency_install_and_deactivation(self):
    self.template("db")
    self.template(dependencies=["db"])
    manager = ServiceManager(self.root)
    self.assertEqual(["db", "app"], manager.install(["app"]))
    self.assertEqual(["app", "db"], manager.active())
    with self.assertRaisesRegex(ValueError, "requires db"):
      manager.deactivate(["db"])
    manager.deactivate(["app", "db"])
    self.assertEqual([], ServiceManager(self.root).active())

  def test_dependency_cycle_is_rejected_before_install(self):
    self.template("db", dependencies=["app"])
    self.template(dependencies=["db"])
    with self.assertRaisesRegex(ValueError, "Circular"):
      ServiceManager(self.root).install(["app"])
    self.assertFalse((self.root / "services").exists())

  def test_python_directory_install_and_repeated_build_preserve_application(self):
    shutil.copytree(ROOT / ".templates/python", self.root / ".templates/python")
    manager = ServiceManager(self.root)
    manager.install(["python"])
    application = self.root / "services/python/app/app.py"
    application.write_text("print('custom application')\n")
    manager.install(["python"], activate=False)
    self.assertEqual([], manager.missing("python"))
    with patch("deps.stack_builder.validateCompose"):
      buildStack(manager)
      buildStack(ServiceManager(self.root))
    self.assertEqual("print('custom application')\n", application.read_text())
    self.assertEqual([], ServiceManager(self.root).updates("python"))

  def test_upgrade_backs_up_unchanged_defaults_and_stages_custom_files(self):
    directory = self.template()
    manager = ServiceManager(self.root)
    manager.install(["app"])
    target = self.root / "services/app/service.yml"
    original = target.read_bytes()
    writeYaml(directory / "service.yml", {"app": {"image": "example:2"}})
    self.assertEqual(["service.yml"], manager.updates("app"))
    manager.upgrade("app")
    self.assertEqual((directory / "service.yml").read_bytes(), target.read_bytes())
    self.assertEqual(original, next(target.parent.glob("*.backup")).read_bytes())
    self.assertEqual([], manager.updates("app"))
    target.write_text("app:\n  image: custom\n")
    writeYaml(directory / "service.yml", {"app": {"image": "example:3"}})
    manager.upgrade("app")
    self.assertEqual("app:\n  image: custom\n", target.read_text())
    self.assertEqual((directory / "service.yml").read_text(), target.with_suffix(".yml.upstream").read_text())
    self.assertEqual(["service.yml"], manager.updates("app"))
    shutil.copy2(str(directory / "service.yml"), str(target))
    manager.upgrade("app")
    self.assertEqual([], manager.updates("app"))

  def test_upgrade_before_first_build_preserves_legacy_settings(self):
    self.template()
    self.original({"app": {"image": "legacy:custom"}})
    manager = ServiceManager(self.root)
    manager.upgrade("app")
    self.assertEqual("legacy:custom", ServiceManager(self.root).definitions()["app"]["image"])
    self.assertTrue((self.root / "services/app/service.yml.upstream").exists())

  def test_repair_keeps_inactive_service_inactive(self):
    self.template()
    manager = ServiceManager(self.root)
    manager.install(["app"])
    manager.deactivate(["app"])
    (self.root / "services/app/service.yml").unlink()
    manager.install(["app"], activate=False)
    self.assertEqual([], ServiceManager(self.root).active())
    self.assertEqual([], manager.missing("app"))

  def test_generated_presets_preserve_existing_env_values_and_full_timezone(self):
    self.template(environment=[{"variable": "APP_PASSWORD", "generate": "password"}])
    setDotEnvValue(self.root / ".env", "TZ", "America/Argentina/Buenos_Aires")
    manager = ServiceManager(self.root)
    manager.install(["app"])
    initial = loadDotEnv(self.root / ".env")
    self.assertEqual(32, len(initial["APP_PASSWORD"]))
    manager.install(["app"])
    self.assertEqual(initial, loadDotEnv(self.root / ".env"))
    self.assertEqual("America/Argentina/Buenos_Aires", initial["TZ"])
    zone = self.root / "zoneinfo/America/Argentina/Buenos_Aires"
    zone.parent.mkdir(parents=True)
    zone.touch()
    localtime = self.root / "localtime"
    localtime.symlink_to(zone)
    self.assertEqual("America/Argentina/Buenos_Aires", hostTimezone(localtime))

  def test_dotenv_device_prefixes_are_distinct_values(self):
    path = self.root / ".env"
    setDotEnvValue(path, "DEVICE", "/dev/ttyUSB10")
    setDotEnvValue(path, "DEVICE", "/dev/ttyUSB1")
    self.assertEqual("/dev/ttyUSB1", loadDotEnv(path)["DEVICE"])

  def test_executable_presets_are_never_evaluated(self):
    self.template(environment=[{"variable": "APP_SETTING", "preset": "$(touch exploited)"}])
    with self.assertRaisesRegex(ValueError, "Executable"):
      ServiceManager(self.root).install(["app"])
    self.assertFalse((self.root / "exploited").exists())

  def test_missing_environment_preserves_active_selection_and_does_not_run_hooks(self):
    directory = self.template(definitions={"app": {"image": "example", "environment": {"SECRET": "${SECRET:?set it}"}}})
    (directory / "build.py").write_text("def preBuild(context):\n  raise AssertionError('hook ran')\n")
    original = self.original({"app": {"image": "example", "environment": {"SECRET": "${SECRET:?set it}"}}})
    with self.assertRaisesRegex(ValueError, "Missing required"):
      buildStack(ServiceManager(self.root))
    self.assertEqual(["app"], ServiceManager(self.root).active())
    self.assertEqual(original, (self.root / "docker-compose.yml").read_bytes())
    self.assertFalse((self.root / "services/docker-compose.save.yml").exists())

  def test_compose_validation_failure_keeps_compose_and_cache(self):
    self.template()
    original = self.original({"app": {"image": "old"}})
    cache = self.root / "services/docker-compose.save.yml"
    writeYaml(cache, {"services": {"app": {"image": "old"}}})
    originalCache = cache.read_bytes()
    with patch("deps.stack_builder.validateCompose", side_effect=ValueError("Invalid Compose")):
      with self.assertRaisesRegex(ValueError, "Invalid Compose"):
        buildStack(ServiceManager(self.root))
    self.assertEqual(original, (self.root / "docker-compose.yml").read_bytes())
    self.assertEqual(originalCache, cache.read_bytes())

  def test_prebuild_and_postbuild_failure_preserve_compose_and_cache(self):
    directory = self.template()
    for action in ("preBuild", "postBuild"):
      (directory / "build.py").write_text("def %s(context):\n  return False\n" % action)
      original = self.original({"app": {"image": "old"}})
      cache = self.root / "services/docker-compose.save.yml"
      writeYaml(cache, {"services": {"app": {"image": "old"}}})
      originalCache = cache.read_bytes()
      with patch("deps.stack_builder.validateCompose"):
        with self.assertRaisesRegex(RuntimeError, action):
          buildStack(ServiceManager(self.root))
      self.assertEqual(original, (self.root / "docker-compose.yml").read_bytes())
      self.assertEqual(originalCache, cache.read_bytes())

  def test_postbuild_script_failure_rolls_back_generated_stack(self):
    self.template()
    original = self.original({"app": {"image": "old"}})
    (self.root / "postbuild.sh").write_text("#!/bin/sh\nexit 7\n")
    (self.root / "postbuild.sh").chmod(0o755)
    with patch("deps.stack_builder.validateCompose"):
      with self.assertRaisesRegex(RuntimeError, "exit status 7"):
        buildStack(ServiceManager(self.root))
    self.assertEqual(original, (self.root / "docker-compose.yml").read_bytes())

  def test_override_precedence_and_saved_definitions(self):
    self.template()
    manager = ServiceManager(self.root)
    manager.install(["app"])
    writeYaml(self.root / "services/app/override.yml", {"app": {"image": "local", "ports": ["1234:80"]}})
    writeYaml(self.root / "compose-override.yml", {"services": {"app": {"image": "global"}}})
    with patch("deps.stack_builder.validateCompose"):
      buildStack(manager)
    effective = readServices(self.root / "docker-compose.yml")["services"]["app"]
    self.assertEqual("global", effective["image"])
    self.assertEqual(["1234:80"], effective["ports"])
    self.assertEqual("example:test", ServiceManager(self.root).definitions()["app"]["image"])

  def test_existing_otbr_selection_and_data_survive_migration(self):
    self.template("otbr")
    self.template("app")
    self.original({"otbr": {"image": "old"}, "app": {"image": "example"}})
    data = self.root / "volumes/otbr/data"
    data.mkdir(parents=True)
    (data / "keep").write_text("existing data")
    manager = ServiceManager(self.root)
    self.assertIn("otbr", manager.catalog.names)
    self.assertIn("otbr", manager.active())
    with patch("deps.stack_builder.validateCompose"):
      buildStack(manager)
    self.assertEqual({"image": "old"}, readServices(self.root / "docker-compose.yml")["services"]["otbr"])
    self.assertEqual("existing data", (data / "keep").read_text())


class BundledCatalogTests(unittest.TestCase):
  def test_manifests_resolve_all_files_and_dependencies(self):
    catalog = ServiceCatalog(ROOT)
    for name in catalog.names:
      with self.subTest(service=name):
        self.assertIn(name, catalog.dependencies([name]))

  def test_n8n_does_not_share_gitea_database_or_use_removed_mysql_backend(self):
    definition = ServiceCatalog(ROOT).definition("n8n", installed=False)
    self.assertEqual({"n8n"}, set(definition))
    self.assertNotIn("gitea", str(definition))
    self.assertNotIn("MYSQL", str(definition).upper())

  def test_pgadmin_persists_data_without_shadowing_image_code(self):
    definition = ServiceCatalog(ROOT).definition("pgadmin4", installed=False)["pgadmin4"]
    self.assertEqual("dpage/pgadmin4:latest", definition["image"])
    self.assertEqual(["./volumes/pgadmin4/pgadmin:/var/lib/pgadmin"], definition["volumes"])


if __name__ == "__main__":
  unittest.main()
