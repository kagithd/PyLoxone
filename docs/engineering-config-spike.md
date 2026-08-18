# Engineering configuration inventory spike

This experimental path complements `LoxAPP3.json` with the complete engineering
configuration stored by the Miniserver. It is intentionally read-only and
manual while real-world hardware coverage is measured.

## Test flow

1. Home Assistant exposes **Refresh engineering inventory** as a configuration
   button on the Loxone Miniserver device.
2. Pressing it runs blocking FTP work in Home Assistant's executor.
3. The integration lists `/prog`, downloads the newest
   `sps_<version>_<timestamp>.zip`, extracts `sps0.LoxCC`, validates its header,
   size and checksum, and parses the XML.
4. Every attributed XML node is retained in memory with its original identity,
   parent UUID and inherited room/category relationship.
5. A conservative classifier prepares plausible Home Assistant candidates but
   does not create entities.
6. The diagnostics download exposes a sanitized element tree and prepared
   candidates. Arbitrary XML attributes are deliberately excluded because the
   engineering file can contain security-sensitive configuration.

## Safety boundaries

- The Miniserver receives only FTP `LIST` and `RETR` operations.
- No config is uploaded, activated or written back.
- The archive and XML are not persisted by the integration.
- FTP credentials are used only in memory. FTP itself is unencrypted and must
  therefore remain restricted to the trusted local network.
- Archive, compressed payload and expanded XML sizes are bounded before parsing.
- Unknown Loxone types remain visible instead of being silently discarded.

## Decisions deferred until the live test

- exact grouping for Miniserver, extensions, buses, devices, ports and controls;
- which raw hardware endpoints have a readable runtime value and can become HA
  entities;
- user selection storage and stable onboarding state;
- a config-entry table/tree UI versus a dedicated integration panel;
- refresh persistence, change detection and warnings for broken automations.
