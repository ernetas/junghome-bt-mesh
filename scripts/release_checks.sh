#!/usr/bin/env bash
# The checks release.yml runs on a vX.Y.Z tag before it publishes anything, and the release notes it publishes. Kept
# here rather than inline in the workflow so they run on every push too (tests/test_release_checks.py, against
# fixture trees) instead of only on a tag, where a mistake in them would first show up while releasing.
#
#   scripts/release_checks.sh on-main      the tagged commit ($GITHUB_SHA) is on origin/main
#   scripts/release_checks.sh changelog    a plain X.Y.Z tag has a dated "## X.Y.Z" heading in CHANGELOG.md
#   scripts/release_checks.sh versions     the tag matches manifest.json and pyproject.toml (one version for both)
#   scripts/release_checks.sh notes FILE   writes the version's CHANGELOG.md section to FILE (the release body)
#
# The tag comes from $GITHUB_REF_NAME (vX.Y.Z, or vX.Y.Zb1 / vX.Y.Zrc1 / vX.Y.Z-rc1 for a pre-release); the files are
# read from the current directory, the root of the tagged checkout. A failed check prints a GitHub `::error::` line
# and exits 1.
set -euo pipefail

ref="${GITHUB_REF_NAME:?GITHUB_REF_NAME must name the tag (vX.Y.Z)}"
tag="${ref#v}"

# A plain X.Y.Z tag is a release; anything with a suffix is a pre-release (release.yml marks it as such).
is_release() {
	printf '%s' "$tag" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'
}

check_on_main() {
	# A tag on a feature branch would publish code main never had.
	local sha="${GITHUB_SHA:?GITHUB_SHA must name the tagged commit}"
	if ! git merge-base --is-ancestor "$sha" origin/main; then
		echo "::error::The tagged commit ($sha) is not on main: merge first, then tag the merged commit."
		exit 1
	fi
	echo "$sha is on main."
}

check_changelog() {
	# A pre-release is cut while the version's section is still "(unreleased)": only a plain X.Y.Z tag has to find its
	# heading dated.
	if ! is_release; then
		echo "Pre-release version ($tag): the CHANGELOG.md heading is not checked."
		return
	fi
	local heading
	heading="$(grep -E "^## ${tag//./\\.}( |$)" CHANGELOG.md || true)"
	if [ -z "$heading" ]; then
		echo "::error file=CHANGELOG.md::No \"## $tag\" heading in CHANGELOG.md."
		exit 1
	fi
	if printf '%s' "$heading" | grep -qi 'unreleased'; then
		echo "::error file=CHANGELOG.md::CHANGELOG.md still calls $tag unreleased: \"$heading\"."
		exit 1
	fi
	echo "$heading"
}

check_versions() {
	local manifest library
	manifest="$(python3 -c "import json; print(json.load(open('custom_components/junghome_ble/manifest.json'))['version'])")"
	library="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")"
	echo "tag=$tag manifest=$manifest pyproject=$library"
	if [ "$tag" != "$manifest" ]; then
		echo "::error::Tag ($tag) does not match custom_components/junghome_ble/manifest.json version ($manifest)."
		exit 1
	fi
	if [ "$tag" != "$library" ]; then
		echo "::error::Tag ($tag) does not match the jhmesh version in pyproject.toml ($library)."
		exit 1
	fi
}

write_notes() {
	# The version's own CHANGELOG.md section (its heading line excluded, up to the next `## ` heading) rather than
	# GitHub's list of merged pull requests: the upgrade steps and the user-visible changes are written there, and HACS
	# shows the release body in its update dialog. A pre-release (X.Y.Zb1) takes the section of the version it
	# previews, still "(unreleased)"; without one it says so instead of publishing an empty body.
	local out="${1:?usage: release_checks.sh notes FILE}"
	local version
	version="$(printf '%s' "$tag" | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+' || true)"
	if [ -z "$version" ]; then
		echo "::error::Tag $ref does not start with a vX.Y.Z version."
		exit 1
	fi
	awk -v heading="## $version" '
		found && /^## / { exit }
		found { print; next }
		index($0, heading) == 1 && (length($0) == length(heading) || substr($0, length(heading) + 1, 1) == " ") {
			found = 1
		}
	' CHANGELOG.md >"$out"
	if ! grep -q '[^[:space:]]' "$out"; then
		if [ "$version" = "$tag" ]; then
			echo "::error file=CHANGELOG.md::The \"## $version\" section of CHANGELOG.md is empty."
			exit 1
		fi
		echo "::warning file=CHANGELOG.md::No \"## $version\" section in CHANGELOG.md for $ref."
		echo "Pre-release of $version: CHANGELOG.md has no section for it yet." >"$out"
	fi
	cat "$out"
}

case "${1:-}" in
on-main) check_on_main ;;
changelog) check_changelog ;;
versions) check_versions ;;
notes) write_notes "${2:-}" ;;
*)
	echo "usage: $0 on-main|changelog|versions|notes FILE" >&2
	exit 2
	;;
esac
