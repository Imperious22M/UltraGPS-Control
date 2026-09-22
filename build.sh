#!/usr/bin/env bash
#
# build.sh — build helper for the UltraGPS Control program.
#
#   ./build.sh deb         Build the installable .deb package
#   ./build.sh clean       Remove the ./build and ./site directories
#   ./build.sh docs        Preview the documentation locally with live reload
#   ./build.sh docs-build  Render the static documentation site into ./site
#
# The 'deb' target is packaged OUTSIDE this directory (in a temp dir under
# /tmp) so that the packaging run leaves no artefacts in the working tree.

set -euo pipefail

# Project root
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
SITE_DIR="$PROJECT_DIR/site"

# The VERSION file is the single source of truth for the package version;
# debian/changelog is generated from it below.
VERSION="$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")"

# Package maintainer for the generated debian/changelog; filled in by
# require_release_metadata() from debian/control.
MAINTAINER=""

usage() {
    cat <<EOF
Usage: ./build.sh <command>

Commands:
  deb          Build the .deb package in an isolated /tmp dir, then copy
               it into ./build
  clean        Remove the ./build and ./site directories
  docs         Preview the documentation locally with live reload
               (mkdocs serve — http://127.0.0.1:8000, Ctrl-C to stop)
  docs-build   Build the static documentation site into ./site

Options:
  -h, --help   Show this help

Examples:
  ./build.sh deb         # ./build/ultragps-control_${VERSION}_all.deb
  ./build.sh docs        # live-preview docs without pushing
  ./build.sh docs-build  # render docs to ./site
  ./build.sh clean       # wipe ./build and ./site

Releasing:
  Bump VERSION, commit, then tag and push to publish a GitHub Release:
      git checkout release
      git tag v${VERSION}
      git push origin v${VERSION}
EOF
}

# Compute $PATH with any activated virtualenv removed.
#
# Debian packaging has to build against the system Python and Debian's own
# python3-* modules.  When ./build.sh runs with the project venv activated, its
# bin directory comes first on PATH, so pybuild invokes .venv/bin/python3.12 --
# which has no 'build' module and fails with:
#
#     .venv/bin/python3.12: No module named build
#
# The venv exists for ./run.sh; packaging must never see it.
#
# Outputs of sanitize_path(): the cleaned PATH, and whether a virtualenv
# directory was actually removed (so the notice only prints when it was).
# Set directly rather than echoed, because a command substitution would run the
# function in a subshell and lose the flag.
CLEAN_PATH=""
STRIPPED_VENV=0

sanitize_path() {
    local out="" entry
    STRIPPED_VENV=0
    while IFS= read -r entry; do
        [[ -z "$entry" ]] && continue
        # Drop the active venv's directories...
        if [[ -n "${VIRTUAL_ENV:-}" && ( "$entry" == "$VIRTUAL_ENV" || "$entry" == "$VIRTUAL_ENV"/* ) ]]; then
            STRIPPED_VENV=1
            continue
        fi
        # ...and any bin dir inside the project tree (.venv/bin, venv/bin),
        # which catches a venv that was put on PATH without VIRTUAL_ENV set.
        if [[ "$entry" == "$PROJECT_DIR"/* ]]; then
            STRIPPED_VENV=1
            continue
        fi
        out="${out:+$out:}$entry"
    done < <(printf '%s\n' "$PATH" | tr ':' '\n')

    # Never hand back an empty PATH.
    if [[ -z "$out" ]]; then
        out="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    fi
    CLEAN_PATH="$out"
}

# Staging directory for the 'deb' target, removed by cleanup_tmp() on exit.
TMP_BUILD_DIR=""

cleanup_tmp() {
    if [[ -n "$TMP_BUILD_DIR" && -d "$TMP_BUILD_DIR" ]]; then
        rm -rf "$TMP_BUILD_DIR"
    fi
}

# Ensure mkdocs is available before running a docs command; print an install
# hint otherwise.
require_mkdocs() {
    if ! command -v mkdocs >/dev/null 2>&1; then
        echo "error: 'mkdocs' is not installed." >&2
        echo "Install the documentation dependencies with:" >&2
        echo "    pip install -r docs/requirements.txt" >&2
        exit 1
    fi
}

# Ensure the Debian packaging tools are available.
require_debtools() {
    local missing=() pkg
    command -v dpkg-buildpackage >/dev/null 2>&1 || missing+=("dpkg-dev")
    command -v dh                >/dev/null 2>&1 || missing+=("debhelper")
    # These have no command of their own, so ask dpkg whether they are present.
    for pkg in dh-python pybuild-plugin-pyproject python3-all python3-setuptools; do
        if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
            missing+=("$pkg")
        fi
    done
    if (( ${#missing[@]} )); then
        echo "error: missing Debian packaging tools: ${missing[*]}" >&2
        echo "Install them with:" >&2
        echo "    sudo apt-get install -y debhelper dh-python pybuild-plugin-pyproject python3-all python3-setuptools" >&2
        exit 1
    fi
}

# Validate the version and pick up the maintainer before packaging.
#
# Both end up verbatim in debian/changelog, and dpkg only *warns* when that file
# fails to parse -- the build still "succeeds", but every field derived from the
# changelog is silently dropped (an empty changelog.gz, no "source changed by").
# Catching it here turns that silent corruption into an error that names the
# file to fix.
require_release_metadata() {
    if [[ -z "$VERSION" ]]; then
        echo "error: $PROJECT_DIR/VERSION is empty." >&2
        exit 1
    fi
    if [[ ! "$VERSION" =~ ^[0-9][A-Za-z0-9.+~-]*$ ]]; then
        echo "error: VERSION ('$VERSION') is not a valid Debian version." >&2
        echo "It must start with a digit and may contain only [A-Za-z0-9.+~-]" \
             "(in particular, no leading 'v' and no spaces)." >&2
        exit 1
    fi

    # Read the maintainer straight out of debian/control so the two can never
    # drift apart: a changelog trailer must be "Name <email>", and a maintainer
    # that disagrees with debian/control is a lintian error.
    MAINTAINER="$(sed -n 's/^Maintainer:[[:space:]]*//p' \
                      "$PROJECT_DIR/debian/control" | head -n 1)"
    if [[ ! "$MAINTAINER" =~ ^.+[[:space:]]\<[^[:space:]@]+@[^[:space:]@]+\>$ ]]; then
        echo "error: debian/control has no usable 'Maintainer: Name <email>' field." >&2
        echo "Got: '${MAINTAINER}'" >&2
        exit 1
    fi
}

# Write debian/changelog for the current VERSION into the given source tree.
# Generated rather than committed so VERSION stays the only place to bump.
write_changelog() {
    local src="$1"
    mkdir -p "$src/debian"
    cat > "$src/debian/changelog" <<EOF
ultragps-control (${VERSION}) unstable; urgency=medium

  * Release ${VERSION}.

 -- ${MAINTAINER}  $(date -R)
EOF
}

cmd_deb() {
    require_debtools
    require_release_metadata

    local src deb
    TMP_BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ultragps-control-deb.XXXXXX")"
    # Clean up the staging tree however we leave
    trap cleanup_tmp EXIT INT TERM

    src="$TMP_BUILD_DIR/ultragps-control-${VERSION}"
    mkdir -p "$src"

    echo ">> Staging the source tree in $src ..."
    # Copy the working tree (not just committed files, so local changes can be
    # test-packaged) minus everything that must not end up in the package.
    tar -C "$PROJECT_DIR" \
        --exclude='./.git' \
        --exclude='./.venv' \
        --exclude='./venv' \
        --exclude='./build' \
        --exclude='./site' \
        --exclude='./dist' \
        --exclude='./.vscode' \
        --exclude='__pycache__' \
        --exclude='*.egg-info' \
        --exclude='*.pyc' \
        -cf - . | tar -C "$src" -xf -

    write_changelog "$src"

    # Packaging runs under the system Python, never the project venv.
    local clean_path
    sanitize_path
    clean_path="$CLEAN_PATH"
    if (( STRIPPED_VENV )); then
        echo ">> Ignoring the active virtualenv; packaging uses the system Python."
    fi

    # pybuild shells out to "python3 -m build"; check it up front so a missing
    # python3-build is a one-line error instead of a pybuild traceback.
    if ! env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME PATH="$clean_path" \
             python3 -c 'import build' 2>/dev/null; then
        echo "error: the system python3 cannot import the 'build' module." >&2
        echo "Install it with:" >&2
        echo "    sudo apt-get install -y python3-build" >&2
        exit 1
    fi

    echo ">> Building the package (version ${VERSION}) ..."
    ( cd "$src" && env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME \
        PATH="$clean_path" dpkg-buildpackage -us -uc -b )

    # dpkg-buildpackage writes the artefacts into the parent of the source dir.
    local built="$TMP_BUILD_DIR/ultragps-control_${VERSION}_all.deb"
    if [[ ! -f "$built" ]]; then
        echo "error: dpkg-buildpackage did not produce $(basename "$built")." >&2
        echo "Artefacts found in $TMP_BUILD_DIR:" >&2
        ls -1 "$TMP_BUILD_DIR" >&2
        exit 1
    fi

    mkdir -p "$BUILD_DIR"
    deb="$BUILD_DIR/ultragps-control_${VERSION}_all.deb"
    cp "$built" "$deb"
    echo ">> Done: $deb"
    echo
    # Print the .deb package information
    dpkg-deb -I "$deb" | sed -n '/Package:/,/Description:/p'
}

cmd_docs() {
    require_mkdocs
    echo ">> Serving docs at http://127.0.0.1:8000  (Ctrl-C to stop) ..."
    ( cd "$PROJECT_DIR" && mkdocs serve )
}

cmd_docs_build() {
    require_mkdocs
    echo ">> Building static docs into $SITE_DIR ..."
    ( cd "$PROJECT_DIR" && mkdocs build --site-dir "$SITE_DIR" )
    echo ">> Done: $SITE_DIR/index.html"
}

cmd_clean() {
    local cleaned=0
    for dir in "$BUILD_DIR" "$SITE_DIR"; do
        if [[ -d "$dir" ]]; then
            echo ">> Removing $dir ..."
            rm -rf "$dir"
            cleaned=1
        fi
    done
    if [[ "$cleaned" -eq 1 ]]; then
        echo ">> Clean."
    else
        echo ">> Nothing to clean."
    fi
}

# ---- argument parsing -------------------------------------------------------
COMMAND=""
for arg in "$@"; do
    case "$arg" in
        deb|clean|docs|docs-build)  COMMAND="$arg" ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; echo >&2; usage >&2; exit 1 ;;
    esac
done

case "$COMMAND" in
    deb)        cmd_deb ;;
    clean)      cmd_clean ;;
    docs)       cmd_docs ;;
    docs-build) cmd_docs_build ;;
    "")         usage >&2; exit 1 ;;
esac
