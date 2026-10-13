# Security policy

## What to report privately

A vulnerability could let someone bypass target permissions, access credentials
or private data, tamper with reviewed runtime acquisition, or control Kodi or
the host without authorization. Ordinary installation problems, feature requests
and other bugs belong in [GitHub Issues](https://github.com/kcook98765/kodi-lab/issues).

Do not publish exploit details or sensitive diagnostics in an issue or pull request.
Never include passwords, bearer tokens, API keys, Bridge credentials, pairing
codes, private keys or database secrets. Logs, screenshots and configuration
files can also reveal private information; review and redact them before sharing.

## Reporting a vulnerability

Open [Security Advisories](https://github.com/kcook98765/kodi-lab/security/advisories).
Choose **Report a vulnerability** to submit a private report. GitHub Private
Vulnerability Reporting is enabled for this repository.

If that option is unavailable to you, open an issue containing only a request
for a private security contact, without vulnerability details or attachments.
Wait for a verified private channel before sending sensitive information.

Include the Kodi Lab version or commit,
relevant Kodi version and managed/external target type, a minimal reproduction,
and the impact you observed. Use sanitized examples and avoid testing against
systems you do not own or have permission to assess.

## Supported versions

Kodi Lab is a prerelease development/testing project. Security fixes focus on
the current `main` branch; there is no maintained long-term-support branch or
guaranteed support period. Reports about older versions are still useful:
include the exact revision so the maintainer can assess whether current code is
affected. No response or remediation deadline is promised.

Reviewed Kodi runtime availability is not a promise of upstream security support
for each Kodi version. Problems in Kodi or another upstream dependency may need
to be reported to that project's security team as well.

For deployment boundaries, see [broker security](docs/BROKER-SECURITY.md) and
[Web security](docs/PHASE4-WEB-SECURITY.md).
