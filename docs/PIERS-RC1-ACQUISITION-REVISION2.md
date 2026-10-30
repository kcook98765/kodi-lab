# Piers RC1 support acquisition revision 2

The official candidate remains Kodi 22.0 RC1, source tag `22.0rc1-Piers`, commit
`28ea2eac1eb7af8fdcbd2672d933ce87f594be79`. The tag is lightweight; the commit
has a verified GitHub signature. Team Kodi announced RC1 on October 10, 2026.
The official website and GitHub release list checked on October 30 UTC showed
no newer published release candidate. Piers remains a development preview.

The old installation recipe and support manifest remain immutable. Four pinned
Ubuntu support DEBs are no longer served from their old archive URLs:

| Package | Exact version | Architecture | Bytes | SHA-256 |
|---|---|---|---:|---|
| ca-certificates | 20260601~26.04.1 | all | 138514 | 6077d27c6b6f8b23590cb01ff877ed8c804a67a5442cc32b5a33da10d2bd0e90 |
| tzdata | 2026c-0ubuntu0.26.04.1 | all | 193008 | d1b10c964c53344b2b99d093bd437391460eec9a872961c2beb256d64a8c5bbf |
| xserver-common | 2:21.1.22-1ubuntu1.2 | all | 29810 | b8c4c5c7d10d3662dd9e1bf65191c35e057adcd4d0d8ed6d76653dbc064eef0e |
| xvfb | 2:21.1.22-1ubuntu1.2 | amd64 | 898612 | fac5e03c7a40668d475178efb85dc63b4b391b020e0e9f499a729d2b62466fe8 |

All four original binaries were recovered byte-identically from Ubuntu's fixed
`https://snapshot.ubuntu.com/ubuntu/20261001T000000Z/` snapshot. Their versions,
architectures, sizes and hashes were independently matched to package indexes
covered by Ubuntu-signed InRelease metadata, verified with archive key
`F6ECB3762474EDA9D21B7022871920D1991BC93C`. Launchpad confirms that the pinned
ca-certificates publication was superseded and removed on October 22.

Installation recipe `piers-22.0-rc1-r2`, revision 2, uses four explicit snapshot
URLs. The other 93 archive URLs and all 97 package versions/hashes/lengths,
base/final inventories, dependencies, Kodi source and OSTree component pins are
unchanged. There is no runtime APT resolution, moving snapshot, redirect or
fallback. The snapshot host, exact timestamp path and four package names are
closed. Existing download concurrency, deadline, byte limits, private staging,
HMAC approval and native validation remain enforced.

The new support manifest has a new digest. A support-revision image label gives
the r2 assembly its own reviewed asset/recipe digests. Historical r1 resources,
acknowledgements and attestations retain their identities; they cannot authorize
or be relabelled as r2. New installation requires exact r2 preview consent.
The inactive OSTree acquisition input remains revision 1 because its signed
upstream component bytes are unchanged; installation and support provenance are
separately bound by the r2 attestation.

This source change permits reviewed acquisition. It does not mark an image ready,
provision Kodi, establish Bridge trust, change retained profiles or qualify a
native Piers development loop. Those gates require separate native evidence.

Official references: [Team Kodi announcement](https://kodi.tv/article/kodi-22-piers-rc-1/),
[GitHub release](https://github.com/xbmc/xbmc/releases/tag/22.0rc1-Piers),
[Ubuntu snapshot service](https://snapshot.ubuntu.com/),
[original Ubuntu publication](https://api.launchpad.net/1.0/ubuntu/+archive/primary/+binarypub/245503080).
