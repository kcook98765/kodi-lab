# Cross-Version Movies fixture family

These are three ordinary single-release Kodi Lab fixtures for compatibility
testing. They share the same nine synthetic movie titles, years, NFO metadata,
media placeholders, and artwork. Each has its own immutable definition and a
configuration-only video database seed made for its Kodi generation. This is a
fixture family for documentation and testing, not a multi-release fixture type.

| Fixture slug | UUID | Kodi release | Video database |
| --- | --- | --- | --- |
| `cross-movies-matrix-v1` | `8b25126e-f633-4778-bb24-41e232e19f26` | 19.4 | `MyVideos119.db` |
| `cross-movies-nexus-v1` | `f153d0d8-d76b-42e7-984c-f7a9b2fd0aa1` | 20.5 | `MyVideos121.db` |
| `cross-movies-omega-v1` | `81765a3d-1fd8-4b16-8f2e-850858052093` | 21.3 | `MyVideos131.db` |

All three scan nine movies named `Kodi Lab Cross-Version Movie 001` through
`009`, with the following years:

| Decade | Years | Movies |
| --- | --- | ---: |
| 2000s | 2001, 2005, 2009 | 3 |
| 2010s | 2011, 2015, 2019 | 3 |
| 2020s | 2021, 2024, 2026 | 3 |

The seeded databases contain source configuration but no movie rows; Kodi's
normal local scan creates the library from the packaged media and NFO files.
No Internet source is configured. Watched and resume state are omitted for the
whole family because the existing trusted post-scan playback contract is
Nexus-specific. Each member remains eligible only for its declared release.

`scripts/materialize-cross-movies-v1.py` records the fixed inputs and produces
the three immutable packages. It refuses to overwrite them. The focused package
test reproduces and compares every generated artifact without touching live
Kodi targets.
