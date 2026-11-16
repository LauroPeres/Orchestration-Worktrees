# Pitfalls: bugs that threw no error

Running many copies of one stack on one machine fails in a particular way: nothing crashes. A
command exits 0, a health check passes, a page answers, and the result is still wrong. These are
six bugs of that kind found while building wt, each with what gave it away, why it happened, what
wt does now and the test that keeps it fixed. The full design notes are in [SPEC.md](SPEC.md).

## 1. One project's `down` deleted another project's data

- **Symptom:** `wt down main` in one project emptied the Redis keys, Kafka groups and topics and
  the Keycloak client of a *different* project's `main`. No error in either project.
- **Cause:** the shared servers serve every project, but the per-branch names used only the branch
  (`main`), so two projects' `main` branches had the same names.
- **Fix:** the project is part of every name: Redis prefix `shop:feat-cart:`, Kafka group
  `<group>-shop.feat-cart`, Keycloak client `<client>-shop.feat-cart`. `.` and `:` cannot appear in
  a project or branch slug, so two different pairs never produce the same name.
- **Kept fixed by:** `tests/test_redis.py`, `tests/test_providers_base.py`,
  `tests/integration/test_redis.py`. ([SPEC §20](SPEC.md#20-v051-dependencies-run-together))

## 2. Two branches on one URL, and every check passed

- **Symptom:** two projects with the same domain and branch both routed the same host. Requests
  went to either backend, and the post-`up` verification still passed, because *some* backend
  answered.
- **Cause:** Traefik reads every route file from one shared folder. Two identical `Host(...)`
  rules are not a conflict for it: it serves both.
- **Fix:** before writing a route file, `up` compares its hosts with every other route file and
  fails before creating anything: `host <host> is already routed by <project>/<slug>`.
- **Kept fixed by:** `tests/test_orchestrator.py`.
  ([SPEC §20](SPEC.md#20-v051-dependencies-run-together))

## 3. Every route answered 404 after a Docker restart

- **Symptom:** after a Docker Desktop restart, every branch URL returned 404. Containers were up,
  route files were on disk, Traefik was running.
- **Cause:** Traefik's bind mounts came back as empty folders, so it saw no routes and no
  certificates.
- **Fix:** `wt doctor` compares the file names on the host with what the container sees and fails
  `traefik mounts` when they differ; `--fix` recreates the container (not `docker restart`, which
  reuses the stale mount). `up` also checks that its new route file is visible inside the
  container, and recreates Traefik once if not.
- **Kept fixed by:** `tests/test_doctor.py`.
  ([SPEC §15](SPEC.md#15-v03-implementation-notes))

## 4. `down` said it cleaned a Kafka group it could not delete

- **Symptom:** removing a branch looked successful, but its Kafka consumer group was still on the
  broker.
- **Cause:** Kafka refuses to delete a group that still has a live member, and the Kafka CLI
  reports that by printing `GroupNotEmptyException` and **exiting 0**.
- **Fix:** wt retries for up to 20 s (a stopped consumer leaves once the broker notices), then
  fails with `consumer group <group> still has members`. `down` stops there and keeps the branch
  as `removing`, so the group is never forgotten, and running `down` again finishes the job.
- **Kept fixed by:** `tests/test_kafka.py`, `tests/integration/test_kafka.py`.
  ([SPEC §18](SPEC.md#18-v04-redis-and-kafka-providers-shop-worker))

## 5. A new branch silently started from old code

- **Symptom:** a branch created by wt had the right name but was missing recent commits.
- **Cause:** new branches started from the local checkout's `HEAD`, which was behind `origin`.
  Git does not warn about that.
- **Fix:** before creating a branch, wt runs `git fetch origin` and starts from `origin/HEAD`
  (else `origin/main` or `origin/master`), with no upstream so a plain `git push` cannot reach the
  default branch. The log says where it started: `created branch 'x' in repo 'r' from origin/main
  (a1b2c3d)`. Offline, it says it is using the last known state.
- **Kept fixed by:** `tests/test_orchestrator.py`.
  ([SPEC §28](SPEC.md#28-v0101-new-branches-start-from-the-latest-origin))

## 6. Every worktree looked like 3 GB

- **Symptom:** disk tools showed every worktree at about 3 GB, as if each held a full copy of its
  dependencies.
- **Cause:** on btrfs, wt's dependency copies are reflinks that share disk blocks. `du` and file
  managers add up file sizes, so shared blocks are counted once per copy. The number was wrong,
  not the disk use.
- **Fix:** `wt du` reports apparent, **exclusive** and shared bytes per wt folder, from
  `btrfs filesystem du`, and `wt doctor` warns on exclusive bytes, which is what a prune frees.
- **Kept fixed by:** `tests/test_disk.py`, `tests/test_doctor.py`.
  ([SPEC §36](SPEC.md#36-v0160-shared-installs-and-real-disk-use))

## The pattern

Each of these was found by checking the result instead of trusting the exit code. That rule is
built into wt: a step is done when it is **verified** (a URL answers through Traefik, a database
clone matches its source, a route file is visible inside the container), not when a command
returned.
