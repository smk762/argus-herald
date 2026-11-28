# Privacy gate

Every draft passes a privacy gate on approve and again on publish. A hit refuses the draft; text is never rewritten. It catches:

- private and link-local IPs, home and mount paths, email addresses (except `example.*`, `*.invalid` and GitHub noreply), and LAN hostnames (`.lan`, `.local`, `.internal`, …);
- crypto addresses (EVM, Base58Check with a valid checksum, bech32, ENS names), except those in `privacy.allow_addresses`;
- timezone offsets attached to a timestamp, as in a pasted log or `git log` line (`+00:00` and `Z` are fine);
- any term in your private deny-list, and any regex in `privacy.patterns`.

The deny-list is a file outside the repository, one term per line (a line starting with `#` is a comment, as is whitespace then `#` after a term; write a hashtag term as `\#tag`; `name#tag` denies both `name#tag` and `name`): `~/.config/argus-herald/opsec-denylist.txt` by default, or the path in `ARGUS_HERALD_OPSEC_DENYLIST`. Keep it `chmod 600`. If [ai-code-auditor](https://github.com/smk762/ai-code-auditor)'s list exists, its terms are enforced too. **Without the deny-list nothing can be approved or published.**

```yaml
privacy:
  allow_addresses: ["0x…"]       # public contracts that may appear in posts
  patterns:
    ticket: "INTERNAL-\\d+"
```

The review page highlights each hit. Edit the text, or accept one specific hit for that draft with a note saying why it is safe. From the shell:

```bash
argus-herald check                 # pending and approved drafts
argus-herald accept 12 'crypto-address:0x…' --note "public demo contract"
```
