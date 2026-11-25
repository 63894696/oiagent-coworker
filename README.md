# OIagent Coworker

Curated module-level derivation from [andrewyng/openworker](https://github.com/andrewyng/openworker) (upstream MIT).
W2/W3 extraction: permission / inbox / selfwake / personas / skills + W3 audit observation pipeline.

Upstream OpenWorker remains MIT (see [LICENSE-OPENWORKER](LICENSE-OPENWORKER)).
This repository is released under **AGPL-3.0-or-later** — see [LICENSE](LICENSE).

## Layout

```
oiagent_coworker/
  permissions/   policy gate, audit stream, path sandbox, shell classifier
  inbox/         inbox models + persistence + service
  selfwake/      scheduler + persistence
  persona/       persona models + service
  skills/        loader, manifest, stage confirm
docs/            extraction plans and build notes
scripts/         fork helpers and probes
```

## Docs

- [docs/W2-extraction-plan.md](docs/W2-extraction-plan.md)
- [docs/W2-extraction-manifest.md](docs/W2-extraction-manifest.md)
- [docs/build.md](docs/build.md)

## License

**GNU Affero General Public License v3.0 or later** — see [LICENSE](LICENSE).
Upstream attribution: [LICENSE-OPENWORKER](LICENSE-OPENWORKER).
