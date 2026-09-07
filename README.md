# Dhaka Kacchi AI harness

Layer 0 warehouse for the agentic operating system described in `ARCHITECTURE.md`.

## Quickstart

```bash
make env        # create .env from .env.example
make upgrade    # create the database, apply all migrations, seed the menu
make verify     # assert the schema matches house conventions
make gate       # run the ARCHITECTURE.md section 9 Phase-1 exit gate
```

`make help` lists everything. See `CLAUDE.md` for conventions and traps.

Requires a running PostgreSQL 13+ and [uv](https://docs.astral.sh/uv/).
