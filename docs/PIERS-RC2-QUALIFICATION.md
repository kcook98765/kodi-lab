# Piers 22.0 RC2 managed runtime

Issue [#349](https://github.com/kcook98765/kodi-lab/issues/349) introduces a
separately pinned Linux amd64 development preview. Native qualification is
pending on this source checkpoint; the earlier RC1 results remain historical.

The official `22.0rc2-Piers` tag resolves to
`d686683a55d50e576dd282f53fb2bf9914931b38`. Its `version.txt` declares RC2.
Flathub beta build 340397, from manifest commit
`0bae0f46fbdd6a0b2600b341c077f09dc58e4bcf`, published app commit
`83b803ab50c19471377943f35bed0d96247a425834580a420d5842912ce7cbdd`.
The commit object's SHA256 matches that identity; its detached signature verifies
with the existing pinned Flathub primary key and signing subkey. Signed metadata
binds the x86_64 beta ref and `org.flathub.Beta` collection. The app's embedded
manifest names the official RC2 source commit, and its metainfo names
`22.0~rc2-Piers`. The beta ref endpoint still returned the RC1 checksum during
investigation; acquisition selects the immutable verified RC2 commit directly.

Recipe `piers-22.0-rc2-r4`, revision 4, has digest
`sha256:16b275718524e6dafef27691677d07ac4bffb7af97e5960d227c0ae54bfa8527`.
It preserves the reviewed RC1 platform, GL, Ubuntu base and support-package pins.
Only the app payload and exact-release image/health identities change. The native
validator requires RC2's ordinal and source revision. RC1 artifacts, consent and
validation cannot satisfy RC2. RC2 provides Python API 3.1.1, ABI 3.0.0; source
admission uses that separately verified descriptor rather than RC1's 3.1.0.

The existing reviewed preview Narrow Bridge 1.1.32 reports the actual native
release dynamically, including ordinal 2. Its source, ZIP and fingerprint remain
unchanged; exact live RC2 authentication still requires native qualification.
The stable Bridge and Matrix/Nexus/Omega runtime pins are unchanged.

Fresh empty Piers slots select RC2 with explicit immutable preview consent.
Retained RC1 profiles keep their exact image, version, trust and operation history.
Ordinary Start, Stop and Reset reuse their recorded runtime; Reset is not an
upgrade. An occupied Piers slot is not automatically replaced. A production
transition requires separately reviewed preservation, exact target/receipt
reconciliation, and an explicitly authorized supported removal/recreation plan.
Original uncertain receipts must remain fenced. This source campaign does not
authorize deleting the retained Claw RC1 profile.

Acquisition retains the existing 8 GiB admission check, 6 GiB staging bound and
OSTree's default storage reserve. Disk capacity must also accommodate Docker's
unpacked build context, intermediate snapshots and compressed image layers.
The isolated QA configuration may use a bounded tmpfs acquisition/socket volume;
application data, profiles and receipts remain on separate persistent storage.

Private campaign evidence and recovery:
`/home/kyle/kodi-lab-deployment/piers-rc2-core-20261112/`.
