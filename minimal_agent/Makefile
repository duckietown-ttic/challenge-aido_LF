repo=aidonode-random_agent
# repo=$(shell basename -s .git `git config --get remote.origin.url`)
branch=$(shell git rev-parse --abbrev-ref HEAD)
tag=duckietown/$(repo):$(branch)

build:
	docker build --pull  -t $(tag) .

build-no-cache:
	docker build --pull -t $(tag)  --no-cache .

#push: build
#	docker push $(tag)

test-data1-direct:
	./minimal_agent.py < test_data/in1.json > test_data/out1.json

test-data1-docker:
	docker run -i $(tag) < test_data/in1.json > test_data/out1.json


submit:
	dts challenges submit


# submit-robotarium:
# 	dts challenges submit --challenge aido2_LF_r_pri,aido2_LF_r_pub

submit-baseline-sim:
	dts challenges submit --impersonate 1639 --user-label "straight" --challenge aido3-LF-sim-validation,aido3-LF-sim-testing,aido3-LFV-sim-validation,aido3-LFV-sim-testing,aido3-LFVI-sim-validation,aido3-LFVI-sim-testing

submit-baseline-real-validation:
	dts challenges submit --impersonate 1639 --user-label "straight" --challenge aido3-LF-real-validation,aido3-LFV-real-validation
