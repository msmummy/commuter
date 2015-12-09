

# Run model; go get lunch
./par-spec.py models.fs -t testgen.c -m model.out --max-tests-per-path 500 -f '!reboot,!sync,!fsync'
# Split testgen.c for parallel build
./split-testgen.py -d ext/sv6/libutil < testgen.c
