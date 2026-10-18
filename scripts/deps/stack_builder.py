"""Shared Python build path for the terminal menu and service CLI."""
import copy
import os
import pathlib
import shutil
import subprocess
import tempfile

from ruamel.yaml import YAML

from deps.compose_environment import requiredEnvironmentIssues
from deps.service_catalog import ServiceManager, atomicWrite, writeYaml
from deps.service_hooks import HookContext, runServiceHook, serviceHookAvailable


def overlay(default, override):
  """IOTstack overrides replace lists/scalars and recursively overlay mappings."""
  if isinstance(default, dict) and isinstance(override, dict):
    result = copy.deepcopy(default)
    for key, value in override.items():
      result[key] = overlay(result[key], value) if key in result else copy.deepcopy(value)
    return result
  return copy.deepcopy(override)


def loadMapping(path):
  with pathlib.Path(path).open() as stream:
    value = YAML().load(stream)
  if value is None:
    return {}
  if not isinstance(value, dict):
    raise ValueError("Expected a YAML mapping in %s" % path)
  return value


def composeCommand():
  try:
    result = subprocess.run(["docker", "compose", "version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0:
      return ["docker", "compose"]
  except OSError:
    pass
  if shutil.which("docker-compose"):
    return ["docker-compose"]
  raise RuntimeError("Docker Compose is required to validate the stack before replacing it")


def validateCompose(root, document):
  """Parse the effective project, including Docker's separate override file."""
  with tempfile.TemporaryDirectory(prefix="iotstack-config-") as directory:
    candidate = pathlib.Path(directory) / "docker-compose.yml"
    writeYaml(candidate, document)
    command = composeCommand() + ["--project-directory", str(root), "-p", "iotstack", "-f", str(candidate)]
    if (root / ".env").exists():
      command += ["--env-file", str(root / ".env")]
    override = root / "docker-compose.override.yml"
    if override.exists():
      command += ["-f", str(override)]
    result = subprocess.run(command + ["config", "--quiet"], cwd=str(root),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode:
      raise ValueError("Compose validation failed; existing stack preserved:\n%s" % result.stderr.strip())


def runHooks(root, names, action, services, renderMode, terminal):
  for name in names:
    path = root / ".templates" / name / "build.py"
    if not path.exists():
      continue
    context = HookContext(services, name, renderMode=renderMode, terminal=terminal)
    if not serviceHookAvailable(path, action, context):
      continue
    if runServiceHook(path, action, context) is False:
      raise RuntimeError("%s reported a failure for '%s'" % (action, name))
    services = context.services
  return services


def buildStack(manager=None, names=None, services=None, renderMode="ascii", terminal=None):
  manager = manager or ServiceManager()
  root = manager.root
  names = manager.active() if names is None else list(names)
  expanded = manager.catalog.dependencies(names)
  if not names:
    raise ValueError("Select at least one service before building")
  manager.install(expanded, activate=False)
  definitions = manager.definitions(expanded)
  if services is not None:
    definitions.update(copy.deepcopy(services))
  for name in expanded:
    missing = manager.missing(name)
    if missing:
      raise ValueError("%s is missing installed files: %s" % (name, ", ".join(missing)))

  def assemble(selected):
    base = loadMapping(root / ".templates/docker-compose-base.yml")
    base = overlay(base, manager.base)
    base["services"] = copy.deepcopy(selected)
    for name in expanded:
      override = root / "services" / name / "override.yml"
      if override.exists():
        base["services"] = overlay(base["services"], loadMapping(override))
    override = root / "compose-override.yml"
    if override.exists():
      base = overlay(base, loadMapping(override))
    return base

  # Configuration errors must be found before privileged/file-generating hooks.
  document = assemble(definitions)
  effective = document
  dockerOverride = root / "docker-compose.override.yml"
  if dockerOverride.exists():
    effective = overlay(effective, loadMapping(dockerOverride))
  issues = requiredEnvironmentIssues(effective, envPath=root / ".env")
  if issues:
    raise ValueError("Missing required settings:\n" + "\n".join(issues.values()))
  for name, service in effective["services"].items():
    for dependency in service.get("depends_on", []):
      if dependency not in effective["services"]:
        raise ValueError("%s requires the missing service %s" % (name, dependency))
  validateCompose(root, document)

  previous = pathlib.Path.cwd()
  try:
    os.chdir(str(root))
    definitions = runHooks(root, expanded, "preBuild", definitions, renderMode, terminal)
    document = assemble(definitions)
    validateCompose(root, document)
    compose = root / "docker-compose.yml"
    original = compose.read_text() if compose.exists() else None
    writeYaml(compose, document)
    try:
      definitions = runHooks(root, expanded, "postBuild", definitions, renderMode, terminal)
      if (root / "postbuild.sh").exists():
        result = subprocess.call(["./postbuild.sh"] + list(definitions))
        if result:
          raise RuntimeError("postbuild.sh failed with exit status %s" % result)
    except Exception:
      if original is None:
        compose.unlink()
      else:
        atomicWrite(compose, original)
      raise
    if original is not None and original != compose.read_text():
      # Unique backups preserve the original through repeated builds.
      descriptor, backup = tempfile.mkstemp(prefix="docker-compose.", suffix=".save", dir=str(root / "services"))
      with os.fdopen(descriptor, "w") as stream:
        stream.write(original)
    writeYaml(root / "services/docker-compose.save.yml", {"services": definitions})
    manager.remember(expanded, definitions)
    return definitions
  finally:
    os.chdir(str(previous))
