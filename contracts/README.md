# contracts/

Generated, versioned interface artifacts. This directory is the published form of a
contract; it is never edited by hand.

```text
contracts/
  schemas/    canonical JSON Schema documents
  fixtures/   cross-language golden payloads with declared expected outcomes
```

`proto/`, `openapi/` and `codegen/` arrive with the services that need them
(`docs/repository-structure.md`).

## Current contracts

| Contract | Source of truth | Generator | Decision |
|---|---|---|---|
| `hear.ingest.v1` | `hear/ingest/envelope.py` | `tools/gen_ingest_contracts.py` | `docs/decisions/0001-hear-ingest-v1-envelope-and-codec.md` |

Regenerate and verify:

```sh
python3 tools/gen_ingest_contracts.py           # rewrite artifacts
python3 tools/gen_ingest_contracts.py --check   # CI gate: fail on drift
```

## Rules

1. **Do not edit generated files.** Change the field table, regenerate, commit both.
2. **Additive only within a major.** New optional fields and new enum members are
   compatible. Removing a field, narrowing a type, or changing a meaning requires a new
   major and a new schema file.
3. **Readers accept unknown fields and unknown enum members** inside a supported major and
   preserve them verbatim. An unsupported major is refused with a machine reason and a
   durable refusal record.
4. **Identity inputs are frozen per major.** Extending the set that derives an event ID
   re-identifies existing data, so it is a major version change.
5. **A cross-owner change ships its fixture in the same commit** as the code that needs it.
   A fixture carries its expected validation outcome, so an adapter in any language can
   assert against it without importing this repository.
