#!/bin/bash

# SPDX-License-Identifier: Apache-2.0

# In-container entry point for the snapshot benchmark: the same session
# setup as test.sh, but running the benchmark runner instead of pytest.

# fail if we encounter an error, uninitialized variable or a pipe breaks
set -eu -o pipefail

TOOLS_DIR=$(dirname $0)
source "$TOOLS_DIR/functions"

# Set our TMPDIR inside /srv, so all files created in the session end up in one
# place
say "Create TMPDIR in /srv"
export TMPDIR=/srv/tmp
mkdir -pv $TMPDIR

# Convert the Docker created cgroup so we can create cgroup children
# From https://github.com/containerd/containerd/issues/6659
say "cgroups v2: enable nesting"
CGROUP=/sys/fs/cgroup
if [ -f $CGROUP/cgroup.controllers -a -e $CGROUP/cgroup.type ]; then
    # move the processes from the root group to the /init group,
    # otherwise writing subtree_control fails with EBUSY.
    # An error during moving non-existent process (i.e., "cat") is ignored.
    mkdir -p $CGROUP/init
    xargs -rn1 < $CGROUP/cgroup.procs > $CGROUP/init/cgroup.procs || :
    # enable controllers
    sed -e 's/ / +/g' -e 's/^/+/' < $CGROUP/cgroup.controllers \
        > $CGROUP/cgroup.subtree_control
fi

if [ -f build/current_artifacts ]; then
  say "Copy artifacts to /srv/test_artifacts, so hardlinks work"
  cp -ruvfL $(cat build/current_artifacts) /srv/test_artifacts
else
  mkdir -p /srv/test_artifacts
  say_warn "No current artifacts are set. The benchmark might break"
fi

cd tests
ret=0
python3 -u bench/run_snapshot_bench.py "$@" || ret=$?

# The container runs as root; hand the results back to whoever owns the
# mounted results store.
if [ -d /bench_results ]; then
    chown -R --reference=/bench_results /bench_results || :
fi

exit $ret
