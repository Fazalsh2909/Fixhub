FixHub Agent Benchmark
======================

Scenarios: 17 (ran 17, skipped 0)

Bug-fix success: 11/11
Safe failures: 3/3
Reliability probes: 3/3
Unexpected failures: 0

Success rate: 100%

Average steps: 4.8
Median steps: 4
Average tool calls: 4.1
Blocked duplicate calls: 4
Average repair rounds: 0.0
Average runtime: 2.7s

Failure breakdown:
WRONG_DIAGNOSIS: 0
WRONG_FILE: 0
BAD_EDIT: 0
TEST_FAILURE: 0
VALIDATION_FAILURE: 0
TOOL_FAILURE: 0
PATH_FAILURE: 0
COMMAND_FAILURE: 0
LOOPING: 0
TIMEOUT: 0
MEMORY_FAILURE: 0
LIFECYCLE_FAILURE: 0
SANDBOX_FAILURE: 0
OTHER: 0
EXPECTED_SAFE_FAILURE: 0

Per-scenario:
ID                     Result Steps Tools Repairs Runtime Failure
01-off-by-one          PASS   4     4     0       4       -
02-exception-handling  PASS   4     4     0       4       -
03-api-response        PASS   5     5     0       4       -
04-missing-validation  PASS   4     4     0       4       -
05-sql-query           PASS   4     4     0       4       -
06-config-parsing      PASS   4     4     0       4       -
07-javascript          PASS   4     4     0       3       -
08-ci-unit-test        PASS   5     5     0       4       -
09-lint-format         PASS   4     4     0       6       -
10-dep-pin-parse       PASS   4     4     0       4       -
S1-timeout             PASS   5     2     0       2       EXPECTED_SAFE_FAILURE
S2-ambiguous           PASS   4     4     0       1       EXPECTED_SAFE_FAILURE
S3-path-security       PASS   7     4     0       0       EXPECTED_SAFE_FAILURE
R-path-discipline      PASS   6     3     0       0       EXPECTED_SAFE_FAILURE
F-duplicates           PASS   9     6     0       0       EXPECTED_SAFE_FAILURE
K-cancel               PASS   2     1     0       0       EXPECTED_SAFE_FAILURE
J-repair               PASS   7     7     0       6       -
