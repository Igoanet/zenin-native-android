---
name: Railway project setup via API
description: How the zenin-portal-bot Railway project (bot + Postgres + api-server) was built from scratch via Railway GraphQL API, with the mutations and pitfalls
---

## Rule
The entire Railway project can be created and managed via `https://backboard.railway.app/graphql/v2` with the RAILWAY_TOKEN secret. Key mutations: `projectCreate` (needs workspaceId — get via `{ me { workspaces { id } } }`), `serviceCreate` (source: `{ repo }` or `{ image }`), `serviceInstanceUpdate` (dockerfilePath, rootDirectory, restartPolicy), `variableCollectionUpsert` (pass variables as a GraphQL variable, NOT inline — inline `${{...}}` refs and nested objects break), `serviceDomainCreate`, `serviceInstanceDeploy`, `variableDelete`.

**Why:** The user expects full autonomous setup — no manual dashboard steps.

**How to apply:** Any future Railway service/env/deploy work on this project: project id is in this repl's memory of past sessions only — re-query `{ me { projects } }` or use `projects` query if empty (token is workspace-scoped and shows no projects under `me.projects`; use `projectCreate`/direct IDs or the `project(id:)` query with known IDs stored in shell history/notes).

## Pitfalls learned
- `preDeployCommand` is rejected by the API ("Invalid input") regardless of list format — run migrations from the start script (start.sh) instead.
- `postgres:15` image needs POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB set before first deploy or it fails silently (empty logs).
- Cross-service DB URL: `postgresql://<user>:<pass>@postgres.railway.internal:5432/<db>` works without Railway variable references.
- Pyrogram Client needs `in_memory=True` — Railway filesystem writes for .sessions fail.
- The `ghcr.io/railwayapp-templates/postgres-ssl:edge` image fails without clear logs; plain `postgres:15` works.
- To debug the DB from outside Railway: `tcpProxyCreate(input: {serviceId, environmentId, applicationPort: 5432})` gives a public host:port for psql; delete it afterward with `tcpProxyDelete(id)` — it's a public DB port.
