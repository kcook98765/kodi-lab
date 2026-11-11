# Trusted managed Reset and Bridge preparation

Trusted Managed Lab is the administrator's one-time consent for autonomous Reset
of idle installed managed profiles, including the native Start/restart/Stop needed
to prepare the compatible reviewed Narrow Bridge. No additional human approval
is requested for each Reset. External Kodi is outside this authority.

Read `managed_status` and save the selected version's `profile_identity`. Send it
as `expected_profile` with `managed_reset`, the version and a saved request UUID.
The binding includes the exact container, runtime revision and image. A stale
binding refuses before replacement. Keep the original binding and request UUID
when replaying; the receipt retains the retired profile identity.

Clean replacement and Bridge preparation remain separate durable workflows.
The Reset result retains native retirement/replacement evidence and includes
`bridge_preparation` with its operation ID, checkpoint, version and stored trust.
Reset `phase=complete` describes replacement only. Check the Bridge checkpoint
separately. Stored trust on a stopped profile does not establish live readiness
or native acceptance. The default final state is stopped; `restart=true` requests
running state after preparation.

Pending or uncertain children are never redispatched on replay. A Bridge failure
keeps the clean replacement and its original preparation receipt for review.
Once existing lifecycle recovery proves the original child terminal and idle,
an agent may explicitly send `resume_bridge_version` from that receipt with the
original Reset request. Recovery keeps both parent identities and the same
replacement profile, and never creates a new Reset. A replay of the completed
recovery returns its retained result without another child.

Current credentials, role and exact target are rechecked at admission, child
claim and dispatch. Capacity, human viewers/control, other actor holds, playback,
pending mutations, reconciliation and native identity guards remain active.
Projects, workspace bytes, other managed versions and unrelated host resources
are preserved by the existing clean Reset contract.

Clients without the role keep their explicitly granted native-only Reset.
Human Web Reset keeps its existing confirmation and separate Bridge action;
consent-aware human composition remains tracked by #302. Fixture/source checks
are separate from the native and browser campaign acceptance in #310.
