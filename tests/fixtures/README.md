# Test fixtures

| File | What it is | How to make it again |
|---|---|---|
| `demo.bin` | A synthetic pen recording (16-byte `input_event` records), generated, never edited. A test fails when it drifts from the generator. | `uv run inkboard demo --out tests/fixtures/demo.bin` |
| `rm2-sample.bin` | A real recording from a reMarkable 2 in portrait mode: a circle, a square and the word "inkboard" (11 strokes, 11,734 events). It is the only check of the decoder and the default orientation against a real device. Deliberately non-personal; its timestamps were shifted to start at 1000 s, with every gap unchanged. | `uv run inkboard record --out tests/fixtures/rm2-sample.bin --seconds 30`, drawing only those three things |

Raw captures of your own drawing go to `recordings/`, which git ignores: they are handwriting.
Test data never uses real device identifiers or networks: MAC addresses come from the
documentation range `00:00:5E:00:53:xx` (RFC 7042) and addresses from the example LAN
`192.168.1.0/24`.
