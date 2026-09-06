# Maintainer notes

This plugin is already listed and approved on the Omarchy marketplace. A new git tag or a push to the default branch does **not** re-verify the listing. Marketplace verification is bound to one exact commit. After that commit moves, the listing shows **Snapshot verified** plus **Update unverified** until a new `[Verify]` issue is filed and approved.

Do not reopen the original `[Plugin]` submission. File a new `[Verify]` issue.

## Listing

- Plugin ID: `gladimdim.config-sync`
- Display name: Omarchy Config Sync
- Repository: https://github.com/gladimdim/omarchy-config-sync-plugin
- Default branch: `main`
- Listing: https://plugins.omarchy.org/plugin.html?id=gladimdim.config-sync

## After every published release

Once the release commit is on `origin/main` (tag pushed, GitHub release published, or `main` fast-forwarded):

1. Confirm `gh` is authenticated as `gladimdim`.
2. `git fetch origin` and take the full 40-character SHA from `git rev-parse origin/main`.
3. Compare that SHA with the live catalog. Skip if `listingValidatedCommit` already equals HEAD.
4. Skip if an open `[Verify]` issue already targets this plugin ID and the same SHA.
5. Otherwise create the issue with the script below. Headings, order, and checkbox text must match exactly so the marketplace bot starts validation.
6. Report the issue URL. Do not wait for marketplace maintainer approval unless asked.

```bash
git fetch origin
SHA=$(git rev-parse origin/main)
VERSION=$(python3 -c "import json; print(json.load(open('manifest.json'))['version'])")

curl -sS https://plugins.omarchy.org/catalog.json | python3 -c "
import json, sys
want, head = 'gladimdim.config-sync', sys.argv[1]
for p in json.load(sys.stdin)['plugins']:
    if p.get('id') == want:
        listed = p.get('listingValidatedCommit') or ''
        print('listed  ', listed)
        print('observed', p.get('upstreamObservedCommit'))
        print('coverage', p.get('verificationCoverage'))
        print('head    ', head)
        raise SystemExit(0 if listed == head else 1)
raise SystemExit(2)
" "$SHA" && echo "Listing already matches HEAD; no verify issue needed." && exit 0

gh search issues --repo omacom/omarchy-plugin-marketplace --state open \
  "gladimdim.config-sync [Verify]"

cat > /tmp/omarchy-plugin-verify.md <<EOF
### Verification action

Verify and publish a newer upstream commit

### Plugin ID

gladimdim.config-sync

### Repository URL

https://github.com/gladimdim/omarchy-config-sync-plugin

### Target commit

${SHA}

### Verification acknowledgment

- [x] I understand that only the exact target commit can become a verified marketplace snapshot and that verification is not a security audit.

### Standard installation acknowledgment

- [x] I confirm that this listed root plugin supports the standard Omarchy installation path and does not require manual setup.
EOF

gh issue create \
  --repo omacom/omarchy-plugin-marketplace \
  --title "[Verify]: Omarchy Config Sync v${VERSION}" \
  --body-file /tmp/omarchy-plugin-verify.md
```

Form and policy: [plugin verification form](https://github.com/omacom/omarchy-plugin-marketplace/issues/new?template=verify-plugin.yml), [VERIFICATION.md](https://github.com/omacom/omarchy-plugin-marketplace/blob/main/VERIFICATION.md). Verification is an exact-commit check, not a security audit. The existing listing stays unchanged until a maintainer applies `approved-and-verified`.
