.PHONY: install fmt fmt-check validate validate-json migrate build build-capsules bootstrap-capsules check-capsules test check context new merge delete clean pr-annotate edit-notes impact canon-check extract-plan extract-apply extract-check archive-plan

# Optional model/backend routing for every LLM-invoking target (extract-plan,
# build-capsules, edit-notes, canon-check): make extract-plan FILE=... MODEL=opus
# BACKEND=api. Unset means each tool's own default — downstream projects route
# bigger models to extraction and smaller ones to capsules, so this must not require
# bypassing the Makefile.
LLM_FLAGS = $(if $(MODEL),--model $(MODEL),) $(if $(BACKEND),--backend $(BACKEND),)

install:                  ## pip install -r requirements.txt
	pip install -r requirements.txt

fmt:                      ## rewrite every canon file into canonical form (key order, YAML style)
	python -m tools.fmt

fmt-check:                ## verify canon is canonically formatted (no writes); CI gate
	python -m tools.fmt --check

validate:                 ## schema + referential integrity + dependency-DAG checks
	python -m tools.validate

validate-json:            ## same checks, machine-readable JSON (file/code/id per finding)
	python -m tools.validate --json

migrate:                  ## apply pending schema migrations to canon, then bump .schema-version
	python -m tools.migrate

build:                    ## regenerate the DETERMINISTIC derived/ (graph, indices)
	python -m tools.build

build-capsules:           ## opt-in: (re)generate stale/missing capsules via LLM (costs); --force, --only ID; [MODEL=...] [BACKEND=cli|api]
	python -m tools.capsules $(LLM_FLAGS)

bootstrap-capsules:       ## create PLACEHOLDER capsules for entities missing one (no LLM)
	python -m tools.capsules --bootstrap

check-capsules:           ## verify every capsule exists and matches its source hash (no LLM)
	python -m tools.capsules --check

test:                     ## run the consistency-check test suite
	python -m pytest -q

check: fmt-check validate extract-check test check-capsules  ## what CI runs (plus the deterministic derived-drift guard)

# Assemble a context bundle:  make context ID=thm.main_bound K=1
context:
	python -m tools.assemble_context $(ID) --k $(or $(K),1) --stats

new:                      ## scaffold an entity:  make new TYPE=statement SLUG=foo NAME="Foo"  (a proof needs SET="--set proves=thm.x")
	python -m tools.lifecycle new $(TYPE) $(SLUG) --name "$(NAME)" $(SET)

merge:                    ## fold a duplicate into a canonical entity:  make merge DUP=thm.x CANON=thm.y
	python -m tools.lifecycle merge $(DUP) $(CANON)

delete:                   ## delete if unreferenced (FORCE=1 to override):  make delete ID=thm.x
	python -m tools.lifecycle delete $(ID) $(if $(FORCE),--force,)

pr-annotate:               ## PR-scoped diff + validator report (BASE=origin/main by default)
	python -m tools.pr_annotate --base $(or $(BASE),origin/main)

impact:                    ## reverse-mentions impact analysis (BASE=origin/main by default)
	python -m tools.impact --base $(or $(BASE),origin/main)

canon-check:               ## advisory LLM canon-check (BASE=origin/main; opt-in, costs — CLI backend by default); [MODEL=...] [BACKEND=cli|api]
	python -m tools.canon_check --base $(or $(BASE),origin/main) $(LLM_FLAGS)

edit-notes:                ## advisory critique note for one exposition: make edit-notes FILE=expositions/.../x.md [PATCH=1] [MODEL=...] [BACKEND=cli|api]
	python -m tools.editor $(FILE) $(if $(PATCH),--patch,) $(LLM_FLAGS)

extract-plan:              ## Extractor plan for one raw file (LLM): make extract-plan FILE=raw/x.md [FORCE=1] [MODEL=...] [BACKEND=cli|api]
	python -m tools.extract plan $(FILE) $(if $(FORCE),--force,) $(LLM_FLAGS)

extract-apply:             ## materialize a triaged plan (no LLM): make extract-apply PLAN=extraction/plans/x.md
	python -m tools.extract apply $(PLAN)

extract-check:             ## structurally validate pending extraction plans (no LLM; a gate leg — parked plans go to extraction/archived/)
	python -m tools.extract check

archive-plan:              ## park a superseded pending plan (no LLM): make archive-plan PLAN=extraction/plans/x.md
	python -m tools.extract archive $(PLAN)

clean:
	rm -rf derived/*
