# Agent-directed add-on installation on managed Kodi

Use `project_create` or `project_import`, then exact-generation
`workspace_validate` → `workspace_deploy` → `workspace_launch` for unattended
managed development. This existing controlled source-deployment path needs no
human Kodi installation action or per-version Web publication approval. It
remains the rapid edit/test/debug loop for Matrix 19.4, Nexus 20.5, Omega 21.3
and the explicitly approved Piers 22.0 RC1 runtime.

**Repository-backed unattended installation is unsupported.** Investigation of
[#339](https://github.com/kcook98765/kodi-lab/issues/339) found that the documented
`InstallAddon(id)` cannot satisfy noninteractive, exact-reviewed-package
installation or controlled upgrades on these versions. No repository installer,
automatic development-repository provisioning, trusted-agent feed publication,
new Bridge command or dependency downloader was added. Source deployment and
native repository installation are separate claims.

The [native feasibility report](MANAGED-ADDON-INSTALLATION-QUALIFICATION.md)
records real four-version observations, fixture boundaries and preservation.

## Supported unattended workflow

An administrator first creates the agent's credential and explicitly grants
**Trusted Managed Lab**. The four approved managed versions must already be
installed/prepared; the role does not acquire runtimes. No per-target grants or
human development selection are required for an owned project. External Kodi
retains its independent authority model.

1. Discover versions with `managed_status({})`. Choose a managed target explicitly.
   Start one version with `managed_start` and a saved request UUID; wait for
   `native_ready`. Observe the one-running policy and human input priority.
2. Create an owned inert Program or video plugin with `project_create`, or import
   a reviewed source ZIP with `project_import`. Use a distinct add-on ID. Retain
   the request UUID, project/workspace identity, and import SHA-256/provenance.
   Inline import accepts at most 45 KiB compressed; use `project_transfer` then
   `project_import` for up to 4 MiB compressed, 1,024 entries and 16 MiB expanded;
   [the source intake rules](AGENT-PROJECTS.md) apply.
3. Wait for `workspace_status` to report the captured generation. Use
   `workspace_read`, `workspace_tree` and generation-bound `workspace_edit` as
   needed. Save each resulting generation; never guess or increment one locally.
4. Validate that generation with `workspace_validate`, then inspect durable
   prerequisites with `workspace_deploy_preflight`. Validation is structural,
   not execution or a promise that all dependencies are available. Preflight
   does not reserve the target or establish native readiness.
5. Deploy to the explicit `target_id` using the same generation and a new saved
   request UUID. `deployment_mode: "direct"` makes the intended transport clear:

   ```json
   {
     "request_id": "<saved deployment UUID>",
     "workspace_id": "<owned workspace UUID>",
     "expected_generation": 0,
     "target_id": "<selected managed target UUID>",
     "deployment_mode": "direct"
   }
   ```

   Replace `0` with the actual observed generation. Retain the operation receipt,
   exact source/tree evidence and target/runtime identity. Deployment checks
   installed ownership, compatibility, dependency state and the exact copied
   tree; newly discovered eligible owned Program/video add-ons are enabled
   under the original lease. Unknown existing bytes or a conflicting ID are
   refused. A `verified_existing` receipt verifies reuse without execution.
6. Select the manifest's validated `extension_ordinal` and call `workspace_launch`
   with that generation, target and another saved request UUID. Inspect
   `inspect_ui_state`, `screenshot_capture`, `log_read` and the execution/launch
   receipt as appropriate. Dispatch acceptance does not prove functional success.
7. Edit, validate and deploy the next generation to update. A version bump alone
   is not byte identity. Reuse the original request after a lost response; never
   replace an uncertain request with a new UUID. For a different Kodi version,
   stop the current version, start the selected version, then deploy/verify there.
   Stop/Start retains the profile. An explicitly authorized Reset replaces it
   and needs fresh deployment; [Reset consent](MANAGED-AGENT-RESET.md) is unchanged.

The [agent-owned project contract](AGENT-PROJECTS.md) defines ownership, replay,
first-copy protection, enablement and stopped-tree recovery. Existing native
managed qualification is recorded separately for
[Nexus/Omega](AGENT-FIRST-QUALIFICATION.md) and
[Matrix/Piers](MATRIX-PIERS-AGENT-QUALIFICATION.md). Those historical results do
not qualify a repository installer.

## Why installation by ID is unsuitable

The exact tagged sources below all call `InstallModal` with prompting enabled,
pass only the add-on ID and discard the installer's boolean result. `return 0`
from this builtin, or Python's `None`, is not an installation receipt.

| Runtime | Builtin source | Installer source | Source decision |
| --- | --- | --- | --- |
| Matrix 19.4 | [AddonBuiltins.cpp](https://github.com/xbmc/xbmc/blob/19.4-Matrix/xbmc/interfaces/builtins/AddonBuiltins.cpp) | [AddonInstaller.cpp](https://github.com/xbmc/xbmc/blob/19.4-Matrix/xbmc/addons/AddonInstaller.cpp) | Confirmation required; no exact-package input |
| Nexus 20.5 | [AddonBuiltins.cpp](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/interfaces/builtins/AddonBuiltins.cpp) | [AddonInstaller.cpp](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/addons/AddonInstaller.cpp) | Same limitation |
| Omega 21.3 | [AddonBuiltins.cpp](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/builtins/AddonBuiltins.cpp) | [AddonInstaller.cpp](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/addons/AddonInstaller.cpp) | Same limitation |
| Approved Piers 22.0 RC1 | [AddonBuiltins.cpp](https://github.com/xbmc/xbmc/blob/22.0rc1-Piers/xbmc/interfaces/builtins/AddonBuiltins.cpp) | [AddonInstaller.cpp](https://github.com/xbmc/xbmc/blob/22.0rc1-Piers/xbmc/addons/AddonInstaller.cpp) | Same limitation; preview scope only |

The [public function list](https://kodi.wiki/view/List_of_built-in_functions)
describes installation by ID, without promising silent selection or completion.
The implementation makes the following additional limits explicit:

- **Refresh:** `UpdateAddonRepos` queues metadata-update jobs for all enabled
  repositories. It supplies no exact-repository argument or completion receipt.
  [RepositoryUpdater](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/addons/RepositoryUpdater.cpp)
  verifies catalog state and updates the native database asynchronously. Its job
  completion can also initiate automatic add-on updates when that policy is on.
  A forced refresh is therefore not intrinsically a read-only operation.
- **Completion:** after confirmation, `InstallModal` uses a foreground modal
  install job. Blocking Python dispatch waits for the modal call; the native
  probes stayed pending until cancellation. Default nonblocking Python dispatch
  can return earlier. Neither mode returns the installer boolean. A dispatch
  or catalog lookup therefore cannot establish download, installation, enabled
  state or package identity.
- **Selection:** the builtin has no repository/version/digest parameters.
  [AddonRepos](https://github.com/xbmc/xbmc/blob/22.0rc1-Piers/xbmc/addons/AddonRepos.cpp)
  chooses compatible candidates using official/private repository policy and
  latest versions. A higher private version can win for a private ID; official
  candidates normally take priority unless the configured policy allows another
  repository. An equal-version tie is not a reviewed-package guarantee.
- **Dependencies:** native installation checks declarations and recursively
  resolves available dependencies. Official candidates normally take priority;
  otherwise resolution uses the parent's repository, with policy-dependent
  private alternatives for official dependencies. These version constraints
  do not bind a reviewed dependency closure or digests.
- **Updates:** `InstallModal` returns early when the ID is already installed,
  including disabled add-ons. The builtin is not an exact-version upgrade or
  enable operation. An internal C++ `Install(id, version, repoId)` overload
  exists, but is not a documented Python/builtin/JSON-RPC endpoint and still
  does not accept the caller's expected package SHA-256.
- **Integrity:** Kodi can validate catalog/package hash sidecars from the selected
  repository. This protects that selection's download; it does not prove the
  agent selected the owner's reviewed artifact. Post-install exact-tree checks
  are necessary but cannot prevent unintended code from installing/running first.
- **Consent:** the Add-on Browser menu lock, initial Yes/No confirmation,
  dependency errors and type-specific behaviors may require interaction.
  Unknown-sources consent belongs to the separate ZIP workflow. No prompt or
  security setting may be silently bypassed to manufacture unattended success.

Each version's reviewed JSON-RPC method schema lacks `Addons.Install` or an
exact-package installation endpoint. `Addons.GetAddonDetails`/`GetAddons` and
`SetAddonEnabled` support observation and enablement, not package acquisition.
The [native ZIP feasibility decision](ONE-CALL-NATIVE-ZIP-FEASIBILITY.md) remains
separate: passing a path to `InstallFromZip` does not implement a ZIP installer.

## Repository provisioning and publication decision

The existing repository implementation already supplies separate stable and
`repository.kodi-lab-dev` add-ons, deterministic bootstrap ZIPs, immutable
artifacts, atomic catalogs and hash sidecars at the approved configured origin.
Its descriptor format admits Kodi 19 and later. Development publication currently
admits reviewed Python video plugins for Matrix/Nexus/Omega with only the
compatible bundled `xbmc.python` dependency. Piers publication is explicitly
unsupported by its generation allowlist; descriptor parsing on Piers does not
change that admission rule.

Provisioning an exact descriptor during stopped managed Create/Reset is
technically possible through a separately reviewed closed installer, alongside
Bridge rather than inside it. It would require exact reviewed descriptor/tree
and origin evidence, native enabled-state verification, manual-update policy,
retained Stop/Start proof and Reset restoration receipts. Provisioning alone
would not remove the install prompt or pin package selection. Enabling a shared
feed also creates future catalog/update authority. Because #339's safe consumer
is absent, this campaign leaves Create/Reset provisioning and publication policy
unchanged instead of installing an unused repository on retained profiles.

Trusted-agent publication is not added to the human-approved shared feed.
An eventual implementation needs owned-project namespace/conflict protection,
current credential/role checks, immutable package/generation/digest receipts and
replay/recovery, plus a separately supported consumer that pins the complete
package/dependency selection before native execution. Exact readback after an
unrestricted ID install cannot supply that missing pre-install protection.
No second publication service or generic Narrow Bridge interface is justified.

## Dependency and package limits

Editable source intake accepts an eligible Python Program/plugin or a passive
reviewed `script.module.*` project with resources. Consumers can require up to
three separately imported, validated and deployed modules owned by the same agent.
Modules themselves may require only the compatible bundled Python API; recursive
graphs/cycles, optional modules, native binaries, services, skins and repository
downloads are outside this slice. See the [module workflow](AGENT-PROJECTS.md#separately-reviewed-python-module-dependencies).

Deploy the modules first, then the main add-on. Existing deployment, launch and
smoke operations check native dependency state and exact module deployment evidence.
Each module retains its independent generation/digest/target/container receipt;
this is explicit composition, not an atomic dependency installer. Unknown installed
files or modules, incompatible/disabled dependencies, ownership conflicts and
uncertain receipts are refused. Registered-source/external policy is unchanged.
Repository publication and original-ZIP testing retain their narrower admission.

`package_import` / `package_install` retain the separate original-ZIP testing
workflow: owner-scoped immutable intake, expiring exact-target delivery and
independent installed-payload verification, with Kodi GUI consumption. They
are not the unattended source-development route.
