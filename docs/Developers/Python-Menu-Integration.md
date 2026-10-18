# Python service management

This integration keeps the Python menu and ports service-management ideas from
[Paraphraser's iotstack-menu branch](https://github.com/Paraphraser/IOTstack/tree/iotstack-menu).
The merge retains upstream master through `37e7877`, including the original
service-management commits, with their original authors and hashes. Their history is merged; the Bash menu implementation
and its SQLite state store are intentionally excluded from the resulting tree.
Future changes on that branch must be reviewed against this integration decision.

## Menu and command line

Run `./menu.sh` as usual. **Build Stack** still provides Python service options,
hardware selection, credentials, and the existing Node-RED add-on workflow.
Selecting a service also selects its declared dependencies. **Manage Services**
provides install, activate, deactivate, repair, update checks, and template upgrades.

The same operations are available without opening an interactive menu:

```console
./menu.sh services status
./menu.sh services install python
./menu.sh services install rtl_433
./menu.sh services deactivate rtl_433
./menu.sh services repair python
./menu.sh services update
./menu.sh services upgrade python
./menu.sh services build
```

`install` activates the named services and their dependencies. `repair` restores
missing files without activating an inactive service. Deactivation retains files
and rejects removal of a dependency still needed by another active service.
These commands only prepare configuration. Use **Build Stack** or `services build`
to regenerate Compose, then `docker compose up -d` to apply it. Nothing starts or
stops containers automatically. Hardware options still use **Build Stack**.

`build` with no names uses the saved active selection. Providing names builds that
selection plus its dependencies. Independently configured custom services are
retained. Deactivation does not stop a running container; review and remove obsolete
containers separately when applying a reduced stack.

## State and migration

Service selection is stored in `services/.menu-state.json`. On first use, the Python
manager reads existing YAML without invoking Compose interpolation. It prefers
the old `services/docker-compose.save.yml` settings when present and retains custom
services from `docker-compose.yml`. Companion databases, unresolved variable
references, inactive settings, and existing `.env` values survive migration.
Malformed input raises an error instead of being interpreted as an empty stack.

Per-service definitions live in `services/NAME/service.yml`. Existing definitions
are retained when new templates are installed. Secrets are generated only for new
installations and only when the variable is absent from both `.env` and the process
environment. A missing `TZ` is initialized from the full host timezone name.

Builds apply `services/NAME/override.yml` service fragments, then the existing
`compose-override.yml` project overlay. Mapping keys merge recursively; lists and
scalar values are replaced by the override. Docker's separate
`docker-compose.override.yml` is included during validation and left separate.
Docker Compose must be installed to validate builds. Missing required variables,
Compose errors, and failed pre-build hooks leave the generated stack intact.
Post-build failure restores the previous Compose file and leaves its saved cache
unchanged; side effects performed by a hook cannot be rolled back automatically.
Successful changes retain the previous Compose file in `services/docker-compose.*.save`.

## Manifests and upgrades

Each template's `menu-config.json` describes its service, dependencies, description,
environment presets, and files to install. A `configure` value identifies the
Python hook where applicable; hook discovery still uses the existing Python API.
Manifests are data and never execute shell expressions. Password presets use
`"generate": "password"` with cryptographically random generation.

Installation and repair copy only missing files, including directory trees.
Tracked files are compared by SHA-256. `update` checks the currently checked-out
templates; it does not fetch Git changes or pull container images. `upgrade`
updates an unchanged installed default after backing it up. If a file was edited
locally or its previous template version is unknown, the candidate is written to
`FILE.upstream` for review. Compare it with the local file and merge deliberately.
Running upgrade again after adopting the candidate records the new template hash.
After a manual partial merge, the difference remains visible until reconciled.

Application data and database contents are never migrated by template upgrades.
Backups and staged candidates are local configuration files and may include secrets.

## Service changes and review fixes

The imported catalog adds Apt-Cacher-NG, Jellyfin, Tailscale, and Vaultwarden.
Selected template updates include Homebridge, Pi-hole 6, Node-RED's host gateway,
rtl_433's published image, and service manifests. Zigbee2MQTT retains its stable
internal serial-device mapping.

The integration addresses the review findings as follows:

* n8n keeps SQLite. It does not introduce the proposed MySQL backend or reuse
  Gitea's database directory. Obsolete MySQL and basic-auth examples were removed.
* pgAdmin uses the official image and persists only `/var/lib/pgadmin`, avoiding
  a mixture of application files from different image versions. Existing pgAdmin
  definitions are retained until explicitly migrated; see its container guide.
* Failed Compose interpolation cannot erase the migrated active service selection.
* Directory installations such as Python's `app` pass installation checks, and
  repeated Python builds retain the user's application.
* Full IANA timezone names are retained, and existing `TZ` values are preserved.
* Dotenv updates replace exact assignments, so `/dev/ttyUSB1` and `/dev/ttyUSB10`
  are distinct values.
* Vaultwarden uses its current single HTTP/WebSocket port. rtl_433's sample
  override does not assume a particular receiver serial number.

OTBR retains beta's Python hardware-selection and build hooks. Existing OTBR
selections, configuration, and data remain available after migration.

Regression tests run with `python -m unittest discover -s tests`. Compose parsing
checks do not establish that physical radios, VPN connectivity, application upgrades,
or database migrations work on a deployment host.
