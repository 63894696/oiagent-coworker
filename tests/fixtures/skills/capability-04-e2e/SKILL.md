---
name: capability-04-e2e
version: 0.1.0
description: E2E encryption overlay for IM transports (capability-04); hybrid-KEM simplified ratchet, seal/open only
entrypoint: oiagent_coworker.skills.capability_04_e2e
metadata:
  e2e_overlay: true
  crypto_suite: X25519+ML-KEM-768 / XChaCha20-Poly1305 / HKDF-SHA256 / Ed25519
  primitive_interface: seal/open
  group_mode: sender-keys
---
# Capability-04 E2E Encryption Overlay

This skill mounts the OIagent capability-04 end-to-end encryption
overlay as a SKILL.md-declared module. It sits between the IM transport
(SimpleX / SecureDM / any future transport) and the agent runtime, so
message content is sealed before it ever reaches a relay server.

## What it is

A self-developed simplified ratchet (方案B), NOT libsignal. The design
trades protocol completeness for auditability: the core is roughly 800
lines and intentionally omits the double-ratchet DH step because
transports such as SimpleX already provide one at the queue layer. The
construction is a hybrid-KEM handshake (X25519 + ML-KEM-768) feeding a
one-directional KDF chain (HKDF-SHA256) with a skipped-message cache
for out-of-order delivery. It provides forward secrecy and
post-compromise recovery through chain advancement, at roughly one
tenth of the code size of a full Signal implementation.

## Interface

Two primitives only: ``seal(plaintext, session) -> blob`` and
``open(blob, session) -> plaintext``. Everything else — key storage,
session establishment, replay handling — is internal to the skill.

The wire format is self-describing:

```
[version:1B][suite_id:1B][flags:1B][seq:8B][nonce:24B][AEAD(padded)][tag]
```

The sequence number ``seq`` is transmitted in cleartext so receivers
can advance their KDF chain without trial decryption; everything after
the nonce is inside the XChaCha20-Poly1305 AEAD envelope.

## Threat model — solved

Server and MITM adversaries never see content keys: relays forward
opaque blobs only. The hybrid KEM (X25519 + ML-KEM-768) protects
against harvest-now-decrypt-later attacks by a future quantum
adversary while keeping classical security today. Fixed padding
buckets (256 B / 1 KB / 4 KB / 16 KB) collapse the length side
channel. Identity keys (Ed25519) are portable across IM transports,
so one identity follows the user across SimpleX, SecureDM, and future
transports.

## Threat model — not solved

Transport-level metadata is out of scope: timing correlation, traffic
volume analysis, and IP-level exposure are properties of the relay and
must be handled by the transport (Tor, mix nets, cover traffic). This
skill also does not protect against endpoint compromise — if the
device running the agent is seized while unlocked, the session keys
are exposed.

## Implementation

Rust core exposing a C ABI FFI, with thin Python and Go bindings on
top. The compiled artifact is a ``cdylib`` (``crypto_conduit.dll``,
~240 KB) loaded by the daemon at skill mount time. Test coverage is
26/26 including round-trip, out-of-order, and chain-advancement
vectors. Group chat uses sender keys (one ratchet per sender, shared
with the group), deliberately NOT MLS, to keep the implementation
within the auditability budget.
