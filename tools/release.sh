#!/usr/bin/env bash

# Copyright 2022 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# fail if we encounter an error, uninitialized variable or a pipe breaks
set -eux -o pipefail

FC_TOOLS_DIR=$(dirname $(realpath $0))
source "$FC_TOOLS_DIR/functions"
FC_ROOT_DIR=$FC_TOOLS_DIR/..

function get-profile-dir {
    case $1 in
        dev)
            echo debug
        ;;
        *)
            echo "$1"
        ;;
    esac
}

function check_swagger_artifact {
    # Validate swagger version against target version.
    local swagger_path version swagger_ver
    swagger_path=$1
    version=$2
    swagger_ver=$(get_swagger_version "$swagger_path")
    if [[ ! $version =~ v$swagger_ver.* ]]; then
        die "Artifact $swagger_path's version: $swagger_ver does not match release version $version."
    fi
}

function check_bin_artifact {
    # Validate binary version against target version.
    local bin_path version bin_version
    bin_path=$1
    version=$2
    bin_version=$($bin_path --version | head -1 | grep -oP ' \Kv.*')
    if [[ "$bin_version" != "$version" ]]; then
        die "Artifact $bin_path's version: $bin_version does not match release version $version."
    fi
}

function strip-and-split-debuginfo {
    local bin=$1
    if [ $bin -ot $bin.debug ]; then
        return
    fi
    echo "STRIP $bin"
    objcopy --only-keep-debug $bin $bin.debug
    chmod a-x $bin.debug
    objcopy --preserve-dates --strip-debug --add-gnu-debuglink=$bin.debug $bin
}

function get-firecracker-version {
    (cd src/firecracker; echo -n v; cargo pkgid | cut -d# -f2 | cut -d: -f2)
}

#### MAIN ####

# defaults
LIBC=musl
PROFILE=dev
MAKE_RELEASE=

#### Option parsing

while [[ $# -gt 0 ]]; do
  case $1 in
      --help)
          cat <<EOF
$0 - Build Firecracker

   --profile PROFILE  - Build with the specified Rust profile (default: dev)
   --libc [musl|gnu]  - Build with the specified libc (default: musl)
   --make-release     - Make release artifacts
EOF
          exit 0
      ;;
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --libc)
      LIBC="$2"
      shift 2
      ;;
    --make-release)
      MAKE_RELEASE=true
      shift 1
      ;;
    *)
      echo "Unknown option $1"
      exit 1
      ;;
  esac
done


# workaround until we rebuild devctr
git config --global --replace-all safe.directory '*'

ARCH=$(uname -m)
VERSION=$(get-firecracker-version)
PROFILE_DIR=$(get-profile-dir "$PROFILE")
CARGO_TARGET=$ARCH-unknown-linux-$LIBC
CARGO_TARGET_DIR=build/cargo_target/$CARGO_TARGET/$PROFILE_DIR
RUST_TOOLCHAIN=$(cargo version | cut -f2 -d ' ')

CARGO_REGISTRY_DIR="build/cargo_registry"
CARGO_GIT_REGISTRY_DIR="build/cargo_git_registry"
for dir in "$CARGO_REGISTRY_DIR" "$CARGO_GIT_REGISTRY_DIR"; do
    mkdir -pv "$dir"
done


CARGO_OPTS=""
# We could use Cargo's --profile when that's stable
if [ "$PROFILE" = "release" ]; then
    CARGO_OPTS+=" --release"
fi

ARTIFACTS=(firecracker jailer seccompiler-bin rebase-snap cpu-template-helper snapshot-editor)

if [ "$LIBC" == "gnu" ]; then
    # Don't build jailer. See commit 3bf285c8f
    echo "Not building jailer because glibc selected instead of musl"
    CARGO_OPTS+=" --exclude jailer"
    ARTIFACTS=(firecracker seccompiler-bin rebase-snap cpu-template-helper snapshot-editor)
fi

# Install build dependencies for libbpf-rs (used by bpf_fault live snapshot).
# libbpf-sys vendors libelf/zlib from source. Building elfutils for musl needs
# autotools + flex/bison/gawk, plus stubs for argp and obstack (glibc-only APIs
# that elfutils configure checks for but libelf itself doesn't use).
if ! command -v flex &>/dev/null; then
    say "Installing build tools for libbpf-sys vendored build..."
    apt-get update -qq && apt-get install -y -qq autoconf automake libtool autopoint flex bison gawk
fi
MUSL_LIB="/usr/lib/x86_64-linux-musl"
MUSL_INC="/usr/include/x86_64-linux-musl"
if [ ! -f "$MUSL_LIB/libargp.a" ] || [ ! -f "$MUSL_INC/argp.h" ]; then
    say "Creating musl compatibility shims for elfutils (argp, obstack)..."
    STUB_DIR=$(mktemp -d)

    # Create stub libraries
    cat > "$STUB_DIR/stubs.c" << 'STUBEOF'
#include <stddef.h>
#include <stdio.h>
#include <stdarg.h>

/* argp stubs */
const char *argp_program_version = "";
const char *argp_program_bug_address = "";
void (*argp_program_version_hook)(FILE *, void *) = NULL;
int argp_parse(const void *a, int b, char **c, unsigned d, int *e, void *f) { return 0; }
void argp_help(const void *a, FILE *b, unsigned c, char *d) {}
void argp_usage(const void *s) {}
void argp_error(const void *s, const char *f, ...) {}
void argp_failure(const void *s, int a, int b, const char *f, ...) {}
void argp_state_help(const void *s, FILE *stream, unsigned flags) {}

/* obstack stubs */
void _obstack_free(void *h, void *obj) {}
int _obstack_begin(void *h, size_t s, size_t a, void *(*c)(size_t), void (*f)(void*)) { return 0; }
void _obstack_newchunk(void *h, size_t s) {}
void obstack_free(void *h, void *obj) {}
STUBEOF
    x86_64-linux-musl-gcc -c "$STUB_DIR/stubs.c" -o "$STUB_DIR/stubs.o"
    ar rcs "$STUB_DIR/libargp.a" "$STUB_DIR/stubs.o"
    ar rcs "$STUB_DIR/libobstack.a" "$STUB_DIR/stubs.o"
    mkdir -p "$MUSL_LIB"
    cp "$STUB_DIR/libargp.a" "$STUB_DIR/libobstack.a" "$MUSL_LIB/"

    # Create comprehensive argp.h header (covers full GNU argp API for elfutils)
    cat > "$STUB_DIR/argp.h" << 'HDREOF'
#ifndef _ARGP_H
#define _ARGP_H
#include <stdio.h>
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
typedef int error_t;
#define __error_t_defined 1
struct argp_option {
    const char *name; int key; const char *arg; int flags; const char *doc; int group;
};
#define OPTION_ARG_OPTIONAL 0x1
#define OPTION_HIDDEN       0x2
#define OPTION_ALIAS        0x4
#define OPTION_DOC          0x8
#define OPTION_NO_USAGE     0x10
struct argp;
struct argp_state {
    const struct argp *root_argp;
    int argc; char **argv; int next;
    unsigned flags; unsigned arg_num; int quoted;
    void *input; void **child_inputs; void *hook;
    char *name; FILE *err_stream; FILE *out_stream; void *pstate;
};
struct argp_child {
    const struct argp *argp; int flags; const char *header; int group;
};
typedef error_t (*argp_parser_t)(int key, char *arg, struct argp_state *state);
struct argp {
    const struct argp_option *options;
    argp_parser_t parser;
    const char *args_doc;
    const char *doc;
    const struct argp_child *children;
    char *(*help_filter)(int key, const char *text, void *input);
    const char *argp_domain;
};
#define ARGP_ERR_UNKNOWN    E2BIG
#define ARGP_KEY_ARG        0
#define ARGP_KEY_ARGS       0x1000006
#define ARGP_KEY_END        0x1000001
#define ARGP_KEY_NO_ARGS    0x1000002
#define ARGP_KEY_INIT       0x1000003
#define ARGP_KEY_FINI       0x1000007
#define ARGP_KEY_SUCCESS    0x1000004
#define ARGP_KEY_ERROR      0x1000005
#define ARGP_KEY_HELP_PRE_DOC   0x2000001
#define ARGP_KEY_HELP_POST_DOC  0x2000002
#define ARGP_KEY_HELP_HEADER    0x2000003
#define ARGP_KEY_HELP_EXTRA     0x2000004
#define ARGP_KEY_HELP_DUP_ARGS_NOTE 0x2000005
#define ARGP_KEY_HELP_ARGS_DOC  0x2000006
#define ARGP_PARSE_ARGV0    0x01
#define ARGP_NO_ERRS        0x02
#define ARGP_NO_ARGS        0x04
#define ARGP_IN_ORDER       0x08
#define ARGP_NO_HELP        0x10
#define ARGP_NO_EXIT        0x20
#define ARGP_LONG_ONLY      0x40
#define ARGP_SILENT         0x80
#define ARGP_HELP_USAGE     0x01
#define ARGP_HELP_SHORT_USAGE 0x02
#define ARGP_HELP_SEE       0x04
#define ARGP_HELP_LONG      0x08
#define ARGP_HELP_PRE_DOC   0x10
#define ARGP_HELP_POST_DOC  0x20
#define ARGP_HELP_DOC       (ARGP_HELP_PRE_DOC | ARGP_HELP_POST_DOC)
#define ARGP_HELP_BUG_ADDR  0x40
#define ARGP_HELP_LONG_ONLY 0x80
#define ARGP_HELP_EXIT_ERR  0x100
#define ARGP_HELP_EXIT_OK   0x200
#define ARGP_HELP_STD_ERR   (ARGP_HELP_SEE | ARGP_HELP_EXIT_ERR)
#define ARGP_HELP_STD_USAGE (ARGP_HELP_SHORT_USAGE | ARGP_HELP_SEE | ARGP_HELP_EXIT_ERR)
#define ARGP_HELP_STD_HELP  (ARGP_HELP_SHORT_USAGE | ARGP_HELP_LONG | ARGP_HELP_EXIT_OK | ARGP_HELP_DOC | ARGP_HELP_BUG_ADDR)
extern const char *argp_program_version;
extern const char *argp_program_bug_address;
extern void (*argp_program_version_hook)(FILE *, struct argp_state *);
extern error_t argp_parse(const struct argp *, int, char **, unsigned, int *, void *);
extern void argp_help(const struct argp *, FILE *, unsigned, char *);
extern void argp_usage(const struct argp_state *);
extern void argp_error(const struct argp_state *, const char *, ...) __attribute__((format(printf,2,3)));
extern void argp_failure(const struct argp_state *, int, int, const char *, ...) __attribute__((format(printf,4,5)));
extern void argp_state_help(const struct argp_state *, FILE *, unsigned);
#endif
HDREOF

    # Create minimal obstack.h header
    cat > "$STUB_DIR/obstack.h" << 'HDREOF2'
#ifndef _OBSTACK_H
#define _OBSTACK_H
#include <stddef.h>
struct obstack { size_t chunk_size; void *chunk; char *object_base; char *next_free; char *chunk_limit; size_t temp; size_t alignment_mask; void *(*chunkfun)(size_t); void (*freefun)(void *); void *extra_arg; unsigned use_extra_arg:1; unsigned maybe_empty_object:1; unsigned alloc_failed:1; };
#define obstack_init(h) _obstack_begin(h, 0, 0, (void*(*)(size_t))malloc, free)
extern int _obstack_begin(struct obstack *, size_t, size_t, void *(*)(size_t), void (*)(void *));
extern void _obstack_newchunk(struct obstack *, size_t);
extern void _obstack_free(struct obstack *, void *);
extern void obstack_free(struct obstack *, void *);
#define obstack_chunk_size(h) ((h)->chunk_size)
#define obstack_base(h) ((void *)(h)->object_base)
#define obstack_next_free(h) ((h)->next_free)
#define obstack_object_size(h) (size_t)((h)->next_free - (h)->object_base)
#define obstack_room(h) (size_t)((h)->chunk_limit - (h)->next_free)
#define obstack_alloc(h,size) (obstack_blank(h,size), obstack_finish(h))
#define obstack_blank(h,size) do { if ((h)->next_free + (size) > (h)->chunk_limit) _obstack_newchunk(h,size); (h)->next_free += (size); } while(0)
#define obstack_grow(h,where,size) do { if ((h)->next_free + (size) > (h)->chunk_limit) _obstack_newchunk(h,size); __builtin_memcpy((h)->next_free, where, size); (h)->next_free += (size); } while(0)
#define obstack_1grow(h,c) do { if ((h)->next_free + 1 > (h)->chunk_limit) _obstack_newchunk(h,1); *(h)->next_free++ = (c); } while(0)
#define obstack_finish(h) ((h)->object_base = (h)->next_free, (void *)((h)->object_base - ((h)->next_free - (h)->object_base)))
#endif
HDREOF2

    cp "$STUB_DIR/argp.h" "$STUB_DIR/obstack.h" "$MUSL_INC/"
    rm -rf "$STUB_DIR"
fi

say "Building version=$VERSION, profile=$PROFILE, target=$CARGO_TARGET, Rust toolchain=${RUST_TOOLCHAIN}..."
# shellcheck disable=SC2086
cargo build --target "$CARGO_TARGET" $CARGO_OPTS --workspace --bins --examples

# Only strip in release mode
if [ "$PROFILE" = "release" ]; then
    for file in "${ARTIFACTS[@]}"; do
        strip-and-split-debuginfo "$CARGO_TARGET_DIR/$file"
    done
fi

say "Binaries placed under $CARGO_TARGET_DIR"

# Check static linking:
# expected "statically linked" for aarch64 and
# "static-pie linked" for x86_64
binary_format=$(file $CARGO_TARGET_DIR/firecracker)
if [[ "$PROFILE" = "release"
        && "$binary_format" != *"statically linked"*
        && "$binary_format" != *"static-pie linked"* ]]; then
    die "Binary not statically linked: $binary_format"
fi

# # # # Make a release
if [ -z "$MAKE_RELEASE" ]; then
    exit 0
fi

if [ "$LIBC" != "musl" ]; then
    die "Releases using a libc other than musl not supported"
fi

SUFFIX=$VERSION-$ARCH
RELEASE_DIR=release-$SUFFIX
mkdir "$RELEASE_DIR"
for file in "${ARTIFACTS[@]}"; do
    check_bin_artifact "$CARGO_TARGET_DIR/$file" "$VERSION"
    cp -v "$CARGO_TARGET_DIR/$file" "$RELEASE_DIR/$file-$SUFFIX"
    cp -v "$CARGO_TARGET_DIR/$file.debug" "$RELEASE_DIR/$file-$SUFFIX.debug"
done
cp -v "resources/seccomp/$CARGO_TARGET.json" "$RELEASE_DIR/seccomp-filter-$SUFFIX.json"
# Copy over arch independent assets
cp -v -t "$RELEASE_DIR" LICENSE NOTICE THIRD-PARTY
check_swagger_artifact src/firecracker/swagger/firecracker.yaml "$VERSION"
cp -v src/firecracker/swagger/firecracker.yaml "$RELEASE_DIR/firecracker_spec-$VERSION.yaml"

CPU_TEMPLATES=(C3 T2 T2S T2CL T2A V1N1)
for template in "${CPU_TEMPLATES[@]}"; do
    cp -v tests/data/custom_cpu_templates/$template.json $RELEASE_DIR/$template-$VERSION.json
done

(
    cd "$RELEASE_DIR"
    find . -type f -not -name "SHA256SUMS" |sort |xargs sha256sum >SHA256SUMS
)
