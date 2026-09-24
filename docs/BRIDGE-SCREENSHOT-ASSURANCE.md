# Bridge transient screenshot assurance (1.1.10 candidate)

The prior 1.1.9 local candidate had ZIP SHA-256
`7fcfcd8060fd6dc6ae3face5507583fabb68726145e2adaefab6568a4dff9156`,
semantic fingerprint
`05acf9f8dcee50727774d12a6b56cc8452faa4b5325045d4722cb8863f495824`,
and `narrow_bridge.py` SHA-256
`51f978bbaf1548546d5be7b521e4feb0ce21cf964c7ee1fc5d8f91dcbd3f97ed`.
It was independently audited but not published, registered, or installed as a
production build. The ACL enforcement changes bridge bytes and the semantic
behavior. Version 1.1.10 assigns a new version-bearing semantic fingerprint,
source hash, and ZIP digest rather than reusing any 1.1.9 reviewed identity.
The 1.1.10 candidate remains local and uninstalled.

Local 1.1.10 candidate identity: ZIP SHA-256
`e43327082373fe82f868dbad37239f1fad5ed6f54367a7ba786af738f4639d40`,
semantic fingerprint
`d1ac487ac17334882b7a2b94c2b7a6d552ae5b826f948e335683315906fd9c6f`,
and `narrow_bridge.py` SHA-256
`c67b9bfc87544d1ab81837fa1dc457eaabaa93703da83ca70c88433d9a3a275f`.

The bridge accepts no screenshot filename from the client. It generates a UUID
name below `special://temp/kodi-lab-bridge`, confirms that name is absent, asks
Kodi to write it with `TakeScreenshot(...,sync)`, waits at most two seconds, and
deletes the candidate before returning pixels. The file reader accepts only a
strict bridge capture basename directly below the translated root. The root
must be a real directory contained under Kodi's translated temporary directory.

## Previous 1.1.8 assurance and its platform sensitivity

| Check | Purpose | Classification |
| --- | --- | --- |
| Translated root, lexical and real containment, fixed random basename | Path escape and malicious client path input | Portable, essential |
| Root and candidate `lstat`, symlink/reparse rejection | Link substitution | Portable with Windows attributes, essential |
| Candidate absent before Kodi call | Pre-existing candidate | Portable, essential |
| Candidate `st_nlink == 1` | Hard-link substitution | POSIX-oriented; essential on POSIX, invalid as a universal Windows rule |
| `lstat` versus `fstat` device/inode | Replacement between stat and open | Essential when identity is available; Windows-sensitive when fields are zero |
| `O_NOFOLLOW` | Final-component symlink race | POSIX-oriented, essential there; absent from Windows `os.open` |
| Open-handle regular-file check | Nonregular file and path-to-handle substitution | Portable, essential |
| Pre-size bound and bounded `os.read` | Oversized file | Portable, essential |
| Exact size/read match | Short or incomplete read | Portable, essential |
| PNG signature, chunk/CRC/IHDR/zlib/filter checks | Malformed or incomplete PNG | Portable, essential |
| `finally` unlink, fail if unlink fails | No successful response with lingering candidate | Portable, essential |
| Two-second poll | Asynchronous Kodi completion | Portable, essential for supported Kodi lines |

The old path-level size check is useful for early rejection but redundant with
the opened-handle size and bounded read for content safety. A path `lstat` still
checks the candidate before opening. The bytes and final size now come from one
open handle. A second `fstat` and path check detect observable replacement or
mutation. Identity comparisons are conditional on metadata availability on
Windows; POSIX keeps the strict before/opened identity and single-link checks.

## Platform adapters and trust boundary

`ScreenshotFileAssurance` selects POSIX or Windows at runtime. POSIX uses
`O_NOFOLLOW`, regular-file mode, link count one, stable nonzero `(st_dev,
st_ino)`, size consistency, and a private root owned by Kodi's effective user.
This naturally covers Linux, LibreELEC/CoreELEC, macOS, and Android when their
Kodi Python runtime offers these primitives. Other POSIX ports lacking
`O_NOFOLLOW` fail closed for screenshot reading.

Windows uses `CreateFileW` with `FILE_FLAG_OPEN_REPARSE_POINT`, then transfers
that *same handle* to Python with `msvcrt.open_osfhandle`. Thus a final-component
reparse point is opened as a reparse point rather than followed. The adapter
rejects observed reparse attributes and any observed link count above one.
`GetFileInformationByHandle` adds handle attributes and link count where the
filesystem supplies them. The read, size checks, and PNG validation use the
opened descriptor. Path and handle identity are compared only when both have
usable nonzero device and file identifiers. A zero link count or identifier is
unknown metadata, not proof of a bad file.

Windows now creates `special://temp/kodi-lab-bridge` with a protected DACL
containing full-access ACEs only for the current Kodi account and SYSTEM. The
ACEs inherit to Kodi-created candidate files. The bridge then opens the child
directory without following a reparse point and verifies its owner is the
current account, its DACL is protected, its ACEs grant the account full access,
and no other principal has an ACE. An existing child is verified, not silently
repaired. An unknown owner, ACL, file type, or security API result fails closed.
The parent Kodi temp ACL is not changed. A read/write sharing handle to the
verified child is held without delete sharing throughout capture and readback,
so another user with parent `DELETE_CHILD` rights cannot replace the child while
the candidate is used. No elevation or external Python package is needed.

Root privacy is required for both Windows modes. Identity metadata compares
the observations that exist, but even full path/handle identity cannot prove
which user wrote the candidate before the first observation if the root is
shared. When identity or link metadata is incomplete, the verified private
root, unpredictable absent name, no-follow open, bounded one-handle read, PNG
validation, and cleanup provide the fallback assurance. `mkdir(..., 0o700)`
alone remains insufficient on Windows Python before 3.13.

There is no useful defense against a process already able to act as the Kodi
user and modify the bridge temp directory between Kodi's path-based request
and the first observation. Such a process can inject an ordinary PNG under the
candidate name; no `stat` field proves which process created it. The bridge
also cannot defend against a compromised Kodi process running its own add-on.
The trust boundary is the Kodi process and its private user data, excluding
other local users and remote bridge clients. This is why the random filename
and private root are both necessary. A different-user race cannot reach a
properly protected root; a same-user race remains a residual risk of Kodi's
path-only screenshot API.

Malformed Kodi image bytes, short reads, changed files, reparse points, and
known hard links fail closed. Cleanup failure prevents a successful pixel
response. Root ACL uncertainty returns the existing `screenshot_capture_failed`
status with fixed internal reason `privacy_query_failed`; an observed nonprivate
root uses `root_not_private`. Successful Windows reads log only
`windows_full_identity` or `windows_private_root_fallback`; unavailable
`GetFileInformationByHandle` logs `windows_metadata_api_failed`, and ACL
rejection/query failure logs `windows_privacy_failed`. No path, SID, account,
exception, ACL dump, or image bytes enter these diagnostics. The authenticated
external screenshot protocol and status vocabulary are unchanged.

## Compatibility and validation

Kodi 19 onward uses Python 3; Kodi's published add-on baseline is Python 3.8
or later. Python documents `st_mode` as file type, `st_ino` as a Windows file
index when nonzero, and Windows `st_file_attributes`/`st_reparse_tag`. Windows
`DirEntry.stat()` zeroes `st_dev`, `st_ino`, and `st_nlink`; the bridge uses
`os.lstat` and `os.fstat` instead, but still accepts unavailable values. In
Python 3.8, Windows `stat` gained broader reparse handling and special-file
mode classification. No timestamp is used as identity.

| Windows metadata source | Usable assurance | Limitation |
| --- | --- | --- |
| `os.stat(path)` | File type/size and nonzero device/index/link count for the resolved path | Follows name-surrogate reparse points by default, so it cannot prove the named candidate was opened |
| `os.lstat(path)` | Pre-open type and reparse attributes of the named candidate; nonzero device/index/link count when supplied | A path observation can race the subsequent open; zero identity fields are unavailable metadata |
| `os.fstat(fd)` | Type, size and nonzero identity/link fields of the opened object | A handle observation cannot alone prove what the path referred to before opening; zero fields are unavailable metadata |
| `GetFileInformationByHandle` | Reparse attribute and link count from the opened Windows handle | Some volumes return partial information or failure; it is supplemental to no-follow opening |

The direct path-versus-handle identity comparison is used only when both
sides provide nonzero device and file identifiers. It catches observable
replacement, including a race between `lstat` and open. When either side is
zero, the adapter relies on the no-follow handle and private random candidate
instead of reporting an identity mismatch. `st_nlink == 0` is likewise not a
link count of zero. Known counts above one still reject hard links.

Linux tests exercise real POSIX files and deterministic Windows metadata and
ACL-decision semantics. This repository has no Windows CI, Windows Kodi
fixture, or native Windows unit-test runner. Native Windows Kodi 19–22
validation of the ctypes wrappers, ACL creation and verification, zero-metadata
fallback, delete-sharing guard, and reparse behavior remains required before
promoting the 1.1.10 candidate to a reviewed production build. The
previously successful Kodi 19.5 Linux production-profile screenshot is
historical evidence for 1.1.8 only; this slice does not repeat it.

References: [Python `os.stat_result`](https://docs.python.org/3.12/library/os.html#os.stat_result),
[Python Windows `mkdir`](https://docs.python.org/3.13/library/os.html#os.mkdir),
[Microsoft `CreateFileW`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
[Microsoft `GetFileInformationByHandle`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfileinformationbyhandle),
[Microsoft `CreateDirectoryW`](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createdirectoryw),
[Microsoft `GetSecurityInfo`](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo),
[Microsoft security descriptor control](https://learn.microsoft.com/en-us/windows/win32/secauthz/security-descriptor-control),
[Microsoft `GetAce`](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getace),
[Kodi Python libraries](https://kodi.wiki/view/Python_libraries), and
[Kodi special paths](https://kodi.wiki/view/Special_protocol).
