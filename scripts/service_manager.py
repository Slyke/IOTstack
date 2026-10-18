#!/usr/bin/env python3
"""Noninteractive counterpart of the Python service management menu."""
import argparse
import sys

from deps.service_catalog import ServiceManager
from deps.stack_builder import buildStack


def main(arguments=None):
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("command", choices=("status", "install", "activate", "deactivate",
                                         "repair", "update", "upgrade", "build"))
  parser.add_argument("services", nargs="*")
  args = parser.parse_args(arguments)
  try:
    manager = ServiceManager()
    names = args.services
    if args.command in ("install", "activate", "deactivate", "repair") and not names:
      parser.error("%s requires at least one service name" % args.command)
    for name in names:
      manager.catalog.manifest(name)
    if args.command == "status":
      for name in names or manager.catalog.names:
        installed = name in manager.state["services"]
        state = "active" if name in manager.active() else "inactive" if installed else "available"
        details = []
        if installed:
          if manager.missing(name):
            details.append("repair needed")
          if manager.updates(name):
            details.append("template changes available")
        print("%-23s %-10s %s" % (name, state, "; ".join(details)))
    elif args.command in ("install", "activate", "repair"):
      selected = manager.install(names, activate=args.command != "repair")
      print("Prepared: " + ", ".join(selected))
    elif args.command == "deactivate":
      manager.deactivate(names)
      print("Deactivated: " + ", ".join(names))
    elif args.command in ("update", "upgrade"):
      for name in names or sorted(manager.state["services"]):
        if args.command == "update":
          print("%s: %s" % (name, ", ".join(manager.updates(name)) or "up to date"))
        else:
          reports = manager.upgrade(name)
          print("%s: %s" % (name, "; ".join(reports) or "up to date"))
    elif args.command == "build":
      buildStack(manager, names=names or None)
      print("Validated and wrote docker-compose.yml. Start with docker compose up -d.")
    return 0
  except Exception as error:
    print("Error: %s" % error, file=sys.stderr)
    return 1


if __name__ == "__main__":
  sys.exit(main())
