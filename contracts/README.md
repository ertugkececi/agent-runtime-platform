# contracts

`openapi.json` is the HTTP contract of the application. It is **generated**, never
edited by hand, and it is the single source of truth for the interface:

- Backend routes and schemas come from the FastAPI application.
- Consumers generate their clients from this file. The frontend client is
  generated in the console repository (#84).
- No second copy of the schema is maintained anywhere.

## Regenerating

```sh
python scripts/export_openapi.py
```

## Drift

`tests/test_openapi_contract.py` regenerates the document and fails when the
committed copy differs, so the existing test gate catches any route or schema
change that was not exported.

## Why JSON

JSON is FastAPI's native format and needs no extra dependency. Producing YAML
would add a parser to the toolchain purely to change the file extension.

## Not covered

Client generation and its CI job arrive with the console repository (#84). This
directory only holds the contract.
