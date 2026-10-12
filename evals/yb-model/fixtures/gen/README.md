# gen: generator fixture (synthetic)

A small schema that exercises every part of the measured pass's sample generator
(`skills/yb-model/scripts/ybm/generate.py`): an enum, a domain with a CHECK, an array, a
generated column, an identity column, a foreign key, a self-referencing foreign key, a cycle of
foreign keys, one-column and two-column CHECKs, a table without a primary key, range partitions
with and without a DEFAULT partition, and a two-column partition key. Statistics are made up.

`recorded.json` holds every SQL call a live `review --replay --measure` sent and the server's
answer, so `test_ybm.py` (class Recorded) runs the whole replay and measured pass offline. It is
release-specific output, so like the other version caches it is built locally and never
committed (git-ignored); without it the test skips. When the engine's SQL changes, the test
fails with "call not in the recording"; record it (again) on a live server:

    YBM_RECORD=evals/yb-model/fixtures/gen/recorded.json python3 skills/yb-model/scripts/yb-model.py \
      review evals/yb-model/fixtures/gen/bundle --release 2025.2.6.0 \
      --image yugabytedb/yugabyte:2025.2.6.0-b111 --replay --measure --accept-missing --out /tmp/gen
