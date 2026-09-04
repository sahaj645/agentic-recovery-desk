.PHONY: demo eval test clean

# Seeded batch of 1,000 items, every baseline, the headline results table.
demo:
	python run.py demo

# Full harness across several seeds: regression table and exception list.
eval:
	python run.py eval --seeds 20260905 20260906 20260907

# Unit and policy-gate invariants. Deliberately small.
test:
	python -m pytest tests -q

clean:
	rm -rf results/reports/* .cache
