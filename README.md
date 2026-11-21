# Blast Radius

Which of sqlglot's tests can a patch break? This repository answers from maps
of sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

| Path | What |
| --- | --- |
| `maps/` | The maps, as plain JSON (about 7.5 MB): what every test executes, reads and opens at the pinned commit, and a description of the source. Formats: `maps/README.md`. How they fared against patches with known answers: `maps/VERIFY.md`. |
| `tools/` | The scripts that build the maps from a sqlglot checkout and the kit's `tests.json`, and that check them. `tools/README.md`. |

The maps are measurements of the pinned commit only. No patch, and no list of
patches or kinds of edit with the tests they break, is stored anywhere here.
