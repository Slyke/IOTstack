# pgAdmin4

pgAdmin4 provides a graphical interface to PostgreSQL. New installations use the
official `dpage/pgadmin4:latest` image. See the
[container deployment documentation](https://www.pgadmin.org/docs/pgadmin4/latest/container_deployment.html)
for supported platforms and configuration.

## Setup

Select `pgadmin4` in the Python menu, then use **Options** to set
`PGADMIN_DEFAULT_EMAIL` and `PGADMIN_DEFAULT_PASSWORD`. Installation can generate
the initial password in `.env`; these variables initialize the administrator
account on first launch. The HTTP interface is exposed on port `5050`.

The official image runs as UID/GID 5050. Before starting a new installation,
prepare its bind-mounted data directory from the IOTstack directory:

```console
sudo install -d -o 5050 -g 5050 -m 700 ./volumes/pgadmin4/pgadmin
docker compose up -d pgadmin4
```

Open `http://YOUR-HOST:5050` and sign in using the configured email and password.
Use **Add New Server** to register PostgreSQL. When using IOTstack's `postgres`
service on the same Docker network, the hostname is `postgres` and the port is
`5432`. Supply its configured database, username, and password.

Only `./volumes/pgadmin4/pgadmin:/var/lib/pgadmin` is persisted. Do not mount a
volume over `/pgadmin4`: that is application code supplied by the image, and
persisting it can prevent image upgrades from replacing old code.

`TZ` uses the shared `.env` setting, defaulting to `Etc/UTC`. HTTPS can be supplied
by a reverse proxy; native container TLS requires additional certificate setup
described in the official documentation.

## Existing installations

The service manager preserves installed definitions, including the previous
`gpongelli/pgadmin4-arm` image and data paths. Template upgrade may produce a
`service.yml.upstream` candidate for review. Adopting it is a separate application
migration: back up the existing data, check image architecture support, and follow
pgAdmin's migration guidance. Do not point the new image at existing data or change
its ownership without checking that migration first. In particular, 32-bit ARM
hosts must verify image support before moving away from their existing image.
