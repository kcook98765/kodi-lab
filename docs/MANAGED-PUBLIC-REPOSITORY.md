# Test the published repository on disposable managed Omega

This release-testing workflow is opt-in. Use a disposable lab with an
unprepared Omega profile. The normal managed installer, dependency catalog
and direct source workflow continue through the internal private feed.

The initial boundary is the passive `repository.ai.kodi` descriptor for
kcook-lab-repo and an operator-reviewed update published in its Omega catalog.
The operator accepts that trusted public content can change after review and
that Kodi can enable a service before installed-byte verification. Keep only
local fixtures in the disposable profile.

## Operator approval

Fetch and review the installer ZIP, target ZIP and exact Omega addons.xml on
the host. Verify published checksums and runner compatibility. Place the
three files in a dedicated directory and use the site's normal Compose
command, project and environment file.

The original repository uses `catalogs/omega/addons.xml` and
`catalogs/omega/addons.xml.sha256` under
`https://kcook98765.github.io/kcook-lab-repo/`. Its descriptor names
`artwork/omega/` for catalog art and `packages/` for add-on archives. The
target archive and its `.sha256` sidecar are both under
`packages/script.tvguide.fullscreen/`. The installer ZIP is at the site root.

```sh
docker compose -f compose.release.yaml --profile lab run --rm -T --no-deps --pull never \
  --user 1000:1000 --volume /path/to/reviewed:/reviewed:ro \
  --entrypoint kodi-lab-public-repository broker approve \
  /reviewed/repository.ai.kodi-0.1.1.zip \
  /reviewed/script.tvguide.fullscreen-0.1.2.zip /reviewed/addons.xml
```

The command checks passive extensions, all descriptor endpoint roles, the
target manifest in the catalog and the three byte identities. Use the same
command without the mount and with `inspect` or `revoke` instead of `approve`.
Stop an active test through Web Managed Kodi before changing or revoking its
approval. The retained approval is reusable for another clean disposable
site with the same reviewed inputs.

## Agent workflow

Use an existing trusted managed client and the normal managed lease. Only
Omega is supported for this candidate.

1. Acquire ownership. Read `managed_public_repository_status` and select
   `repository.ai.kodi` with `managed_public_repository_select`. Selection
   refuses a pre-existing working profile.
2. Prepare and Start with the standard tools. Install the baseline and
   reviewed dependencies through the private feed. Configure fixture data.
3. From Kodi Home or Add-on browser, call `managed_public_install` with the
   approved repository add-on ID, version and digest. The exact passive ZIP
   is staged only in the disposable private feed and installed by Kodi.
4. Call `managed_public_catalog` to refresh Kodi and observe the approved
   public catalog checksum. Call `managed_public_install` again with the
   approved target ID, version and digest, plus explicit service consent
   when required. The update selects the version in the original public
   repository. Read back enabled state, version, origin and installed bytes.
   Required non-system dependencies must already be installed and verified
   against the operator-approved private catalog in this first workflow.
5. Inspect the result and run the local fixtures. Stop and release. Stop
   removes public egress and the disposable feed container. The public
   profile is retained for inspection and requires explicit Reset before
   ordinary Start. Named snapshots are protected from public test save/load.

## Network and trust boundary

The disposable runtime joins an IPv4 Docker internal bridge in isolated
gateway mode. It has no external route or host gateway. The temporary
helper accepts CONNECT for `kcook98765.github.io:443` and verifies the
same clear TLS ClientHello server name. It forwards to a single [published
GitHub Pages IPv4 address](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site#configuring-an-apex-domain).
Ordinary Kodi certificate validation, original URLs and native discovery
stay in effect. The relay accepts only the approved CONNECT and TLS names
and opens connections to the pinned IPv4 address on port 443. The runtime
has no external DNS route. The helper's incoming Kodi control
ports accept only the Docker host gateway on its control interface. The
native runtime remains protected by the existing RPC and signed Bridge
auth. Egress has a 15-minute upper bound and no restart policy.

The approved host is the transport boundary. The relay cannot inspect
encrypted HTTP paths or authority headers on the shared Pages front end.
It cannot attest exact content before Kodi processes it. An owner can use
the private feed workflow when pre-install archive-byte control is required.

The candidate uses the Docker isolated IPv4 gateway mode and its standard
bridge for this temporary helper. This deployment arrangement is qualified
only where validated on Linux. Normal Stop, explicit recovery and expiry
remove access without giving the runtime a broader network attached for
fallback.
