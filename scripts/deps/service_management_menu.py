"""Service installation and update actions for the existing Python menu."""
from deps.environment_options import _runMenu
from deps.issue_viewer import runIssueViewer
from deps.service_catalog import ServiceManager


def runServiceMenu(term, renderMode):
  with term.fullscreen(), term.cbreak():
    try:
      manager = ServiceManager()
      while True:
        entries = []
        for name in manager.catalog.names:
          state = "active" if name in manager.active() else (
            "inactive" if name in manager.state["services"] else "available"
          )
          entries.append((name, "%s [%s]" % (name, state)))
        name = _runMenu(term, renderMode, "Manage Services", "Select a service", entries)
        if name is None:
          return
        while True:
          action = _runMenu(term, renderMode, name, manager.catalog.manifest(name)["description"], [
            ("install", "Install / Activate (including dependencies)"),
            ("deactivate", "Deactivate"),
            ("repair", "Repair missing files"),
            ("update", "Check installed files and template updates"),
            ("upgrade", "Apply template updates / stage local conflicts"),
          ])
          if action is None:
            break
          try:
            if action in ("install", "repair"):
              names = manager.install([name], activate=action == "install")
              report = "Prepared: " + ", ".join(names)
            elif action == "deactivate":
              manager.deactivate([name])
              report = "Deactivated. Build Stack to update the Compose file."
            elif action == "update":
              report = "Missing: %s\nTemplate changes: %s" % (
                ", ".join(manager.missing(name)) or "none",
                ", ".join(manager.updates(name)) or "none",
              )
            else:
              report = "\n".join(manager.upgrade(name)) or "Up to date."
            runIssueViewer(term, renderMode, [(name, action, report)])
          except Exception as error:
            runIssueViewer(term, renderMode, [(name, "Error", str(error))])
    except Exception as error:
      runIssueViewer(term, renderMode, [("Service state", "Error", str(error))])
