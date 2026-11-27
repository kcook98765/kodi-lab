# Private managed Kodi recordings

An authorized agent with the Trusted Managed Lab role can record the private Kodi display. The Linux application bundle includes FFmpeg. The broker starts a short-lived
encoder from its exact packaged image for each recording. That worker has a
private network namespace, an explicit resource budget and one writable output
file. Recording uses the existing 640×360 Xvfb display in managed Kodi and leaves the runtime image and profile pin unchanged. It produces a silent H.264 MP4. An issuing MCP credential is sufficient for recording.

## Record and retrieve

Discover the tools with the authorized MCP credential. Prepare and start the intended managed generation before recording. The recording API uses the same managed profile read authority as `managed_gui`.

| MCP tool | Arguments | Result |
| --- | --- | --- |
| `managed_recording_start` | Optional `generation`, `fps` (`"2"` or `"5"`), `seconds` (1–1800, as a decimal string) | A recording ID, exact runtime incarnation digest and state. The default is 2 FPS with a five-minute limit. |
| `managed_recording_status` | Optional `recording` | Starting, recording, finalizing, ready or failed. Recording status includes a UTC `started_at` anchor for the first captured frame. |
| `managed_recording_stop` | `recording` | Requests finalization. Poll status for the final duration, size, stop reason and SHA-256. |
| `managed_recording_delete` | `recording` | Retires a finalized server copy. Verify the private download first if you want to keep it. |

Use the issuing credential for all operations, including retrieval. The Toolkit `video download` command streams the bytes into a private local MP4 and verifies the checksum and stream metadata. Clients implementing their own transfer can send an authenticated empty POST to the same `/mcp` endpoint with `Content-Type: application/vnd.kodi-lab.recording` and `X-Kodi-Lab-Recording: RECORDING_ID`. A successful response is a private `video/mp4` attachment with a fixed content length. The MCP tool result carries metadata only.

If a start response is interrupted, query status without an ID to recover the most recent recording from the issuing credential while the broker is still running.

There is one active recorder and one artifact transfer per Lab. Each segment uses at most 30 minutes and 64 MiB. The broker keeps up to four finalized segments for up to 24 hours and protects a 2 GiB free-disk reserve. Retrieve and, if appropriate, delete completed segments before starting more. A broker restart retires interrupted work and preserves committed artifacts until their retention expires. Store downloaded originals outside the add-on worktree.

## Authority, lifecycle and troubleshooting

Recording reads display pixels independently of the agent lease and human input. Agent input still uses ordinary managed acquisition and navigation. A passive Web viewer may be open during capture. A credential expiry, revocation, client disablement or Trusted Managed Lab role loss ends capture after a short lease. Export also checks current authority and the exact issuing credential.

Stop capture before any private settings, authentication prompt or other sensitive screen. The Kodi display is recorded as shown. Review the entire artifact before sharing it. Toolkit commentary and composition take place locally.

A Kodi Stop, restart or profile replacement ends the segment. Inspect terminal status and verify any partial duration. Begin another segment after checking the new managed instance, and keep its ID, incarnation and UTC start separately. A stop requested too early, an unavailable display, a full recorder or encoder failure can leave no playable artifact. Wait for finalization and check the reported error before retrying.

Linux runtime qualification is recorded with the application release. Windows/WSL2 remains a separate qualification lane.
