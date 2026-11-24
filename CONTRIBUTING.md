# Contributing

Thanks for helping. This project is licensed under AGPL-3.0-or-later
(see `LICENSE` and `LICENSING.md`).

## Ground rules

1. **One logical change per pull request.** A fix and a refactor in the same PR
   cannot be reviewed or reverted independently.
2. **Tests come with behavior changes.** A bug fix needs a test that fails
   before it and passes after it.
3. **No secrets.** Keys, tokens, cookies, certificates, and personal data never
   belong in the repository, in tests, in fixtures, or in logs.
4. **Explain the why.** The diff shows what changed; the description must say
   why it changed and what you considered instead.

## Sign-off (DCO)

Every commit must carry a Developer Certificate of Origin sign-off:

```bash
git commit -s -m "fix: ..."
```

This certifies that you wrote the change or have the right to submit it. There
is no copyright assignment and no separate CLA: contributions are accepted
under the same license as the project (inbound = outbound).

## License compatibility

Do not paste code you cannot license under AGPL-3.0-or-later. Code under
permissive licenses (MIT, BSD, Apache-2.0) may be incorporated with its notice
preserved. Code under GPLv2-only or other AGPL-incompatible terms may not.
When in doubt, open an issue before writing the patch.

## Reporting a vulnerability

Do not open a public issue. Follow `SECURITY.md`.
