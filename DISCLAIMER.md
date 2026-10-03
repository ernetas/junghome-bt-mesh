# Disclaimer & legal notice

## Not affiliated

This is an independent, community project developed by volunteers. It is **not** affiliated with, authorized by,
sponsored by, or endorsed by Albrecht JUNG GmbH & Co. KG ("JUNG") or any of its subsidiaries or partners. All
statements, findings, and code here are those of the project's contributors, not of JUNG.

## Trademarks

"JUNG", "JUNG HOME", "LB Connect", and related names and logos are trademarks or registered trademarks of their
respective owner. They are used in this project **solely descriptively and nominatively** — that is, to identify the
hardware and system this software is designed to interoperate with. No claim of ownership is made, and no affiliation
or endorsement is implied. This project does not use the JUNG name or logo as its own brand, and does not distribute
JUNG's logo or brand artwork.

## Purpose: interoperability

The purpose of this project is **interoperability**: to let the lawful owner of JUNG HOME Bluetooth Mesh devices
operate those devices from third-party software (such as Home Assistant) without being required to use the vendor's
gateway or apps.

The mesh stack in this repository is an **independent implementation** written against the publicly available
Bluetooth SIG Mesh specification and verified against the specification's own test vectors. It is not derived from,
and does not incorporate, the vendor's source code or firmware.

Under European Union law, the acts of loading, running, observing, studying, and testing a computer program to
determine the ideas and principles underlying it, and — where indispensable to obtain interoperability — decompiling
it, are permitted to the lawful user for the purpose of achieving the interoperability of an independently created
program (Directive 2009/24/EC on the legal protection of computer programs, Articles 5(3) and 6). Article 8 provides
that contractual terms purporting to prohibit these acts, to the extent carried out for that purpose, are null and
void. Analogous interoperability provisions exist in other jurisdictions (for example, the reverse-engineering /
interoperability exception in 17 U.S.C. § 1201(f) in the United States).

## No redistribution of vendor materials

This repository does **not** contain and does **not** redistribute:

- the vendor's mobile-app source code, whether original or decompiled;
- any device or gateway firmware image, in whole or in part;
- the vendor's logo, icons, or other brand artwork.

Developer-only inputs that would contain such material or private key material — a decompiled copy of the app and an
iOS/Android app backup holding a network's keys — are kept locally and are excluded from version control via
`.gitignore`. Contributors must not commit them.

## Use only on your own network

A Bluetooth Mesh network can only be controlled by a party holding its network and application keys, which belong to
the network's owner. Use this software **only** with devices and networks that you own or are expressly authorized to
administer. Do not use it to access, interfere with, or control devices or networks belonging to others.

## No warranty; use at your own risk

This software is provided "AS IS", without warranty of any kind, express or implied, as set out in the accompanying
MIT License. Interacting with device firmware and mesh networks carries inherent risk, including misconfiguration,
temporary or permanent loss of device function, and the possibility of voiding a manufacturer's warranty. The
contributors accept no liability for any damage or loss arising from use of this software. **You use it at your own
risk.**

## Not legal advice

This notice is a statement of the project's intent and understanding; it is not legal advice. If you require certainty
about your rights and obligations in your jurisdiction, consult a qualified lawyer.

## Contact

If you are a rights holder and have a concern about anything in this repository, please open an issue or contact the
maintainer so it can be addressed promptly.
