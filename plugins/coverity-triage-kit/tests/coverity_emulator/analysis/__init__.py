"""Mock Coverity Analysis commands (cov-build, cov-analyze, cov-format-errors, cov-commit-defects).

The analysis runs real static analyzers (clang --analyze, gcc -fanalyzer, cppcheck with the MISRA
addon) and source-shape rules, and turns their findings into Coverity checkers, events and
checkerProperties (specification 4 and 5.9 - 5.11).

Environment switches (specification 1.5):
    COV_EMU_EMIT_FAIL=glob[,glob]     cov-build: these source files fail to emit
    COV_EMU_FORMAT_ERRORS_ZERO_EXIT=1 cov-format-errors: exit 1 when there are no issues (default 0)
    COV_EMU_COMMIT_RESULT=0|2|3       cov-commit-defects: 3 commits and exits 3 (non-fatal errors);
                                      2 fails without committing
    COV_EMU_PATH_MAP=local=remote[;..] the analysis reports paths as if built under "remote"
                                      (a build agent such as /build/agent/work/envmon)
    COV_EMU_VERSION=2024.12.1         the version printed in the banners
    COV_EMU_STRIP_KEEP_SLASH=0        --strip-path drops the leading "/" (real data) instead of keeping it
                                      (official example; default)
    COV_EMU_TOOLS=clang,gcc,cppcheck  analyzers to run (default: all that are installed)
"""
