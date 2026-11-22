# Source provenance

This release contains a newly written Lua HTTP adapter and startup module, a
Python HTTP bridge, a VPK reader/builder, tests, and documentation.

The project began by examining the behavior and generated addon of VG 0.2.38.
The earlier private prototype included two Lua files extracted from that
installation and a wrapper adapted from it. Those extracted files and that
wrapper are **not included in this public source package**.

The public adapter is newly implemented around metatable-based request objects,
an AHB/1 length-framed protocol, paged response envelopes, and explicit mailbox
acknowledgments. It does not implement VG's wire format and does not load a VG
application, library, or service. The Python components were developed for this
project and updated for this release.

The snapshot-disconnect fix adds a newly written Lua UI sender, JavaScript
receiver, and Panorama resource repacker. At preparation time, the user's own
compiled script resources receive the project's receiver prefix. The public
source package contains only that new prefix and repacking code, not the
Workshop's script contents or modified game assets.

Earlier source code was inspected during the investigation. This is therefore
not represented as a formal clean-room implementation or a legal certification.
Renaming files, writing new source, or selecting a license does not by itself
establish legal clearance in every jurisdiction.

No Workshop VPK, extracted map script, Dota binary, backend response, account
identifier, console log, or captured request is distributed here. Preparation
reads the user's own local Workshop VPK and keeps its original entry point in
the generated local addon so game behavior is preserved. Generated game assets
remain subject to their owners' terms and are outside this package's MIT license.

References used for interfaces and formats:

- [Valve Developer Community: VPK file format](https://developer.valvesoftware.com/wiki/VPK_(file_format))
- [Valve Developer Community: Dota 2 Workshop scripting](https://developer.valvesoftware.com/wiki/Dota_2_Workshop_Tools/Scripting)
- [Python standard library documentation](https://docs.python.org/3/library/)
- [MIT license identifier and text](https://spdx.org/licenses/MIT.html)
