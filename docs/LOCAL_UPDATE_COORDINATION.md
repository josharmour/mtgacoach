# Local update coordination

The November 1 update attempt closed Coach and Arena after match `9f593806`
ended at 04:50:34–35 UTC. Coach reopened at 04:52:07. The separate installer
then failed to acquire `desktop.lock`, before modifying the checkout. The logs
establish a reopened process, not whether the user or an automatic mechanism
launched it. Build `07098e91` remained installed.

Further deployment restarts are paused. Preparing code does not resume them.

When activation resumes, use one continuous operation for the approved safe
shutdown, installation, installed-source checks, and normal relaunch. Do not
close the apps and return to an agent/tool decision before starting installation.

1. Verify a fresh completed match, no subsequent queue submission, and the
   original process identities. Never use an old completed checkpoint alone.
2. Close only those verified processes. If a new instance appears, leave it
   alone; do not loop over shutdown attempts.
3. Acquire the existing runtime `desktop.lock` with an exclusive nonblocking
   `flock`. If Coach has reopened and owns it, abort before changing code.
   Never remove, replace, truncate, or override the lock to gain ownership.
4. With the lock held, check that both apps remain closed, the installed branch
   and baseline are expected, and the checkout is clean. Preserve settings and
   history. Fast-forward only to the reviewed, tested revision.
5. Keep the same lock held through protected-file checks and installed-source
   smoke verification. These checks must not launch a competing Coach or Arena.
6. Release the lock and use the normal launcher. Its own singleton check handles
   a simultaneous user launch. If a process already exists, inspect it rather
   than start or stop another one. Verify source revision, bridge connection,
   queue identity, and actions separately.

If installation or verification fails, report the exact resulting revision and
app state. Do not claim success or restart unverified code. App startup failure
does not authorize rolling back unrelated changes or bypassing security settings.

`tests/test_update_lock_coordination.py` checks real subprocess contention using
temporary runtime directories. It verifies both ownership orders and release;
it does not launch installed apps, manipulate matches, or qualify a live rollout.
