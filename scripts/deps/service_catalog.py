"""Declarative service metadata, adapted from Paraphraser's iotstack-menu.

Manifests are data: presets are never evaluated as shell or Python code.
"""
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import string
import tempfile

from ruamel.yaml import YAML

from deps.compose_environment import loadDotEnv, setDotEnvValue


def atomicWrite(path, text):
  path = pathlib.Path(path)
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
  try:
    with os.fdopen(descriptor, "w") as stream:
      stream.write(text)
    if path.exists():
      os.chmod(temporary, path.stat().st_mode & 0o777)
    os.replace(temporary, str(path))
  finally:
    if os.path.exists(temporary):
      os.unlink(temporary)


def writeYaml(path, value):
  import io
  stream = io.StringIO()
  YAML().dump(value, stream)
  atomicWrite(path, stream.getvalue())


def readServices(path):
  with pathlib.Path(path).open() as stream:
    document = YAML().load(stream)
  if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
    raise ValueError("%s must contain a services mapping; existing state was not replaced" % path)
  return document


def fileDigest(path):
  return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def copyMissing(source, destination):
  """Install files/directories without overwriting local edits on repair."""
  source, destination = pathlib.Path(source), pathlib.Path(destination)
  if source.is_dir():
    destination.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
      copyMissing(child, destination / child.name)
  elif not destination.exists():
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(destination))


def hostTimezone(localtime="/etc/localtime", timezone="/etc/timezone"):
  """Keep the full IANA name, including three-component regions."""
  resolved = str(pathlib.Path(localtime).resolve())
  if "/zoneinfo/" in resolved:
    return resolved.split("/zoneinfo/", 1)[1]
  path = pathlib.Path(timezone)
  return path.read_text().strip() if path.is_file() else "Etc/UTC"


class ServiceCatalog:
  def __init__(self, root="."):
    self.root = pathlib.Path(root).resolve()
    self.templates = self.root / ".templates"
    self.names = sorted(path.parent.name for path in self.templates.glob("*/service.yml"))

  def manifest(self, name):
    if name not in self.names:
      raise ValueError("Unknown service: %s" % name)
    path = self.templates / name / "menu-config.json"
    if path.exists():
      data = json.loads(path.read_text())
    else:
      data = {"service": name, "description": name, "dependencies": [], "environment": [],
              "install": [{"template": "service.yml", "service": "service.yml", "tracked": True}]}
    if data.get("service") != name:
      raise ValueError("Manifest service does not match directory: %s" % path)
    for item in data.get("install", []):
      self.itemPaths(name, item)
    return data

  def itemPaths(self, name, item):
    sourceRoot = (self.templates / name).resolve()
    targetRoot = (self.root / "services" / name).resolve()
    source = (sourceRoot / item["template"]).resolve()
    target = (targetRoot / item["service"]).resolve()
    for path, parent in ((source, sourceRoot), (target, targetRoot)):
      if path != parent and parent not in path.parents:
        raise ValueError("Manifest path escapes service directory: %s" % path)
    if not source.exists():
      raise ValueError("Missing template item: %s" % source)
    return source, target

  def dependencies(self, names):
    ordered, visiting = [], set()

    def visit(name):
      if name in visiting:
        raise ValueError("Circular service dependency involving %s" % name)
      if name in ordered:
        return
      visiting.add(name)
      for dependency in self.manifest(name).get("dependencies", []):
        visit(dependency)
      visiting.remove(name)
      ordered.append(name)

    for name in names:
      visit(name)
    return ordered

  def definition(self, name, installed=True):
    self.manifest(name)
    path = self.root / "services" / name / "service.yml"
    if not installed or not path.exists():
      path = self.templates / name / "service.yml"
    with path.open() as stream:
      value = YAML().load(stream)
    if not isinstance(value, dict) or name not in value:
      raise ValueError("%s must define the %s service" % (path, name))
    return value


class ServiceManager:
  """Persist selection independently of generated Compose files.

Migration reads YAML directly so unresolved Compose variables retain the user's
selection. Invalid YAML is an error, never an empty-stack migration.
"""
  def __init__(self, root="."):
    self.catalog = ServiceCatalog(root)
    self.root = self.catalog.root
    self.path = self.root / "services/.menu-state.json"
    self.external = {}
    self.base = {}
    self.saved = {}
    self.state = {"version": 1, "services": {}}
    if self.path.exists():
      self.state = json.loads(self.path.read_text())
      if self.state.get("version") != 1 or not isinstance(self.state.get("services"), dict):
        raise ValueError("Unsupported service state: %s" % self.path)
    else:
      self._migrate()
    self.external = self.state.get("external", self.external)
    self.base = self.state.get("base", self.base)
    self.saved = self.state.get("saved", self.saved)

  def _migrate(self):
    cache = self.root / "services/docker-compose.save.yml"
    compose = self.root / "docker-compose.yml"
    if compose.exists():
      document = readServices(compose)
      self.base = {key: value for key, value in document.items() if key != "services"}
    if cache.exists():
      self.saved = dict(document["services"]) if compose.exists() else {}
      self.saved.update(readServices(cache)["services"])
    elif compose.exists():
      self.saved = document["services"]
    owned = set()
    for name in self.catalog.names:
      if name in self.saved:
        definition = self.catalog.definition(name, installed=False)
        owned.update(definition)
        owned.update(self.catalog.definition(name))
        self.state["services"][name] = {"active": True, "tracked": {}}
    # Preserve custom services and companions instead of dropping them on build.
    self.external = {name: value for name, value in self.saved.items()
                     if name not in owned}

  def active(self):
    return sorted(name for name, state in self.state["services"].items()
                  if state.get("active") and name in self.catalog.names)

  def save(self):
    self.state["external"] = self.external
    self.state["base"] = self.base
    self.state["saved"] = self.saved
    atomicWrite(self.path, json.dumps(self.state, indent=2) + "\n")

  def install(self, names, activate=True):
    selected = self.catalog.dependencies(names)
    # Validate the entire dependency closure before making any changes.
    manifests = [(name, self.catalog.manifest(name)) for name in selected]
    if selected and "TZ" not in loadDotEnv(self.root / ".env") and "TZ" not in os.environ:
      setDotEnvValue(self.root / ".env", "TZ", hostTimezone())
    for name, manifest in manifests:
      existing = self.state["services"].get(name)
      tracked = dict(existing.get("tracked", {})) if existing else {}
      for item in manifest["install"]:
        source, target = self.catalog.itemPaths(name, item)
        if item["service"] == "service.yml" and name in self.saved and not target.exists():
          definitions = self.catalog.definition(name, installed=False)
          writeYaml(target, {key: self.saved.get(key, value) for key, value in definitions.items()})
          for key in definitions:
            self.saved.pop(key, None)
        copyMissing(source, target)
        if item.get("tracked") and source.is_file():
          # Never mark a customized or old file as current just because repair ran.
          if target.is_file() and fileDigest(target) == fileDigest(source):
            tracked[item["template"]] = fileDigest(source)
      if existing is None:
        self._presets(manifest)
      self.state["services"][name] = {"active": activate or bool(existing and existing.get("active")),
                                      "tracked": tracked}
    self.save()
    return selected

  def _presets(self, manifest):
    path = self.root / ".env"
    current = loadDotEnv(path)
    for item in manifest.get("environment", []):
      name = item["variable"]
      if name in current or name in os.environ:
        continue
      if item.get("generate") == "password":
        value = "".join(secrets.choice(string.ascii_letters + string.digits) for unused in range(32))
      elif "preset" in item:
        value = item["preset"]
        if "$" in value or "`" in value:
          raise ValueError("Executable environment presets are not supported: %s" % name)
      else:
        raise ValueError("Unknown environment generator for %s" % name)
      setDotEnvValue(path, name, value)
      current[name] = value

  def deactivate(self, names):
    for name in names:
      self.catalog.manifest(name)
    remaining = set(self.active()) - set(names)
    for name in remaining:
      missing = set(self.catalog.dependencies([name])) & set(names)
      if missing:
        raise ValueError("%s requires %s; deactivate its dependants first" % (name, ", ".join(sorted(missing))))
    for name in names:
      if name in self.state["services"]:
        self.state["services"][name]["active"] = False
    self.save()

  def definitions(self, names=None):
    result = dict(self.external)
    for name in self.active() if names is None else names:
      definitions = self.catalog.definition(name)
      for key, value in definitions.items():
        result[key] = self.saved.get(key, value)
    return result

  def missing(self, name):
    missing = []
    for item in self.catalog.manifest(name)["install"]:
      source, target = self.catalog.itemPaths(name, item)
      if not (target.is_dir() if source.is_dir() else target.is_file()):
        missing.append(str(target))
      elif source.is_dir():
        for child in source.rglob("*"):
          destination = target / child.relative_to(source)
          if not (destination.is_dir() if child.is_dir() else destination.is_file()):
            missing.append(str(destination))
    return missing

  def updates(self, name):
    if name not in self.state["services"]:
      return []
    tracked = self.state["services"][name].get("tracked", {})
    changes = []
    for item in self.catalog.manifest(name)["install"]:
      source, target = self.catalog.itemPaths(name, item)
      if item.get("tracked") and source.is_file() and tracked.get(item["template"]) != fileDigest(source):
        changes.append(item["template"])
    return changes

  def upgrade(self, name):
    """Update unchanged defaults and stage conflicts alongside customized files."""
    self.catalog.manifest(name)
    if name not in self.state["services"]:
      raise ValueError("Install %s before checking its updates" % name)
    # Materialize saved legacy settings before considering new defaults.
    self.install([name], activate=False)
    tracked = self.state["services"][name].setdefault("tracked", {})
    reports = []
    for item in self.catalog.manifest(name)["install"]:
      source, target = self.catalog.itemPaths(name, item)
      if not item.get("tracked") or not source.is_file():
        copyMissing(source, target)
        continue
      current = fileDigest(source)
      if target.exists() and fileDigest(target) != current:
        if fileDigest(target) == tracked.get(item["template"]):
          shutil.copy2(str(target), str(target) + "." + fileDigest(target)[:12] + ".backup")
          shutil.copy2(str(source), str(target))
          tracked[item["template"]] = current
          reports.append("Updated %s (previous version backed up)" % target)
          continue
        candidate = target.with_name(target.name + ".upstream")
        shutil.copy2(str(source), str(candidate))
        reports.append(str(candidate))
        continue
      copyMissing(source, target)
      tracked[item["template"]] = current
    self.save()
    return reports

  def remember(self, names, definitions):
    """Record a successful build, retaining inactive service configuration."""
    selected = set(names)
    for name, state in self.state["services"].items():
      state["active"] = name in selected
    for name in selected:
      owned = self.catalog.definition(name)
      value = {key: definitions[key] for key in owned if key in definitions}
      if value != self.catalog.definition(name):
        writeYaml(self.root / "services" / name / "service.yml", value)
      for key in owned:
        self.saved.pop(key, None)
    self.save()
