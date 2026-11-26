# contracts-v1 archive golden vectors

Copied verbatim from the SDK's frozen archive contract, `runwhen-capability`
`contracts/vectors/archive/` (commit `89fd813afc0475de5cd17a2f111b3b267aa74154`; the valid vectors were added in `65b0bb0` and are unchanged since).
Do not edit; to change one, change it in `runwhen-capability` and re-copy.

Each case is `<name>.json` (`description`, `entries[]` of `path`/`dataBase64`/`executable`, `sha256`, `size`)
and `<name>.tar` (the exact archive bytes). `tests/test_archive.py::test_contracts_v1_vectors` checks that
`build_archive(entries)` equals the `.tar` and that `read_archive(tar)` returns the entries.

`invalid/` holds the malicious archives from the same directory: each `<name>.tar` must make `read_archive` raise
`ArchiveError` with the `expectedCode` in `<name>.json` (`tests/test_archive.py::test_contracts_v1_invalid_vectors_rejected`).
